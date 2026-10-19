// Package jt1078bridge translates the JT/T 1078-2016 video an MDVR sends into
// standard RTP and delivers it to ZLMediaKit.
//
// Why this bridge exists (the only viable option with open-source
// ZLMediaKit): the open-source edition of ZLMediaKit does NOT understand
// JT1078 natively -- that is a feature of its closed professional edition.
// There is a community shortcut ("multi-port mode": point the device straight
// at ZLMediaKit's RTP port via the JT808 0x9101 command, translating
// nothing), but the ZLMediaKit author explicitly advises against it: JT1078
// bytes are not bit-compatible with RTP (different header), so the shortcut
// relies on ZLMediaKit tolerating something that is not really its format.
// Existing open-source JT808 servers only implement the JT808/JT1078
// signaling and leave "the streaming service" to whoever deploys them. So
// this package builds the real translation: it reassembles the JT1078
// fragments of a frame, splits the H.264 NAL units, and packs them as RTP
// (RFC 6184) over TCP with the 2-byte length framing ZLMediaKit's rtp_proxy
// expects in tcp_mode=1 (confirmed by reading src/Rtp/RtpSplitter.cpp in
// ZLMediaKit itself).
package jt1078bridge

import (
	"encoding/binary"
)

// RTP payload types ZLMediaKit uses by default for its rtp_proxy (see
// [rtp_proxy] h264_pt/h265_pt in its config.ini -- the authoritative source
// for these values).
const (
	PayloadTypeH264 = 98
	PayloadTypeH265 = 99
)

// rtpHeaderSize is the fixed RTP header size without extensions (RFC 3550
// §5.1): V/P/X/CC (1) + M/PT (1) + sequence (2) + timestamp (4) + SSRC (4).
const rtpHeaderSize = 12

// videoClockRate is the standard RTP clock rate for H.264/H.265 (RFC 6184
// §5.1: fixed at 90000 Hz, not configurable by the payload).
const videoClockRate = 90000

// maxRTPPayloadSize bounds how many payload bytes a single RTP packet
// carries. This is NOT a network MTU constraint (we use TCP, not UDP): it is
// that ZLMediaKit, like any normal RTP receiver, does not expect "RTP
// packets" of tens of KB. A frame reassembled from several JT1078 fragments
// can hold a single NALU (the video slice) of 70KB or more; putting it whole
// into one RTP packet makes ZLMediaKit treat it as corrupt (an "abnormal RTP
// packet length, searching ssrc to recover context" log line). This exact bug
// was reproduced against a real ZLMediaKit instance. 1400 payload bytes is
// the conventional margin to stay under a 1500 Ethernet MTU with room for
// RTP/IP/TCP headers -- the same value most real RTP/H.264 senders use,
// regardless of transport.
const maxRTPPayloadSize = 1400

// buildRTPPacket builds ONE RTP packet around an already prepared payload (a
// whole NALU in RFC 6184 §5.6 "Single NAL Unit" mode, or an RFC 6184 §5.8
// FU-A fragment -- see fragmentNALU). It does not decide fragmentation; it
// only builds the 12-byte header over the given payload.
func buildRTPPacket(pt uint8, seq uint16, timestamp90k uint32, ssrc uint32, marker bool, nalu []byte) []byte {
	pkt := make([]byte, rtpHeaderSize+len(nalu))
	pkt[0] = 0x80 // V=2, P=0, X=0, CC=0
	pkt[1] = pt & 0x7f
	if marker {
		pkt[1] |= 0x80
	}
	binary.BigEndian.PutUint16(pkt[2:4], seq)
	binary.BigEndian.PutUint32(pkt[4:8], timestamp90k)
	binary.BigEndian.PutUint32(pkt[8:12], ssrc)
	copy(pkt[rtpHeaderSize:], nalu)
	return pkt
}

// msToRTPTimestamp converts the JT1078 timestamp (uint64, milliseconds,
// arbitrary per the standard) to the RTP video timestamp (uint32, 90kHz clock
// units). Truncation/wrap to 32 bits is normal, expected RTP behavior (the
// receiver handles it with modular arithmetic), not a bug.
func msToRTPTimestamp(ms uint64) uint32 {
	return uint32(ms * (videoClockRate / 1000))
}

// fragmentNALU splits a NALU into the RTP payloads to send: the whole NALU
// if it fits in maxRTPPayloadSize ("Single NAL Unit Packet", RFC 6184 §5.6),
// or several FU-A fragments (RFC 6184 §5.8) otherwise.
//
// FU-A replaces the NALU's original header byte with two bytes: the "FU
// indicator" (same forbidden_zero_bit/nri bits as the original header,
// type=28) and the "FU header" (S/E start/end bits + the real NALU type in
// the low 5 bits).
func fragmentNALU(nalu []byte) [][]byte {
	if len(nalu) == 0 {
		return nil
	}
	if len(nalu) <= maxRTPPayloadSize {
		return [][]byte{nalu}
	}

	naluHeader := nalu[0]
	forbiddenAndNRI := naluHeader & 0xE0 // bits 7-5
	naluType := naluHeader & 0x1F
	fuIndicator := forbiddenAndNRI | 28 // 28 = FU-A

	payload := nalu[1:]
	const chunkSize = maxRTPPayloadSize - 2 // -2: FU indicator + FU header

	var fragments [][]byte
	for i := 0; i < len(payload); i += chunkSize {
		end := i + chunkSize
		if end > len(payload) {
			end = len(payload)
		}
		fuHeader := naluType
		if i == 0 {
			fuHeader |= 0x80 // S: first fragment
		}
		if end == len(payload) {
			fuHeader |= 0x40 // E: last fragment
		}
		frag := make([]byte, 0, 2+(end-i))
		frag = append(frag, fuIndicator, fuHeader)
		frag = append(frag, payload[i:end]...)
		fragments = append(fragments, frag)
	}
	return fragments
}

// lengthPrefix prepends the 2-byte big-endian prefix ZLMediaKit expects for
// each RTP packet when connected in tcp_mode=1 (length of the following
// packet, excluding these 2 bytes -- confirmed in
// RtpSplitter::onSearchPacketTail_l in the ZLMediaKit source).
func lengthPrefix(rtpPacket []byte) []byte {
	out := make([]byte, 2+len(rtpPacket))
	binary.BigEndian.PutUint16(out[:2], uint16(len(rtpPacket)))
	copy(out[2:], rtpPacket)
	return out
}
