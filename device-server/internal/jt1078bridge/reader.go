package jt1078bridge

import (
	"errors"

	"github.com/cuteLittleDevil/go-jt808/protocol/jt1078"
)

// maxPacketReaderBuffer bounds how large a STILL-INCOMPLETE JT1078 packet
// (waiting for more bytes) may grow before it is considered corrupt -- same
// reason and pattern as maxFrameBufferSize in the JT808 server
// (internal/jt808server/conn.go): without a bound, a sender that declares a
// large DataBodyLen and never finishes sending it (bug or attack) would grow
// the buffer without limit. A legitimate JT1078 packet never exceeds ~980
// bytes (header ~30 + body max 950), so this leaves ample margin.
//
// Important: the limit is checked on the REMAINDER after extracting every
// complete packet, never on the total just read from the socket. Checking
// before extracting produced false positives: a single socket read can carry
// several KB of legitimate data if it holds many complete packets at once
// (an I-frame is split into dozens of JT1078 packets that arrive in a
// burst), and that is not an attack.
const maxPacketReaderBuffer = 8192

// packetReader accumulates raw bytes from a JT1078 connection and yields
// complete packets. Unlike JT808 (delimited by 0x7e), JT1078 has no
// delimiters: each packet's declared length says where it ends.
// jt1078.Packet.Decode already reads that; this type only hides the "not
// enough bytes yet" handling (ErrHeaderLength2Short/ErrBodyLength2Short are
// NOT real errors, they mean "try again when more data arrives").
type packetReader struct {
	buf []byte
}

// Feed appends bytes just read from the socket and returns every packet that
// can already be decoded in full. A non-nil error means genuinely corrupt
// data (not just incomplete) and the connection must be closed: JT1078 has
// no way to "resync" the way JT808 does with 0x7e.
func (r *packetReader) Feed(data []byte) ([]*jt1078.Packet, error) {
	r.buf = append(r.buf, data...)

	var packets []*jt1078.Packet
	for {
		p := jt1078.NewPacket()
		remain, err := p.Decode(r.buf)
		if err != nil {
			if errors.Is(err, jt1078.ErrHeaderLength2Short) || errors.Is(err, jt1078.ErrBodyLength2Short) {
				break // more data needed, not an error
			}
			return packets, err
		}
		packets = append(packets, p)
		r.buf = remain
		if len(r.buf) == 0 {
			return packets, nil
		}
	}

	if len(r.buf) > maxPacketReaderBuffer {
		return packets, errors.New("jt1078bridge: incomplete packet exceeds the maximum allowed size")
	}
	return packets, nil
}
