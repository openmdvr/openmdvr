package jt808server

import (
	"testing"
	"time"

	"github.com/cuteLittleDevil/go-jt808/protocol/jt808"
	"github.com/cuteLittleDevil/go-jt808/protocol/model"
)

// TestToGPSPosition_WestLongitudeSign is the most important test in this
// file: for a fleet at western longitude / northern latitude, a sign bug here
// would put every vehicle on the wrong side of the planet without anything
// else in the system noticing. StatusSignDetails.East, despite its name, is
// true when the frame bit indicates WEST longitude -- see the comment in
// toGPSPosition.
func TestToGPSPosition_WestLongitudeSign(t *testing.T) {
	loc := &model.T0x0200{}
	loc.Latitude = 32_500_000   // 32.5 degrees
	loc.Longitude = 117_000_000 // 117.0 degrees
	loc.DateTime = "2026-01-15 10:30:00"
	loc.StatusSignDetails.South = false // northern hemisphere
	loc.StatusSignDetails.East = true   // west (despite the field name)

	pos, _, err := toGPSPosition(loc)
	if err != nil {
		t.Fatalf("toGPSPosition: %v", err)
	}
	if pos.Lat <= 0 {
		t.Errorf("latitude should be positive (north), got %v", pos.Lat)
	}
	if pos.Lon >= 0 {
		t.Errorf("longitude should be negative (west, e.g. ~ -117), got %v", pos.Lon)
	}
	if got, want := pos.Lon, -117.0; got != want {
		t.Errorf("longitude = %v, want %v", got, want)
	}
}

func TestToGPSPosition_SouthLatitudeSign(t *testing.T) {
	loc := &model.T0x0200{}
	loc.Latitude = 34_600_000
	loc.Longitude = 58_400_000
	loc.DateTime = "2026-01-15 10:30:00"
	loc.StatusSignDetails.South = true // e.g. Buenos Aires
	loc.StatusSignDetails.East = true

	pos, _, err := toGPSPosition(loc)
	if err != nil {
		t.Fatalf("toGPSPosition: %v", err)
	}
	if pos.Lat >= 0 {
		t.Errorf("latitude should be negative (south), got %v", pos.Lat)
	}
}

func TestToGPSPosition_NorthEastPositive(t *testing.T) {
	loc := &model.T0x0200{}
	loc.Latitude = 39_900_000
	loc.Longitude = 116_400_000
	loc.DateTime = "2026-01-15 10:30:00"
	loc.StatusSignDetails.South = false
	loc.StatusSignDetails.East = false // east

	pos, _, err := toGPSPosition(loc)
	if err != nil {
		t.Fatalf("toGPSPosition: %v", err)
	}
	if pos.Lat <= 0 || pos.Lon <= 0 {
		t.Errorf("north+east must both be positive, lat=%v lon=%v", pos.Lat, pos.Lon)
	}
}

// TestToGPSPosition_UnparseableDateTimeUsesServerTime: issue #39, matching
// GT06. An unparseable date (all-zero BCD, month 13, garbage) must NOT drop
// the whole frame -- the real lat/lon are kept and the timestamp is replaced
// with the server's reception time, with a non-empty reason. Only genuinely
// out-of-range coordinates reject the frame (see RejectsOutOfRangeLatLon).
func TestToGPSPosition_UnparseableDateTimeUsesServerTime(t *testing.T) {
	loc := &model.T0x0200{}
	loc.Latitude = 32_500_000
	loc.Longitude = 117_000_000
	loc.StatusSignDetails.East = true // west
	loc.DateTime = "not-a-date"

	before := time.Now().UTC()
	pos, reason, err := toGPSPosition(loc)
	after := time.Now().UTC()
	if err != nil {
		t.Fatalf("unparseable date should not error, got %v", err)
	}
	if reason == "" {
		t.Fatal("expected a non-empty reason for an unparseable timestamp")
	}
	if pos.Time.Before(before.Add(-time.Second)) || pos.Time.After(after.Add(time.Second)) {
		t.Errorf("stored time %s not substituted with server time (~%s)", pos.Time, before)
	}
	if pos.Lon != -117.0 {
		t.Errorf("longitude = %v, want -117 (real lat/lon must be preserved)", pos.Lon)
	}
}

func TestAlarmsFromSignDetails_MapsSetBitsOnly(t *testing.T) {
	var d model.AlarmSignDetails
	d.EmergencyAlarm = true
	d.CollisionAlarm = true
	// everything else stays false (default)

	events := alarmsFromSignDetails(d)
	if len(events) != 2 {
		t.Fatalf("expected 2 alarms, got %d: %v", len(events), alarmTypeNames(events))
	}

	byType := map[string]string{}
	for _, e := range events {
		byType[e.alarmType] = e.severity
	}
	if sev, ok := byType["emergency"]; !ok || sev != "critical" {
		t.Errorf("emergency: got severity=%q ok=%v, want critical/true", sev, ok)
	}
	if sev, ok := byType["collision_warning"]; !ok || sev != "critical" {
		t.Errorf("collision_warning: got severity=%q ok=%v, want critical/true", sev, ok)
	}
}

func TestAlarmsFromSignDetails_NoneSet(t *testing.T) {
	var d model.AlarmSignDetails
	events := alarmsFromSignDetails(d)
	if len(events) != 0 {
		t.Fatalf("expected 0 alarms, got %d: %v", len(events), alarmTypeNames(events))
	}
}

