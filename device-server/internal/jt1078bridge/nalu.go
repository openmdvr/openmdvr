package jt1078bridge

// splitAnnexBNALUs splits an Annex-B buffer (NALUs delimited by 0x000001 or
// 0x00000001 start codes) into individual NALUs, WITHOUT the start code -- an
// RTP NALU payload never includes the start code, which is purely a
// byte-stream delimiter (RFC 6184 §1.3).
//
// Why this is needed instead of treating each reassembled JT1078 fragment as
// one NALU: an I-frame (keyframe) typically packs several NALUs together
// (SPS + PPS + IDR slice), and MDVR vendors differ on whether they split that
// into several JT1078 packets or send it together in Annex-B format inside a
// single reassembled frame. Scanning for start codes is robust to both.
func splitAnnexBNALUs(data []byte) [][]byte {
	starts := findStartCodes(data)
	if len(starts) == 0 {
		// No start codes: not Annex-B (some vendors send the "bare" NALU).
		// Treat the whole buffer as a single NALU instead of discarding it.
		if len(data) == 0 {
			return nil
		}
		return [][]byte{data}
	}

	var naluBoundaries []int
	for _, s := range starts {
		naluBoundaries = append(naluBoundaries, s.naluStart)
	}

	var nalus [][]byte
	for i, start := range naluBoundaries {
		end := len(data)
		if i+1 < len(naluBoundaries) {
			end = starts[i+1].codeStart
		}
		if start < end {
			nalus = append(nalus, data[start:end])
		}
	}
	return nalus
}

type startCode struct {
	codeStart int // where the start code begins (00 00 [00] 01)
	naluStart int // where the NALU begins (right after the start code)
}

func findStartCodes(data []byte) []startCode {
	var out []startCode
	i := 0
	for i+2 < len(data) {
		if data[i] == 0 && data[i+1] == 0 {
			if data[i+2] == 1 {
				out = append(out, startCode{codeStart: i, naluStart: i + 3})
				i += 3
				continue
			}
			if i+3 < len(data) && data[i+2] == 0 && data[i+3] == 1 {
				out = append(out, startCode{codeStart: i, naluStart: i + 4})
				i += 4
				continue
			}
		}
		i++
	}
	return out
}
