package gt06server

import (
	"context"
	"testing"
	"time"
)

// These tests cover the pure parsing functions (no database) -- same pattern
// as jt808server/location_test.go (TestToGPSPosition_*). The paths that do
// call Postgres (handleLogin/handlePosition/handleAlarm/handleHeartbeat end
// to end) are verified against a real Docker stack with the GT06 simulator
// (see testclient/), not here.

func TestParseIMEI_Valid(t *testing.T) {
	// IMEI 123456789012345 -> 0x01 0x23 0x45 0x67 0x89 0x01 0x23 0x45,
	// verified against the exact example in the primary specification
	// (5.1.1.4, Wanway S20).
	payload := []byte{0x01, 0x23, 0x45, 0x67, 0x89, 0x01, 0x23, 0x45}
	imei, ok := parseIMEI(payload)
	if !ok {
		t.Fatal("parseIMEI = false, want true")
	}
	if imei != "123456789012345" {
		t.Errorf("imei = %q, want %q", imei, "123456789012345")
	}
}

func TestParseIMEI_TooShort(t *testing.T) {
	_, ok := parseIMEI([]byte{0x01, 0x23, 0x45})
	if ok {
		t.Error("parseIMEI of a short payload = true, want false")
	}
}

func TestParseIMEI_ExtraBytesIgnored(t *testing.T) {
	// A real login may carry Model Identifier(2)+TimeZone(2) after the IMEI
	// -- parseIMEI must only look at the first 8 bytes.
	payload := []byte{0x01, 0x23, 0x45, 0x67, 0x89, 0x01, 0x23, 0x45, 0x00, 0x00, 0x32, 0x00}
	imei, ok := parseIMEI(payload)
	if !ok || imei != "123456789012345" {
		t.Errorf("parseIMEI with extra bytes = (%q, %v), want (123456789012345, true)", imei, ok)
	}
}

func TestParseGPSBlock_NorthEastValidFix(t *testing.T) {
	// Bit-by-bit example from the primary specification (5.2.1.9): flags
	// 0x15 0x4C = north, east, GPS positioned, course 332 degrees.
	payload := make([]byte, gpsBlockLen)
	// arbitrary valid date/time
	payload[0], payload[1], payload[2] = 24, 1, 15
	payload[3], payload[4], payload[5] = 10, 30, 0
	payload[6] = 0xCC // satellites, unused
	// lat/lon: 22deg32.7658' -> 40582974 -> 0x02 0x6B 0x3F 0x3E (example 5.2.1.6)
	copy(payload[7:11], []byte{0x02, 0x6B, 0x3F, 0x3E})
	copy(payload[11:15], []byte{0x02, 0x6B, 0x3F, 0x3E})
	payload[15] = 0 // speed
	copy(payload[16:18], []byte{0x15, 0x4C})

	pos, fix, ok, reason := parseGPSBlock(payload)
	if !ok {
		t.Fatalf("parseGPSBlock = false (%s), want true", reason)
	}
	if !fix {
		t.Error("fix = false, want true (BYTE_1 bit4 is set in the example)")
	}
	wantDeg := 40582974.0 / 60.0 / 30000.0
	if pos.Lat <= 0 || pos.Lat-wantDeg > 0.0001 || wantDeg-pos.Lat > 0.0001 {
		t.Errorf("Lat = %v, want ~%v (positive, north)", pos.Lat, wantDeg)
	}
	if pos.Lon <= 0 {
		t.Errorf("Lon = %v, want positive (east)", pos.Lon)
	}
	if pos.Heading == nil || *pos.Heading != 332 {
		t.Errorf("Heading = %v, want 332 (specification example)", pos.Heading)
	}
}

