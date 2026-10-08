package jt1078bridge

import (
	"crypto/rand"
	"encoding/binary"
)

// RTPStream holds the state RTP requires for the lifetime of a stream (fixed
// SSRC, incrementing sequence number) and produces bytes ready to write to
// the ZLMediaKit TCP connection (with the length prefix).
//
// The sequence number belongs to this stream -- the JT1078 packet's Seq (see
// jt1078.Packet.Seq) is deliberately NOT reused, because the number of RTP
// packets emitted per frame need not match the number of JT1078 packets
// received 1:1 (a frame reassembled from several JT1078 fragments can split
// into several NALUs/RTP packets, and several NALUs in a single atomic
// JT1078 packet are also emitted as several RTP packets).
type RTPStream struct {
	ssrc    uint32
	seq     uint16
	payload uint8
}

// NewRTPStream creates the state for a new stream. SSRC is random (standard
// RTP practice for a new sender, RFC 3550 §8.1) and so is the initial
// sequence number, so it does not always start at 0 (makes a restarted
// stream easier to detect on the receiving side).
func NewRTPStream(payloadType uint8) (*RTPStream, error) {
	var buf [6]byte
	if _, err := rand.Read(buf[:]); err != nil {
		return nil, err
	}
	return &RTPStream{
		ssrc:    binary.BigEndian.Uint32(buf[0:4]),
		seq:     binary.BigEndian.Uint16(buf[4:6]),
		payload: payloadType,
	}, nil
}

// FrameToRTP converts an already reassembled video frame (Annex-B bytes, as
// JT1078 delivered them) into the corresponding RTP packets. Each NALU is
// sent as a single "Single NAL Unit" packet if it fits, or fragmented into
// several FU-A packets otherwise (see fragmentNALU), each already carrying
// the 2-byte length prefix ZLMediaKit expects, concatenated into one []byte
// ready for a single Write() to the TCP connection.
//
// timestampMs is the frame's JT1078 Timestamp (ms). All NALUs (and all their
// FU-A fragments) of one frame share the same RTP timestamp -- an RTP
// invariant, not a simplification (RFC 3550 §5.1). The Marker bit is set to 1
// only on the LAST packet of the LAST NALU of the frame (RFC 6184 §5.3); for
// a fragmented NALU that is its last FU-A fragment, never the intermediate
// ones.
func (s *RTPStream) FrameToRTP(rawFrame []byte, timestampMs uint64) []byte {
	nalus := splitAnnexBNALUs(rawFrame)
	if len(nalus) == 0 {
		return nil
	}
	ts := msToRTPTimestamp(timestampMs)

	var out []byte
	for i, nalu := range nalus {
		fragments := fragmentNALU(nalu)
		for j, frag := range fragments {
			marker := i == len(nalus)-1 && j == len(fragments)-1
			pkt := buildRTPPacket(s.payload, s.seq, ts, s.ssrc, marker, frag)
			s.seq++
			out = append(out, lengthPrefix(pkt)...)
		}
	}
	return out
}
