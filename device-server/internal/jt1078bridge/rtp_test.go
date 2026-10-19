package jt1078bridge

import (
	"encoding/binary"
	"testing"
)

func TestBuildRTPPacket_HeaderLayout(t *testing.T) {
	nalu := []byte{0x67, 0xAA, 0xBB} // fake NALU (SPS-like header byte)
	pkt := buildRTPPacket(PayloadTypeH264, 1234, 90000, 0xDEADBEEF, true, nalu)

	if len(pkt) != rtpHeaderSize+len(nalu) {
		t.Fatalf("length = %d, want %d", len(pkt), rtpHeaderSize+len(nalu))
	}
	if pkt[0] != 0x80 {
		t.Errorf("byte0 = %#x, want 0x80 (V=2,P=0,X=0,CC=0)", pkt[0])
	}
	if pkt[1] != (0x80 | PayloadTypeH264) {
		t.Errorf("byte1 = %#x, want marker=1 + PT=98", pkt[1])
	}
	if got := binary.BigEndian.Uint16(pkt[2:4]); got != 1234 {
		t.Errorf("seq = %d, want 1234", got)
	}
	if got := binary.BigEndian.Uint32(pkt[4:8]); got != 90000 {
		t.Errorf("timestamp = %d, want 90000", got)
	}
	if got := binary.BigEndian.Uint32(pkt[8:12]); got != 0xDEADBEEF {
		t.Errorf("ssrc = %#x, want 0xDEADBEEF", got)
	}
	if string(pkt[12:]) != string(nalu) {
		t.Errorf("payload = %v, want %v", pkt[12:], nalu)
	}
}

func TestBuildRTPPacket_MarkerBitOff(t *testing.T) {
	pkt := buildRTPPacket(PayloadTypeH264, 0, 0, 0, false, []byte{0x61})
	if pkt[1]&0x80 != 0 {
		t.Errorf("marker bit should be 0, byte1=%#x", pkt[1])
	}
	if pkt[1]&0x7f != PayloadTypeH264 {
		t.Errorf("payload type = %d, want %d", pkt[1]&0x7f, PayloadTypeH264)
	}
}

func TestMsToRTPTimestamp(t *testing.T) {
	// 90kHz = 90 ticks per ms: 1000ms (1s) must give exactly 90_000 ticks.
	if got := msToRTPTimestamp(1000); got != 90_000 {
		t.Errorf("msToRTPTimestamp(1000) = %d, want 90000", got)
	}
	if got := msToRTPTimestamp(0); got != 0 {
		t.Errorf("msToRTPTimestamp(0) = %d, want 0", got)
	}
}

func TestLengthPrefix(t *testing.T) {
	pkt := []byte{1, 2, 3, 4, 5}
	out := lengthPrefix(pkt)
	if len(out) != 2+len(pkt) {
		t.Fatalf("length = %d, want %d", len(out), 2+len(pkt))
	}
	if got := binary.BigEndian.Uint16(out[:2]); got != uint16(len(pkt)) {
		t.Errorf("length prefix = %d, want %d", got, len(pkt))
	}
	if string(out[2:]) != string(pkt) {
		t.Errorf("payload after the prefix does not match")
	}
}

func TestSplitAnnexBNALUs_ThreeByteStartCodes(t *testing.T) {
	data := []byte{0, 0, 1, 0x67, 0xAA, 0, 0, 1, 0x68, 0xBB, 0xCC}
	nalus := splitAnnexBNALUs(data)
	if len(nalus) != 2 {
		t.Fatalf("expected 2 NALUs, got %d", len(nalus))
	}
	if string(nalus[0]) != string([]byte{0x67, 0xAA}) {
		t.Errorf("nalu[0] = %v, want [67 AA]", nalus[0])
	}
	if string(nalus[1]) != string([]byte{0x68, 0xBB, 0xCC}) {
		t.Errorf("nalu[1] = %v, want [68 BB CC]", nalus[1])
	}
}

func TestSplitAnnexBNALUs_FourByteStartCodes(t *testing.T) {
	data := []byte{0, 0, 0, 1, 0x67, 0xAA, 0, 0, 0, 1, 0x65, 0xBB}
	nalus := splitAnnexBNALUs(data)
	if len(nalus) != 2 {
		t.Fatalf("expected 2 NALUs, got %d", len(nalus))
	}
	if string(nalus[0]) != string([]byte{0x67, 0xAA}) {
		t.Errorf("nalu[0] = %v", nalus[0])
	}
	if string(nalus[1]) != string([]byte{0x65, 0xBB}) {
		t.Errorf("nalu[1] = %v", nalus[1])
	}
}