func TestParseGPSBlock_SouthWestNegatesSigns(t *testing.T) {
	payload := make([]byte, gpsBlockLen)
	payload[0], payload[1], payload[2] = 24, 1, 15
	payload[3], payload[4], payload[5] = 10, 30, 0
	copy(payload[7:11], []byte{0x00, 0x00, 0x00, 0x01})
	copy(payload[11:15], []byte{0x00, 0x00, 0x00, 0x01})
	// bit2=0 (south), bit3=1 (west), bit4=1 (fix) -> BYTE_1 = 0b00011000 = 0x18
	copy(payload[16:18], []byte{0x18, 0x00})

	pos, fix, ok, reason := parseGPSBlock(payload)
	if !ok || !fix {
		t.Fatalf("parseGPSBlock = (%v, %v, %s), want (true, true, \"\")", ok, fix, reason)
	}
	if pos.Lat >= 0 {
		t.Errorf("Lat = %v, want negative (south)", pos.Lat)
	}
	if pos.Lon >= 0 {
		t.Errorf("Lon = %v, want negative (west)", pos.Lon)
	}
}

func TestParseGPSBlock_NoFix(t *testing.T) {
	payload := make([]byte, gpsBlockLen)
	payload[0], payload[1], payload[2] = 24, 1, 15
	// bit4 (fix) cleared -> no GPS fix
	copy(payload[16:18], []byte{0x00, 0x00})

	_, fix, ok, reason := parseGPSBlock(payload)
	if !ok {
		t.Fatalf("parseGPSBlock = false (%s), want true (well-formed payload, just without a fix)", reason)
	}
	if fix {
		t.Error("fix = true, want false")
	}
}

func TestParseGPSBlock_FarFutureYearFallsBackToServerTime(t *testing.T) {
	// Security review finding: without a guardrail, payload[0]=0xFF (year
	// 2255) was accepted and stored AS IS, creating future chunks in
	// gps_positions (a hypertable SHARED by all tenants) that
	// enforce_gps_position_retention() never prunes (it deletes
	// time < now() - interval, which never reaches the future) -- it stayed
	// as a fake "last position" forever. Demonstrated live: 120 positions
	// with different years grew the hypertable from 9 to 64 chunks from a
	// single connection.
	//
	// Robustness follow-up with real hardware: dropping the WHOLE block threw
	// away perfectly valid lat/lon when the device RTC was simply not
	// synchronized -- fixed to substitute the server time instead of
	// rejecting, without reopening the security finding (the stored
	// timestamp is still never under device control).
	payload := make([]byte, gpsBlockLen)
	payload[0], payload[1], payload[2] = 0xFF, 1, 1 // year 2255
	copy(payload[16:18], []byte{0x14, 0x00})        // fix=1, north, east

	before := time.Now().UTC()
	pos, _, ok, reason := parseGPSBlock(payload)
	after := time.Now().UTC()
	if !ok {
		t.Fatal("parseGPSBlock with year 2255 = false, want true (fallback to server time)")
	}
	if reason == "" {
		t.Error("empty reason, want a timestamp fallback note")
	}
	if pos.Time.Before(before) || pos.Time.After(after) {
		t.Errorf("pos.Time = %v, want between %v and %v (server time)", pos.Time, before, after)
	}
}

func TestParseGPSBlock_TooFarInPastFallsBackToServerTime(t *testing.T) {
	payload := make([]byte, gpsBlockLen)
	payload[0], payload[1], payload[2] = 5, 1, 1 // year 2005, before minValidTime
	copy(payload[16:18], []byte{0x14, 0x00})

	before := time.Now().UTC()
	pos, _, ok, reason := parseGPSBlock(payload)
	after := time.Now().UTC()
	if !ok {
		t.Fatal("parseGPSBlock with year 2005 = false, want true (fallback to server time)")
	}
	if reason == "" {
		t.Error("empty reason, want a timestamp fallback note")
	}
	if pos.Time.Before(before) || pos.Time.After(after) {
		t.Errorf("pos.Time = %v, want between %v and %v (server time)", pos.Time, before, after)
	}
}

