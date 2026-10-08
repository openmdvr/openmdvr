package gt06server

// encodeFrame builds a complete GT06 frame (short 0x7878 header -- the
// server's ACKs are always small, so the extended 0x7979 header is never
// needed) from protocolNumber + payload + serial. Used both for real ACKs
// (handlers.go) and by tests to build synthetic input frames, so the
// framing/CRC arithmetic lives in one place.
func encodeFrame(protocolNumber byte, payload []byte, serial uint16) []byte {
	body := make([]byte, 0, 1+len(payload)+2)
	body = append(body, protocolNumber)
	body = append(body, payload...)
	body = append(body, byte(serial>>8), byte(serial))

	// The declared length includes the CRC (2 bytes) in addition to
	// protocol number + payload + serial -- see tryDecodeOne (reader.go).
	lengthByte := byte(len(body) + 2)
	crcInput := make([]byte, 0, 1+len(body))
	crcInput = append(crcInput, lengthByte)
	crcInput = append(crcInput, body...)
	crc := crc16X25(crcInput)

	frame := make([]byte, 0, 3+len(body)+2+2)
	frame = append(frame, 0x78, 0x78, lengthByte)
	frame = append(frame, body...)
	frame = append(frame, byte(crc>>8), byte(crc))
	frame = append(frame, 0x0D, 0x0A)
	return frame
}