func TestSplitAnnexBNALUs_MixedStartCodeLengths(t *testing.T) {
	data := []byte{0, 0, 0, 1, 0x67, 0xAA, 0, 0, 1, 0x68, 0xBB}
	nalus := splitAnnexBNALUs(data)
	if len(nalus) != 2 {
		t.Fatalf("expected 2 NALUs (mixed 3- and 4-byte start codes), got %d", len(nalus))
	}
}

func TestSplitAnnexBNALUs_NoStartCode_TreatsWholeBufferAsOneNALU(t *testing.T) {
	data := []byte{0x67, 0xAA, 0xBB, 0xCC}
	nalus := splitAnnexBNALUs(data)
	if len(nalus) != 1 {
		t.Fatalf("expected 1 NALU (no start code), got %d", len(nalus))
	}
	if string(nalus[0]) != string(data) {
		t.Errorf("nalu[0] = %v, want %v", nalus[0], data)
	}
}

func TestSplitAnnexBNALUs_Empty(t *testing.T) {
	if nalus := splitAnnexBNALUs(nil); len(nalus) != 0 {
		t.Errorf("expected 0 NALUs for empty input, got %d", len(nalus))
	}
}

func TestRTPStream_FrameToRTP_MarkerOnlyOnLastNALU(t *testing.T) {
	s, err := NewRTPStream(PayloadTypeH264)
	if err != nil {
		t.Fatalf("NewRTPStream: %v", err)
	}
	frame := []byte{0, 0, 1, 0x67, 0xAA, 0, 0, 1, 0x68, 0xBB, 0, 0, 1, 0x65, 0xCC, 0xDD}
	out := s.FrameToRTP(frame, 1000)

	pkts := splitFramedPackets(t, out)
	if len(pkts) != 3 {
		t.Fatalf("expected 3 RTP packets (3 NALUs), got %d", len(pkts))
	}
	for i, p := range pkts {
		marker := p[1]&0x80 != 0
		wantMarker := i == len(pkts)-1
		if marker != wantMarker {
			t.Errorf("packet %d: marker=%v, want %v", i, marker, wantMarker)
		}
	}
}

func TestRTPStream_FrameToRTP_SequenceIncrementsAcrossFrames(t *testing.T) {
	s, err := NewRTPStream(PayloadTypeH264)
	if err != nil {
		t.Fatalf("NewRTPStream: %v", err)
	}
	frame1 := []byte{0, 0, 1, 0x67, 0xAA}
	frame2 := []byte{0, 0, 1, 0x65, 0xBB}

	out1 := s.FrameToRTP(frame1, 1000)
	out2 := s.FrameToRTP(frame2, 1040)

	seq1 := binary.BigEndian.Uint16(splitFramedPackets(t, out1)[0][2:4])
	seq2 := binary.BigEndian.Uint16(splitFramedPackets(t, out2)[0][2:4])
	if seq2 != seq1+1 {
		t.Errorf("seq2 = %d, want seq1+1 = %d", seq2, seq1+1)
	}
}

func TestRTPStream_FrameToRTP_SameTimestampWithinFrame(t *testing.T) {
	s, err := NewRTPStream(PayloadTypeH264)
	if err != nil {
		t.Fatalf("NewRTPStream: %v", err)
	}
	frame := []byte{0, 0, 1, 0x67, 0xAA, 0, 0, 1, 0x68, 0xBB}
	out := s.FrameToRTP(frame, 5000)
	pkts := splitFramedPackets(t, out)
	if len(pkts) != 2 {
		t.Fatalf("expected 2 packets, got %d", len(pkts))
	}
	ts0 := binary.BigEndian.Uint32(pkts[0][4:8])
	ts1 := binary.BigEndian.Uint32(pkts[1][4:8])
	if ts0 != ts1 {
		t.Errorf("NALUs of the same frame must share the RTP timestamp: ts0=%d ts1=%d", ts0, ts1)
	}
	if want := msToRTPTimestamp(5000); ts0 != want {
		t.Errorf("timestamp = %d, want %d", ts0, want)
	}
}

func TestFragmentNALU_SmallNALU_SinglePacket(t *testing.T) {
	nalu := []byte{0x67, 0xAA, 0xBB}
	frags := fragmentNALU(nalu)
	if len(frags) != 1 {
		t.Fatalf("expected 1 fragment (fits in one packet), got %d", len(frags))
	}
	if string(frags[0]) != string(nalu) {
		t.Errorf("frags[0] = %v, want %v (unmodified NALU)", frags[0], nalu)
	}
}