func TestParseGPSBlock_SmallClockDriftAccepted(t *testing.T) {
	// Real clock skew of minutes/hours into the future must not be
	// rejected -- only arbitrary years (maxFutureDrift = 24h).
	now := time.Now().UTC().Add(2 * time.Hour)
	payload := make([]byte, gpsBlockLen)
	payload[0] = byte(now.Year() - 2000)
	payload[1] = byte(now.Month())
	payload[2] = byte(now.Day())
	payload[3] = byte(now.Hour())
	payload[4] = byte(now.Minute())
	payload[5] = byte(now.Second())
	copy(payload[16:18], []byte{0x14, 0x00})

	_, _, ok, reason := parseGPSBlock(payload)
	if !ok {
		t.Error("parseGPSBlock with 2h clock skew = false, want true (within maxFutureDrift)")
	}
	if reason != "" {
		t.Errorf("reason = %q, want empty (within tolerance, must not trigger the fallback)", reason)
	}
}

func TestParseGPSBlock_TooShort(t *testing.T) {
	_, _, ok, _ := parseGPSBlock(make([]byte, gpsBlockLen-1))
	if ok {
		t.Error("parseGPSBlock of a short payload = true, want false")
	}
}

func TestParseGPSBlock_OutOfRangeRejected(t *testing.T) {
	payload := make([]byte, gpsBlockLen)
	payload[0], payload[1], payload[2] = 24, 1, 15
	// Absurdly large raw lat/lon -> outside -90..90/-180..180 even after
	// division -- confirms the sanity guardrail.
	copy(payload[7:11], []byte{0xFF, 0xFF, 0xFF, 0xFF})
	copy(payload[11:15], []byte{0xFF, 0xFF, 0xFF, 0xFF})
	copy(payload[16:18], []byte{0x14, 0x00}) // fix=1, north, east

	_, _, ok, _ := parseGPSBlock(payload)
	if ok {
		t.Error("parseGPSBlock with out-of-range lat/lon = true, want false")
	}
}

func TestAlarmCodeName_KnownCodes(t *testing.T) {
	cases := map[byte]string{
		0x01: "gt06_sos",
		0x02: "gt06_power_cut",
		0x03: "gt06_vibration",
		0x2C: "gt06_collision",
	}
	for code, want := range cases {
		info, ok := alarmCodeName[code]
		if !ok {
			t.Errorf("code 0x%02X not found in alarmCodeName", code)
			continue
		}
		if info.alarmType != want {
			t.Errorf("alarmCodeName[0x%02X] = %q, want %q", code, info.alarmType, want)
		}
	}
}

// TestParseTerminalInfo_KnownBitCombinations covers the four real
// ignition/power combinations against the bit layout verified with real
// JC261 evidence (see parseTerminalInfo) -- bit1=ignition, bit7=power cut. It
// replaces the original bit6/bit0 positions, which real hardware proved
// wrong (bit6 never moved despite real ignition toggles).
func TestParseTerminalInfo_KnownBitCombinations(t *testing.T) {
	cases := []struct {
		name         string
		b            byte
		wantIgnition bool
		wantPower    bool
	}{
		{"all zero: ignition off, power normal", 0x00, false, true},
		{"bit1 (ACC): ignition on", 1 << 1, true, true},
		{"bit7 (oil/power): power cut", 1 << 7, false, false},
		{"bit1+bit7: ignition on and power cut", (1 << 1) | (1 << 7), true, false},
		// Irrelevant bits (armed, charging, redundant alarm type) must not
		// affect the result -- 0x7D has EVERY bit set except bit1 (ACC) and
		// bit7 (oil/power).
		{"irrelevant bits set, no ACC or cut", 0x7D, false, true},
		// REAL samples captured from a JC261 while toggling the ignition
		// several times -- the most direct regression against real hardware.
		{"real sample: 0x04 (ignition off)", 0x04, false, true},
		{"real sample: 0x06 (ignition on)", 0x06, true, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			gotIgnition, gotPower := parseTerminalInfo(c.b)
			if gotIgnition != c.wantIgnition {
				t.Errorf("parseTerminalInfo(0x%02X) ignitionOn = %v, want %v", c.b, gotIgnition, c.wantIgnition)
			}
			if gotPower != c.wantPower {
				t.Errorf("parseTerminalInfo(0x%02X) powerConnected = %v, want %v", c.b, gotPower, c.wantPower)
			}
		})
	}
}

