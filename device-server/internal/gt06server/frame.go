package gt06server

// parsedFrame is a GT06 frame split into its fields, with the CRC checked.
type parsedFrame struct {
	ProtocolNumber byte
	Payload        []byte // between protocol number and serial
	Serial         uint16
	CRCValid       bool
}

// parseFrame splits a frame already delimited by packetReader (which
// guaranteed a valid header/length/trailer via tryDecodeOne) into its fields
// and verifies the CRC. It does not re-validate framing.
func parseFrame(frame []byte) parsedFrame {
	lenFieldSize := 1
	if frame[0] == 0x79 {
		lenFieldSize = 2
	}
	bodyStart := 2 + lenFieldSize
	bodyEnd := len(frame) - 2 // before the 0x0D0A trailer
	body := frame[bodyStart:bodyEnd]
	lengthField := frame[2:bodyStart]

	// body = protocol number (1) + payload (variable) + serial (2) + crc (2)
	// -- tryDecodeOne guarantees len(body) >= 5, so these indices are never
	// negative.
	protocolNumber := body[0]
	crcBytes := body[len(body)-2:]
	serialBytes := body[len(body)-4 : len(body)-2]
	payload := body[1 : len(body)-4]

	gotCRC := uint16(crcBytes[0])<<8 | uint16(crcBytes[1])
	crcInput := make([]byte, 0, len(lengthField)+len(body)-2)
	crcInput = append(crcInput, lengthField...)
	crcInput = append(crcInput, body[:len(body)-2]...)
	wantCRC := crc16X25(crcInput)

	return parsedFrame{
		ProtocolNumber: protocolNumber,
		Payload:        payload,
		Serial:         uint16(serialBytes[0])<<8 | uint16(serialBytes[1]),
		CRCValid:       gotCRC == wantCRC,
	}
}