func TestFragmentNALU_LargeNALU_FUA(t *testing.T) {
	// NALU type 5 (IDR slice), nri=3, forbidden=0 -> header = 0b0_11_00101 = 0x65
	header := byte(0x65)
	payload := make([]byte, 5000)
	for i := range payload {
		payload[i] = byte(i % 256)
	}
	nalu := append([]byte{header}, payload...)

	frags := fragmentNALU(nalu)
	if len(frags) < 2 {
		t.Fatalf("a %d-byte NALU must be fragmented into more than 1 packet, got %d", len(nalu), len(frags))
	}

	var reassembled []byte
	for i, f := range frags {
		if len(f) < 2 {
			t.Fatalf("fragment %d too short: %d bytes", i, len(f))
		}
		fuIndicator := f[0]
		fuHeader := f[1]

		if fuIndicator&0xE0 != header&0xE0 {
			t.Errorf("fragment %d: FU indicator forbidden/nri = %#x, want %#x", i, fuIndicator&0xE0, header&0xE0)
		}
		if fuIndicator&0x1F != 28 {
			t.Errorf("fragment %d: FU indicator type = %d, want 28 (FU-A)", i, fuIndicator&0x1F)
		}
		if fuHeader&0x1F != header&0x1F {
			t.Errorf("fragment %d: real type in FU header = %d, want %d", i, fuHeader&0x1F, header&0x1F)
		}

		isStart := fuHeader&0x80 != 0
		isEnd := fuHeader&0x40 != 0
		if i == 0 && !isStart {
			t.Errorf("the first fragment must have the S (start) bit set")
		}
		if i != 0 && isStart {
			t.Errorf("fragment %d is not the first but has the S (start) bit set", i)
		}
		if i == len(frags)-1 && !isEnd {
			t.Errorf("the last fragment must have the E (end) bit set")
		}
		if i != len(frags)-1 && isEnd {
			t.Errorf("fragment %d is not the last but has the E (end) bit set", i)
		}
		if len(f) > maxRTPPayloadSize {
			t.Errorf("fragment %d is %d bytes, exceeds maxRTPPayloadSize (%d)", i, len(f), maxRTPPayloadSize)
		}

		reassembled = append(reassembled, f[2:]...)
	}

	if string(reassembled) != string(payload) {
		t.Errorf("the payload reassembled from the fragments does not match the original (len got=%d want=%d)", len(reassembled), len(payload))
	}
}

func TestFragmentNALU_Empty(t *testing.T) {
	if frags := fragmentNALU(nil); frags != nil {
		t.Errorf("expected nil for an empty NALU, got %v", frags)
	}
}

func TestRTPStream_FrameToRTP_LargeNALUStaysUnderMaxPayload(t *testing.T) {
	s, err := NewRTPStream(PayloadTypeH264)
	if err != nil {
		t.Fatalf("NewRTPStream: %v", err)
	}
	bigNALU := append([]byte{0x65}, make([]byte, 70000)...) // like a real reassembled I-frame
	frame := append([]byte{0, 0, 1}, bigNALU...)

	out := s.FrameToRTP(frame, 1000)
	pkts := splitFramedPackets(t, out)
	if len(pkts) < 50 {
		t.Fatalf("a 70KB NALU with packets of at most %d bytes must give many fragments, got %d", maxRTPPayloadSize, len(pkts))
	}
	for i, p := range pkts {
		if len(p)-rtpHeaderSize > maxRTPPayloadSize {
			t.Errorf("packet %d: %d-byte payload exceeds maxRTPPayloadSize", i, len(p)-rtpHeaderSize)
		}
	}
	// The marker must only be on the LAST packet (last NALU, last fragment).
	for i, p := range pkts {
		marker := p[1]&0x80 != 0
		want := i == len(pkts)-1
		if marker != want {
			t.Errorf("packet %d: marker=%v, want %v", i, marker, want)
		}
	}
}

// splitFramedPackets undoes the 2-byte length framing so each RTP packet
// can be inspected individually in tests.
func splitFramedPackets(t *testing.T, data []byte) [][]byte {
	t.Helper()
	var pkts [][]byte
	for len(data) > 0 {
		if len(data) < 2 {
			t.Fatalf("truncated data: %d bytes left, not enough for the length prefix", len(data))
		}
		l := binary.BigEndian.Uint16(data[:2])
		if len(data) < 2+int(l) {
			t.Fatalf("length prefix (%d) exceeds the available bytes (%d)", l, len(data)-2)
		}
		pkts = append(pkts, data[2:2+int(l)])
		data = data[2+int(l):]
	}
	return pkts
}