func TestHandleFrame_InvalidCRC_NoResponseNoClose(t *testing.T) {
	f := encodeFrame(protoHeartbeat, nil, 1)
	// f = [0x78,0x78, lenByte, protocol, serialHi, serialLo, crcHi, crcLo, 0x0D, 0x0A]
	// (10 bytes for a heartbeat without payload). Corrupt the serial (index
	// 5, covered by the CRC) without touching header/length/CRC/trailer.
	f[5] ^= 0xFF
	sess := &connSession{}
	resp, closeConn := handleFrame(context.Background(), nil, sess, "test", f, nil)
	if resp != nil {
		t.Errorf("resp = %v, want nil for an invalid CRC", resp)
	}
	if closeConn {
		t.Error("closeConn = true, want false for an invalid CRC (line noise, no close)")
	}
}

func TestHandleFrame_UnsupportedProtocolNumber_Ignored(t *testing.T) {
	f := encodeFrame(0x94, []byte{0x01, 0x02}, 5) // out of v1 scope
	sess := &connSession{}
	resp, closeConn := handleFrame(context.Background(), nil, sess, "test", f, nil)
	if resp != nil {
		t.Errorf("resp = %v, want nil for an unsupported protocol number", resp)
	}
	if closeConn {
		t.Error("closeConn = true, want false for an unsupported protocol number")
	}
}

func TestHandleFrame_UnauthenticatedHeartbeat_Closes(t *testing.T) {
	f := encodeFrame(protoHeartbeat, nil, 1)
	sess := &connSession{} // no prior login
	_, closeConn := handleFrame(context.Background(), nil, sess, "test", f, nil)
	if !closeConn {
		t.Error("closeConn = false, want true for an unauthenticated heartbeat (should never arrive before login)")
	}
}

// Confirmed against real hardware (a physical CY06-2G): the device sends
// 0x12 (position) and 0x16 (alarm) -- the "classic" Concox numbering, not the
// Wanway S20 one (0x22/0x26) originally verified against the printed manual.
// handleFrame must recognize BOTH numberings.

func TestHandleFrame_ClassicPositionNumber_Recognized(t *testing.T) {
	// Unauthenticated sess -- enough to confirm dispatch does NOT fall into
	// the "unsupported protocol number" branch (which neither closes nor
	// authenticates); closeConn=true here confirms it DID enter
	// handlePosition (which requires authentication), not the silent default.
	payload := make([]byte, gpsBlockLen)
	f := encodeFrame(protoPositionClassic, payload, 1)
	sess := &connSession{}
	_, closeConn := handleFrame(context.Background(), nil, sess, "test", f, nil)
	if !closeConn {
		t.Error("closeConn = false, want true -- an unauthenticated 0x12 must go through handlePosition (closes), not stay silent as unsupported")
	}
}

func TestHandleFrame_ClassicAlarmNumber_Recognized(t *testing.T) {
	payload := make([]byte, gpsBlockLen+alarmTrailerLen)
	f := encodeFrame(protoAlarmClassic, payload, 1)
	sess := &connSession{}
	_, closeConn := handleFrame(context.Background(), nil, sess, "test", f, nil)
	if !closeConn {
		t.Error("closeConn = false, want true -- an unauthenticated 0x16 must go through handleAlarm (closes), not stay silent as unsupported")
	}
}

// TestHandleFrame_VideoEventReportNumber_Recognized confirms 0x95 (see
// protoVideoEventReport) no longer falls into the silent default -- before,
// EVERY JC261 camera event was dropped without recording any alarm.
func TestHandleFrame_VideoEventReportNumber_Recognized(t *testing.T) {
	f := encodeFrame(protoVideoEventReport, []byte("EVENT_490154203237518_00000000_2026_09_16_08_10_10_F_23.ts"), 1)
	sess := &connSession{} // no prior login
	_, closeConn := handleFrame(context.Background(), nil, sess, "test", f, nil)
	if !closeConn {
		t.Error("closeConn = false, want true -- an unauthenticated 0x95 must go through handleVideoEventReport (closes), not stay silent as unsupported")
	}
}

