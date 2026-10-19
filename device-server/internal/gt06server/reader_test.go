package gt06server

import (
	"bytes"
	"testing"
)

func TestPacketReader_SingleCompleteFrame(t *testing.T) {
	f := encodeFrame(0x13, nil, 1) // heartbeat, no payload
	r := &packetReader{}
	frames, err := r.Feed(f)
	if err != nil {
		t.Fatalf("Feed: %v", err)
	}
	if len(frames) != 1 || !bytes.Equal(frames[0], f) {
		t.Fatalf("frames = %v, want [%v]", frames, f)
	}
	if len(r.buf) != 0 {
		t.Errorf("remaining buffer = %d bytes, want 0", len(r.buf))
	}
}

func TestPacketReader_IncompleteAcrossFeeds(t *testing.T) {
	f := encodeFrame(0x01, []byte{1, 2, 3, 4, 5, 6, 7, 8}, 7) // login-shaped
	r := &packetReader{}

	frames, err := r.Feed(f[:5]) // cut in the middle
	if err != nil {
		t.Fatalf("partial Feed: %v", err)
	}
	if len(frames) != 0 {
		t.Fatalf("frames with an incomplete packet = %d, want 0", len(frames))
	}

	frames, err = r.Feed(f[5:])
	if err != nil {
		t.Fatalf("Feed rest: %v", err)
	}
	if len(frames) != 1 || !bytes.Equal(frames[0], f) {
		t.Fatalf("frames after completion = %v, want [%v]", frames, f)
	}
}

func TestPacketReader_ManyCompleteFramesInOneRead_NoFalsePositive(t *testing.T) {
	// Same case that caused a real bug in jt1078bridge/reader.go: a single
	// socket read with MANY complete frames must not trip the "incomplete
	// packet exceeds the maximum" cap -- it applies only to the incomplete
	// REMAINDER, not to the total read.
	var all []byte
	var want [][]byte
	for i := 0; i < 50; i++ {
		f := encodeFrame(0x13, nil, uint16(i))
		all = append(all, f...)
		want = append(want, f)
	}
	r := &packetReader{}
	frames, err := r.Feed(all)
	if err != nil {
		t.Fatalf("Feed: %v", err)
	}
	if len(frames) != len(want) {
		t.Fatalf("frames = %d, want %d", len(frames), len(want))
	}
	for i := range want {
		if !bytes.Equal(frames[i], want[i]) {
			t.Errorf("frame[%d] does not match", i)
		}
	}
	if len(r.buf) != 0 {
		t.Errorf("remaining buffer = %d bytes, want 0", len(r.buf))
	}
}

func TestPacketReader_InvalidHeader_Errors(t *testing.T) {
	r := &packetReader{}
	_, err := r.Feed([]byte{0x00, 0x00, 0x05, 0x01, 0x02, 0x03, 0x04, 0x05, 0x0D, 0x0A})
	if err == nil {
		t.Fatal("expected an error for an invalid header, got nil")
	}
}

func TestPacketReader_BodyLenTooSmall_Errors(t *testing.T) {
	// declared bodyLen 3 (< 5, the real minimum: 1 protocol + 2 serial + 2 crc)
	r := &packetReader{}
	_, err := r.Feed([]byte{0x78, 0x78, 0x03, 0x01, 0x02, 0x03, 0x0D, 0x0A})
	if err == nil {
		t.Fatal("expected an error for bodyLen < 5, got nil")
	}
}

func TestPacketReader_MissingTrailer_Errors(t *testing.T) {
	f := encodeFrame(0x13, nil, 1)
	f[len(f)-2] = 0xFF // corrupt the 0x0D0A trailer
	r := &packetReader{}
	_, err := r.Feed(f)
	if err == nil {
		t.Fatal("expected an error for a corrupt trailer, got nil")
	}
}

func TestPacketReader_OversizedIncompletePacket_Rejected(t *testing.T) {
	// Declares a large length (0x79 0x79, 2-byte length field) and never
	// sends the rest -- it must not wait forever.
	r := &packetReader{}
	huge := []byte{0x79, 0x79, 0xFF, 0xFF} // declared bodyLen = 65535 > maxGT06PacketBuffer
	_, err := r.Feed(huge)
	if err == nil {
		t.Fatal("expected an error for bodyLen > maxGT06PacketBuffer, got nil")
	}
}

func TestPacketReader_ExtendedHeaderLengthField(t *testing.T) {
	// 0x7979 with a bodyLen within the cap -- confirms the 2-byte length
	// field is read correctly (not just the 0x7878 path).
	payload := make([]byte, 50)
	bodyLen := 1 + len(payload) + 2 + 2 // protocol + payload + serial + crc
	body := append([]byte{0x94}, payload...)
	body = append(body, 0x00, 0x09)
	crcInput := append([]byte{byte(bodyLen >> 8), byte(bodyLen)}, body...)
	crc := crc16X25(crcInput)
	frame := []byte{0x79, 0x79, byte(bodyLen >> 8), byte(bodyLen)}
	frame = append(frame, body...)
	frame = append(frame, byte(crc>>8), byte(crc), 0x0D, 0x0A)

	r := &packetReader{}
	frames, err := r.Feed(frame)
	if err != nil {
		t.Fatalf("Feed: %v", err)
	}
	if len(frames) != 1 || !bytes.Equal(frames[0], frame) {
		t.Fatalf("frames = %v, want [%v]", frames, frame)
	}
}
