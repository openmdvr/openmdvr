package gt06server

import "errors"

// maxGT06PacketBuffer caps how large a still-INCOMPLETE GT06 packet may grow
// before it is considered corrupt -- same reason and pattern as
// maxFrameBufferSize (jt808server/conn.go) and maxPacketReaderBuffer
// (jt1078bridge/reader.go): without a cap, a sender that declares a large
// length and never finishes sending it would grow the buffer without bound.
// A real GT06 message in the supported subset (login/GPS/heartbeat/alarm)
// never approaches 2048 bytes -- ample margin without letting an extended
// 0x7979 packet (out of scope) hold memory indefinitely.
const maxGT06PacketBuffer = 2048

var errGT06FramingInvalid = errors.New("gt06server: invalid packet framing")

// tryDecodeOne tries to extract ONE complete frame from the start of buf.
//
//   - (frame, rest, true, nil): a complete frame was present.
//   - (nil, buf, false, nil): more bytes are needed (not an error).
//   - (nil, buf, false, err): invalid framing -- the connection must close.
//     Unlike JT808 (0x7e), GT06 has no agreed way to resynchronize in the
//     middle of a corrupt stream, so -- as with JT1078 in
//     jt1078bridge/reader.go -- corrupt data is fatal for THIS connection
//     (not the process: recover() in conn.go contains the damage).
//
// GT06 format (verified against a widely used open-source GT06 decoder):
// 0x7878 header (1-byte length) or 0x7979 (2-byte length) + declared length
// (covers protocol number + body + 2-byte serial + 2-byte CRC, NOT the
// header or trailer) + protocol number + body + serial + CRC-16/X-25 +
// 0x0D 0x0A trailer.
func tryDecodeOne(buf []byte) (frame []byte, rest []byte, ok bool, err error) {
	if len(buf) < 2 {
		return nil, buf, false, nil
	}

	var lenFieldSize int
	switch {
	case buf[0] == 0x78 && buf[1] == 0x78:
		lenFieldSize = 1
	case buf[0] == 0x79 && buf[1] == 0x79:
		lenFieldSize = 2
	default:
		return nil, buf, false, errGT06FramingInvalid
	}

	if len(buf) < 2+lenFieldSize {
		return nil, buf, false, nil
	}

	var bodyLen int
	if lenFieldSize == 1 {
		bodyLen = int(buf[2])
	} else {
		bodyLen = int(buf[2])<<8 | int(buf[3])
	}

	// Minimum real bodyLen: 1 (protocol number) + 0 (some messages carry no
	// payload) + 2 (serial) + 2 (crc) = 5. Anything smaller is impossible for
	// a real GT06 packet -- corrupt framing, not "wait for more bytes".
	if bodyLen < 5 || bodyLen > maxGT06PacketBuffer {
		return nil, buf, false, errGT06FramingInvalid
	}

	total := 2 + lenFieldSize + bodyLen + 2 // header + length field + body + trailer
	if len(buf) < total {
		return nil, buf, false, nil
	}
	if buf[total-2] != 0x0D || buf[total-1] != 0x0A {
		return nil, buf, false, errGT06FramingInvalid
	}

	return buf[:total], buf[total:], true, nil
}

// packetReader accumulates raw bytes from a GT06 connection and yields
// complete frames. Each extracted frame is copied to a new slice before being
// returned -- the internal buffer is reused/reallocated on later calls, so a
// returned frame must not share memory with it.
type packetReader struct {
	buf []byte
}

// Feed appends bytes just read from the socket and returns every frame that
// can now be extracted complete. A non-nil error means genuinely corrupt
// data -- the connection must close.
func (r *packetReader) Feed(data []byte) ([][]byte, error) {
	r.buf = append(r.buf, data...)

	var frames [][]byte
	for {
		frame, rest, ok, err := tryDecodeOne(r.buf)
		if err != nil {
			return frames, err
		}
		if !ok {
			break
		}
		f := make([]byte, len(frame))
		copy(f, frame)
		frames = append(frames, f)
		r.buf = rest
		if len(r.buf) == 0 {
			return frames, nil
		}
	}

	// As in jt1078bridge/reader.go: the cap is checked against the REMAINDER
	// after extracting every complete frame, never against the total just
	// read -- avoids a real false positive (a single socket read can
	// legitimately carry several KB if it contains many complete frames).
	if len(r.buf) > maxGT06PacketBuffer {
		return frames, errors.New("gt06server: incomplete packet exceeds the maximum allowed size")
	}
	return frames, nil
}