// TestHandleFrame_VideoEventReportNumber_UnknownPayload_Ignored confirms a
// payload without any recognizable "EVENT_...ts" is ignored without touching
// the database or closing the connection (the format is never guessed) --
// unlike the unauthenticated case above, sess IS authenticated here, so
// without this behavior the test would hit Postgres.
func TestHandleFrame_VideoEventReportNumber_UnknownPayload_Ignored(t *testing.T) {
	f := encodeFrame(protoVideoEventReport, []byte{0xde, 0xad, 0xbe, 0xef}, 1)
	sess := &connSession{Authenticated: true}
	resp, closeConn := handleVideoEventReport(context.Background(), nil, sess, "test", parseFrame(f), nil)
	if resp == nil {
		t.Error("resp = nil, want the ACK -- a payload without a recognized match must still echo the protocol number")
	}
	if closeConn {
		t.Error("closeConn = true, want false for an unrecognized camera event payload")
	}
}

func mustParseTime(t *testing.T, layout, value string) time.Time {
	t.Helper()
	parsed, err := time.Parse(layout, value)
	if err != nil {
		t.Fatalf("time.Parse(%q, %q): %v", layout, value, err)
	}
	return parsed
}

func TestParseVideoEventGroups_TwoChannelsSameEvent_OneGroup(t *testing.T) {
	now := mustParseTime(t, time.RFC3339, "2026-09-16T08:11:00Z")
	payload := "EVENT_490154203237518_00000000_2026_09_16_08_10_10_I_24.ts,EVENT_490154203237518_00000000_2026_09_16_08_10_10_F_23.ts"
	groups := parseVideoEventGroups([]byte(payload), now)
	if len(groups) != 1 {
		t.Fatalf("len(groups) = %d, want 1 -- two cameras of the SAME event must be grouped into a single alarm", len(groups))
	}
	g := groups[0]
	if g.imei != "490154203237518" {
		t.Errorf("imei = %q, want %q", g.imei, "490154203237518")
	}
	if len(g.files) != 2 || len(g.channels) != 2 {
		t.Errorf("files=%v channels=%v, want 2 files/channels", g.files, g.channels)
	}
	want := mustParseTime(t, time.RFC3339, "2026-09-16T08:10:10Z")
	if !g.when.Equal(want) {
		t.Errorf("when = %v, want %v", g.when, want)
	}
}

func TestVideoEventGroup_FrontFile_PicksChannelF(t *testing.T) {
	now := mustParseTime(t, time.RFC3339, "2026-09-16T08:11:00Z")
	// Deliberately reversed order (I before F, as the real device sends it)
	// to confirm frontFile() looks up by channel and never assumes index 0.
	payload := "EVENT_490154203237518_00000000_2026_09_16_08_10_10_I_24.ts,EVENT_490154203237518_00000000_2026_09_16_08_10_10_F_23.ts"
	groups := parseVideoEventGroups([]byte(payload), now)
	if len(groups) != 1 {
		t.Fatalf("len(groups) = %d, want 1", len(groups))
	}
	want := "EVENT_490154203237518_00000000_2026_09_16_08_10_10_F_23.ts"
	if got := groups[0].frontFile(); got != want {
		t.Errorf("frontFile() = %q, want %q", got, want)
	}
}

func TestVideoEventGroup_FrontFile_FallsBackToFirstFileWhenNoF(t *testing.T) {
	now := mustParseTime(t, time.RFC3339, "2026-09-16T08:11:00Z")
	payload := "EVENT_490154203237518_00000000_2026_09_16_08_10_10_I_24.ts"
	groups := parseVideoEventGroups([]byte(payload), now)
	if len(groups) != 1 {
		t.Fatalf("len(groups) = %d, want 1", len(groups))
	}
	want := "EVENT_490154203237518_00000000_2026_09_16_08_10_10_I_24.ts"
	if got := groups[0].frontFile(); got != want {
		t.Errorf("frontFile() = %q, want %q -- without an F channel it must fall back to the first file instead of requesting nothing", got, want)
	}
}

