package jt808server

import (
	"testing"

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

	pos, err := toGPSPosition(loc)
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

	pos, err := toGPSPosition(loc)
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

	pos, err := toGPSPosition(loc)
	if err != nil {
		t.Fatalf("toGPSPosition: %v", err)
	}
	if pos.Lat <= 0 || pos.Lon <= 0 {
		t.Errorf("north+east must both be positive, lat=%v lon=%v", pos.Lat, pos.Lon)
	}
}

func TestToGPSPosition_InvalidDateTime(t *testing.T) {
	loc := &model.T0x0200{}
	loc.DateTime = "not-a-date"
	if _, err := toGPSPosition(loc); err == nil {
		t.Fatal("expected an error with an invalid DateTime, got none")
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

	if _, err := toGPSPosition(loc); err == nil {
		t.Fatal("expected an error with out-of-range latitude, got none")
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