// TestToGPSPosition_RejectsOutOfRangeLatLon: security review regression.
// gps_positions has a range CHECK; without validating first, a faulty GPS
// made the INSERT fail with no ack to the device, causing indefinite
// retries.
func TestToGPSPosition_RejectsOutOfRangeLatLon(t *testing.T) {
	loc := &model.T0x0200{}
	loc.Latitude = 0xFFFFFFFF // ~4294 degrees, impossible
	loc.Longitude = 50_000_000
	loc.DateTime = "2026-01-15 10:30:00"

	if _, _, err := toGPSPosition(loc); err == nil {
		t.Fatal("expected an error with out-of-range latitude, got none")
	}
}

// TestToGPSPosition_BoundsOutOfRangeTimestamp is the issue #39 regression:
// a JT808 device reporting a time far in the future (a forged/stuck clock)
// or before minValidTime must have the position stored with the server's
// reception time -- never its own -- so it cannot create a permanent fake
// "last position" or explode TimescaleDB chunks in the shared gps_positions
// hypertable. The real lat/lon are kept and a non-empty reason is returned.
func TestToGPSPosition_BoundsOutOfRangeTimestamp(t *testing.T) {
	cases := []struct {
		name     string
		dateTime string
	}{
		{"far future (forged clock)", "2255-06-01 12:00:00"},
		{"before lower bound (stuck RTC)", "2007-01-25 08:10:10"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			loc := &model.T0x0200{}
			loc.Latitude = 32_500_000
			loc.Longitude = 117_000_000
			loc.StatusSignDetails.East = true // west
			loc.DateTime = c.dateTime

			before := time.Now().UTC()
			pos, reason, err := toGPSPosition(loc)
			after := time.Now().UTC()
			if err != nil {
				t.Fatalf("toGPSPosition: %v", err)
			}
			if reason == "" {
				t.Fatal("expected a non-empty reason for an out-of-range timestamp")
			}
			// Stored time must be the server's "now", not the device's.
			if pos.Time.Before(before.Add(-time.Second)) || pos.Time.After(after.Add(time.Second)) {
				t.Errorf("stored time %s not substituted with server time (~%s)", pos.Time, before)
			}
			// The real position is still kept.
			if pos.Lon != -117.0 {
				t.Errorf("longitude = %v, want -117 (real lat/lon must be preserved)", pos.Lon)
			}
		})
	}
}

// TestToGPSPosition_KeepsValidTimestamp confirms an in-window device time is
// stored as-is (converted to UTC from the JT/T 808 GMT+8 field), with no
// correction reason -- the bound must not touch normal traffic. The
// near-future case locks in that the 24h tolerance does not over-correct a
// device clock that is slightly ahead.
func TestToGPSPosition_KeepsValidTimestamp(t *testing.T) {
	cases := []struct {
		name   string
		offset time.Duration
	}{
		{"recent past", -time.Hour},
		{"near future within 24h tolerance", 12 * time.Hour},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			loc := &model.T0x0200{}
			loc.Latitude = 32_500_000
			loc.Longitude = 117_000_000
			loc.StatusSignDetails.East = true
			want := time.Now().UTC().Add(c.offset).Truncate(time.Second)
			loc.DateTime = want.In(cstOffset).Format("2006-01-02 15:04:05")

			pos, reason, err := toGPSPosition(loc)
			if err != nil {
				t.Fatalf("toGPSPosition: %v", err)
			}
			if reason != "" {
				t.Errorf("valid timestamp must not be corrected, got reason %q", reason)
			}
			if !pos.Time.Equal(want) {
				t.Errorf("stored time = %s, want %s (valid device time kept, converted to UTC)", pos.Time, want)
			}
		})
	}
}

// TestSafeParseLocation_RecoversFromMalformedAddition reproduces EXACTLY the
// panic a security review found in go-jt808's additional-info parsing: id
// 0x11 with declared length 1 and a first byte != 0 makes the library read a
// uint32 (4 bytes) from a 1-byte slice. safeParseLocation must turn this into
// a normal error, not let the panic propagate.
func TestSafeParseLocation_RecoversFromMalformedAddition(t *testing.T) {
	locationItem := make([]byte, 28) // zeros: AlarmSign/StatusSign/Lat/Lon/Alt/Speed/Dir/DateTime(bcd)
	malformedAddition := []byte{0x11, 0x01, 0x01}
	body := append(locationItem, malformedAddition...)

	jtMsg := jt808.NewJTMessage()
	jtMsg.Body = body

	loc := &model.T0x0200{}
	err := safeParseLocation(loc, jtMsg)
	if err == nil {
		t.Fatal("expected safeParseLocation to recover the panic and return an error, got none")
	}
}

// TestDeviceStatusFromLocation_ElectricityIsInverted: the library's
// Electricity is "0-normal 1-disconnected", so power_connected must be the
// NEGATION of that field -- a sign error here would show "power disconnected"
// exactly when it is connected, and vice versa. ACC needs no inversion (it is
// already "0-off 1-on").
func TestDeviceStatusFromLocation_ElectricityIsInverted(t *testing.T) {
	cases := []struct {
		name           string
		acc            bool
		electricityCut bool
		wantIgnition   bool
		wantPower      bool
	}{
		{"all normal, ignition off", false, false, false, true},
		{"ignition on, power normal", true, false, true, true},
		{"ignition off, power cut", false, true, false, false},
		{"ignition on, power cut", true, true, true, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			loc := &model.T0x0200{}
			loc.StatusSignDetails.ACC = c.acc
			loc.StatusSignDetails.Electricity = c.electricityCut
			gotIgnition, gotPower := deviceStatusFromLocation(loc)
			if gotIgnition != c.wantIgnition {
				t.Errorf("ignitionOn = %v, want %v", gotIgnition, c.wantIgnition)
			}
			if gotPower != c.wantPower {
				t.Errorf("powerConnected = %v, want %v", gotPower, c.wantPower)
			}
		})
	}
}