func TestVideoEventGroup_CabinFile_PicksChannelI(t *testing.T) {
	now := mustParseTime(t, time.RFC3339, "2026-09-16T08:11:00Z")
	payload := "EVENT_490154203237518_00000000_2026_09_16_08_10_10_I_24.ts,EVENT_490154203237518_00000000_2026_09_16_08_10_10_F_23.ts"
	groups := parseVideoEventGroups([]byte(payload), now)
	if len(groups) != 1 {
		t.Fatalf("len(groups) = %d, want 1", len(groups))
	}
	want := "EVENT_490154203237518_00000000_2026_09_16_08_10_10_I_24.ts"
	if got := groups[0].cabinFile(); got != want {
		t.Errorf("cabinFile() = %q, want %q", got, want)
	}
}

func TestVideoEventGroup_CabinFile_EmptyWhenNoI(t *testing.T) {
	now := mustParseTime(t, time.RFC3339, "2026-09-16T08:11:00Z")
	payload := "EVENT_490154203237518_00000000_2026_09_16_08_10_10_F_23.ts"
	groups := parseVideoEventGroups([]byte(payload), now)
	if len(groups) != 1 {
		t.Fatalf("len(groups) = %d, want 1", len(groups))
	}
	if got := groups[0].cabinFile(); got != "" {
		t.Errorf("cabinFile() = %q, want \"\" -- unlike frontFile(), it must never fall back to another file", got)
	}
}

func TestParseVideoEventGroups_DifferentTimestamps_SeparateGroups(t *testing.T) {
	now := mustParseTime(t, time.RFC3339, "2026-09-16T08:11:00Z")
	payload := "EVENT_490154203237518_00000000_2026_09_16_08_10_10_F_23.ts,EVENT_490154203237518_00000000_2026_09_16_09_00_00_F_23.ts"
	groups := parseVideoEventGroups([]byte(payload), now)
	if len(groups) != 2 {
		t.Fatalf("len(groups) = %d, want 2 -- two events with different timestamps must never merge", len(groups))
	}
}

func TestParseVideoEventGroups_NoMatch_ReturnsNil(t *testing.T) {
	now := time.Now().UTC()
	groups := parseVideoEventGroups([]byte{0xde, 0xad, 0xbe, 0xef}, now)
	if groups != nil {
		t.Errorf("groups = %v, want nil for a payload without any recognizable EVENT_", groups)
	}
}

func TestParseVideoEventGroups_UntrustedFutureTimestamp_UsesNow(t *testing.T) {
	// Same issue as parseGPSBlock (untrusted device RTC) -- a timestamp far
	// in the future is replaced by `now`; the event is never dropped.
	now := mustParseTime(t, time.RFC3339, "2026-09-16T08:11:00Z")
	payload := "EVENT_490154203237518_00000000_2099_01_01_00_00_00_F_23.ts"
	groups := parseVideoEventGroups([]byte(payload), now)
	if len(groups) != 1 {
		t.Fatalf("len(groups) = %d, want 1", len(groups))
	}
	if !groups[0].when.Equal(now) {
		t.Errorf("when = %v, want now (%v) -- a 2099 timestamp must not be trusted", groups[0].when, now)
	}
}

func TestParseVideoEventGroups_UntrustedPastTimestamp_UsesNow(t *testing.T) {
	now := mustParseTime(t, time.RFC3339, "2026-09-16T08:11:00Z")
	payload := "EVENT_490154203237518_00000000_2007_01_25_00_00_00_F_23.ts" // same year as the stuck CY06-2G RTC
	groups := parseVideoEventGroups([]byte(payload), now)
	if len(groups) != 1 {
		t.Fatalf("len(groups) = %d, want 1", len(groups))
	}
	if !groups[0].when.Equal(now) {
		t.Errorf("when = %v, want now (%v) -- a 2007 timestamp must not be trusted", groups[0].when, now)
	}
}
