package jt1078bridge

import "github.com/cuteLittleDevil/go-jt808/protocol/jt1078"

// Frame is a video/audio frame reassembled from one or more JT1078 packets
// (per SubcontractType), ready to convert to RTP.
type Frame struct {
	Data        []byte
	TimestampMs uint64
	DataType    jt1078.DataType
}

// Reassembler accumulates JT1078 fragments (SubcontractType:
// first/middle/last) until a frame is complete. JT1078 carries no explicit
// frame identifier -- reconstruction relies on arrival order within ONE TCP
// connection per channel, which is exactly what TCP guarantees (in-order
// bytes). Not safe for concurrent use: one Reassembler per connection/channel,
// never shared.
type Reassembler struct {
	buf         []byte
	timestampMs uint64
	dataType    jt1078.DataType
	inProgress  bool
}

// maxReassemblyBytes bounds how large a frame under reassembly may grow
// before it is discarded. Same reason as the JT808 server buffer cap
// (conn.go maxFrameBufferSize): without it, "first" fragments without their
// matching "last" (buggy device, or malicious traffic on the video port)
// would grow memory without bound. 4MB is generous for a video I-frame at the
// typical resolution of a low-cost MDVR.
const maxReassemblyBytes = 4 << 20

// Feed processes an already decoded JT1078 packet. It returns the complete
// frame and ok=true when the packet completes a frame (atomic, or the last
// of a fragment series); ok=false while the frame is still incomplete.
func (r *Reassembler) Feed(p *jt1078.Packet) (Frame, bool) {
	switch p.SubcontractType {
	case jt1078.SubcontractTypeAtomic:
		// Defensive copy: p.Body is a sub-slice of the caller's read buffer
		// (packetReader.buf), which stays alive and is reused/grown on the
		// next socket read. Without copying, a later append() on that buffer
		// could overwrite these bytes before they are converted to RTP --
		// silent data corruption, not a crash, the worst kind of video bug to
		// debug. The fragmented cases (First/Middle/Last) already copy
		// naturally via append() into r.buf (the Reassembler's OWN buffer), so
		// the explicit copy is only needed here.
		data := append([]byte(nil), p.Body...)
		return Frame{Data: data, TimestampMs: p.Timestamp, DataType: p.DataType}, true

	case jt1078.SubcontractTypeFirst:
		r.buf = append(r.buf[:0], p.Body...)
		r.timestampMs = p.Timestamp
		r.dataType = p.DataType
		r.inProgress = true
		return Frame{}, false

	case jt1078.SubcontractTypeMiddle:
		if !r.inProgress {
			// Middle fragment without a preceding "first" (lost packet, or
			// reconnect mid-frame): there is no valid frame to rebuild, so it
			// is discarded instead of producing a corrupt frame with a gap.
			return Frame{}, false
		}
		r.appendBounded(p.Body)
		return Frame{}, false

	case jt1078.SubcontractTypeLast:
		if !r.inProgress {
			return Frame{}, false
		}
		r.appendBounded(p.Body)
		frame := Frame{Data: r.buf, TimestampMs: r.timestampMs, DataType: r.dataType}
		r.buf = nil
		r.inProgress = false
		return frame, true

	default:
		return Frame{}, false
	}
}

func (r *Reassembler) appendBounded(body []byte) {
	if len(r.buf)+len(body) > maxReassemblyBytes {
		// Over the limit: abort this frame instead of growing further.
		r.buf = nil
		r.inProgress = false
		return
	}
	r.buf = append(r.buf, body...)
}
