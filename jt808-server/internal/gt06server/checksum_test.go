package gt06server

import "testing"

// The standard check value from the CRC catalogue (reveng, CRC-16/X-25) for
// the ASCII string "123456789" is 0x906E. This public test vector confirms
// the implementation is exactly CRC-16/X-25 as the protocol requires.
func TestCRC16X25_StandardCheckValue(t *testing.T) {
	got := crc16X25([]byte("123456789"))
	want := uint16(0x906E)
	if got != want {
		t.Errorf("crc16X25(\"123456789\") = 0x%04X, want 0x%04X", got, want)
	}
}

func TestCRC16X25_EmptyInput(t *testing.T) {
	// init=0xFFFF, xorout=0xFFFF -- with no input they cancel out.
	got := crc16X25(nil)
	if got != 0x0000 {
		t.Errorf("crc16X25(nil) = 0x%04X, want 0x0000", got)
	}
}

func TestCRC16X25_DifferentInputsDifferentChecksums(t *testing.T) {
	a := crc16X25([]byte{0x01, 0x02, 0x03})
	b := crc16X25([]byte{0x01, 0x02, 0x04})
	if a == b {
		t.Error("two different inputs produced the same checksum -- suspicious for such a simple sanity check")
	}
}
