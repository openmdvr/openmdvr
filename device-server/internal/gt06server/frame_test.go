package gt06server

import "testing"

func TestParseFrame_RoundTrip(t *testing.T) {
	payload := []byte{0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08}
	f := encodeFrame(0x01, payload, 42)

	pf := parseFrame(f)
	if pf.ProtocolNumber != 0x01 {
		t.Errorf("ProtocolNumber = 0x%02X, want 0x01", pf.ProtocolNumber)
	}
	if pf.Serial != 42 {
		t.Errorf("Serial = %d, want 42", pf.Serial)
	}
	if string(pf.Payload) != string(payload) {
		t.Errorf("Payload = %v, want %v", pf.Payload, payload)
	}
	if !pf.CRCValid {
		t.Error("CRCValid = false, want true for a freshly encoded frame")
	}
}

func TestParseFrame_EmptyPayload(t *testing.T) {
	f := encodeFrame(0x13, nil, 7)
	pf := parseFrame(f)
	if len(pf.Payload) != 0 {
		t.Errorf("Payload = %v, want empty", pf.Payload)
	}
	if !pf.CRCValid {
		t.Error("CRCValid = false, want true")
	}
}

func TestParseFrame_CorruptedCRCDetected(t *testing.T) {
	f := encodeFrame(0x13, []byte{0xAA}, 1)
	// Corrupt a payload byte WITHOUT touching the computed CRC -- framing
	// (length/trailer) stays valid (packetReader would accept it), but the
	// CRC no longer matches the content.
	payloadIdx := 2 + 1 + 1 // header(2) + lenfield(1) + protocol(1) -> first payload byte
	f[payloadIdx] ^= 0xFF

	pf := parseFrame(f)
	if pf.CRCValid {
		t.Error("CRCValid = true after corrupting the payload, want false")
	}
}
