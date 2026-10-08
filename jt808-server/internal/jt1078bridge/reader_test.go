package jt1078bridge

import (
	"encoding/binary"
	"testing"
)

// buildRawJT1078Packet builds the raw bytes of an atomic JT1078 video packet
// (DataType=I, SubcontractType=Atomic) for tests, without going through the
// protocol library (so the test does not depend on its Encode() being right,
// only on our reader decoding real JT1078 bytes).
func buildRawJT1078Packet(body []byte) []byte {
	pkt := make([]byte, 0, 30+len(body))
	pkt = append(pkt, '0', '1', 'c', 'd')
	pkt = append(pkt, 0x81) // V=2,P=0,X=0,CC=1
	pkt = append(pkt, 0xE2) // M=1, PT=98(H264)
	pkt = binary.BigEndian.AppendUint16(pkt, 1)
	pkt = append(pkt, make([]byte, 6)...) // sim BCD (irrelevant for this test)
	pkt = append(pkt, 1)                  // logical channel
	pkt = append(pkt, 0x00)               // dataType=I(0), subcontract=atomic(0)
	pkt = binary.BigEndian.AppendUint64(pkt, 1000)
	pkt = binary.BigEndian.AppendUint16(pkt, 0) // lastIFrameInterval
	pkt = binary.BigEndian.AppendUint16(pkt, 0) // lastFrameInterval
	pkt = binary.BigEndian.AppendUint16(pkt, uint16(len(body)))
	pkt = append(pkt, body...)
	return pkt
}

// TestPacketReader_ManyCompletePacketsInOneRead_NoFalsePositive is the exact
// regression for a bug found in end-to-end testing: a real I-frame is split
// into dozens of JT1078 packets arriving in a single socket read (>8KB of
// legitimate data), and the max-size check ran BEFORE extracting complete
// packets instead of on the remainder -- rejecting ordinary video traffic.
func TestPacketReader_ManyCompletePacketsInOneRead_NoFalsePositive(t *testing.T) {
	var buf []byte
	const numPackets = 20
	const bodySize = 900 // near the real JT1078 maximum (950)
	for i := 0; i < numPackets; i++ {
		buf = append(buf, buildRawJT1078Packet(make([]byte, bodySize))...)
	}
	if len(buf) <= maxPacketReaderBuffer {
		t.Fatalf("the test buffer (%d bytes) must exceed maxPacketReaderBuffer (%d) for the test to be meaningful", len(buf), maxPacketReaderBuffer)
	}

	var r packetReader
	packets, err := r.Feed(buf)
	if err != nil {
		t.Fatalf("Feed must not fail with %d legitimate complete packets in one read: %v", numPackets, err)
	}
	if len(packets) != numPackets {
		t.Fatalf("expected %d extracted packets, got %d", numPackets, len(packets))
	}
	if len(r.buf) != 0 {
		t.Errorf("no remainder should be left after extracting all complete packets, %d bytes left", len(r.buf))
	}
}

func TestPacketReader_IncompletePacketAcrossReads(t *testing.T) {
	full := buildRawJT1078Packet([]byte{1, 2, 3, 4, 5})
	var r packetReader

	// Send the packet split across two socket reads.
	packets, err := r.Feed(full[:10])
	if err != nil {
		t.Fatalf("Feed (partial): %v", err)
	}
	if len(packets) != 0 {
		t.Fatalf("no complete packets expected yet, got %d", len(packets))
	}

	packets, err = r.Feed(full[10:])
	if err != nil {
		t.Fatalf("Feed (rest): %v", err)
	}
	if len(packets) != 1 {
		t.Fatalf("expected 1 complete packet after the second read, got %d", len(packets))
	}
}

// TestPacketReader_StalledOversizedPacket_Rejected confirms the limit STILL
// protects against the case it was meant for: a packet that declares a huge
// DataBodyLen and never finishes arriving.
func TestPacketReader_StalledOversizedPacket_Rejected(t *testing.T) {
	// Header of a packet that declares a 60000-byte body, but only the header
	// + a few body bytes are sent -- it never completes.
	header := buildRawJT1078Packet(nil)
	binary.BigEndian.PutUint16(header[len(header)-2:], 60000) // fix the declared length field

	var r packetReader
	stalledBody := make([]byte, maxPacketReaderBuffer+1000) // never reaches the declared 60000, but exceeds our cap
	_, err := r.Feed(append(header, stalledBody...))
	if err == nil {
		t.Fatal("expected an error for an incomplete packet exceeding the max size, got none")
	}
}
