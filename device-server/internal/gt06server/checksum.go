package gt06server

// crc16X25 computes the CRC-16/X-25 checksum (a.k.a. CRC-16/IBM-SDLC) used
// by GT06, as confirmed against a widely used open-source GT06 decoder.
// Parameters: poly=0x1021, init=0xFFFF, reflected input and output,
// xorout=0xFFFF. Implemented bitwise (no table: GT06 packets are ~30-260
// bytes) with the pre-reflected polynomial (0x8408).
//
// Covers the length byte through the serial number (never the 0x7878/0x7979
// header, the CRC itself, or the 0x0D0A trailer).
func crc16X25(data []byte) uint16 {
	crc := uint16(0xFFFF)
	for _, b := range data {
		crc ^= uint16(b)
		for i := 0; i < 8; i++ {
			if crc&1 != 0 {
				crc = (crc >> 1) ^ 0x8408
			} else {
				crc >>= 1
			}
		}
	}
	return ^crc
}
