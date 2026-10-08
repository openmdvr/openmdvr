package jt808server

import (
	"fmt"
	"github.com/cuteLittleDevil/go-jt808/shared/consts"
	"time"

	"github.com/cuteLittleDevil/go-jt808/protocol/jt808"
	"github.com/cuteLittleDevil/go-jt808/protocol/model"

	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
)

// safeParseLocation wraps loc.Parse(jtMsg) with its own recover().
//
// A security review found that go-jt808's parsing of 0x0200 additional info
// has exploitable out-of-range reads with malformed but otherwise
// structurally valid fields (e.g. id 0x11 with declared length 1, or id 0x31
// with length 0) -- a real device with a firmware quirk could trigger this
// repeatedly and without malice. The per-connection recover() in conn.go
// already keeps this from bringing the process down, but it closed the whole
// connection over a single malformed message, which for a real device means a
// reconnect loop losing telemetry instead of just dropping THAT message. This
// narrower recover() turns the panic into a normal parse error -- same
// treatment as any other unreadable 0x0200, and the connection stays alive.
func safeParseLocation(loc *model.T0x0200, jtMsg *jt808.JTMessage) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("panic parsing 0x0200 (malformed additional info): %v", r)
		}
	}()
	return loc.Parse(jtMsg)
}

// toGPSPosition converts an already parsed T0x0200 into the fields we persist.
//
// Latitude/longitude sign: StatusSignDetails.East, despite its name, is true
// when the bit indicates WEST longitude (see the library comment: "0-east
// 1-west"), and South is true for southern latitude. Confirmed by reading the
// library's bit parsing before trusting the field name -- a sign error here
// would put every vehicle in the wrong hemisphere (critical for fleets in the
// Americas, which are at western longitudes).
func toGPSPosition(loc *model.T0x0200) (db.GPSPosition, error) {
	t, err := time.ParseInLocation("2006-01-02 15:04:05", loc.DateTime, cstOffset)
	if err != nil {
		return db.GPSPosition{}, fmt.Errorf("invalid location date %q: %w", loc.DateTime, err)
	}

	lat := float64(loc.Latitude) / 1e6
	if loc.StatusSignDetails.South {
		lat = -lat
	}
	lon := float64(loc.Longitude) / 1e6
	if loc.StatusSignDetails.East { // true == west, see comment above
		lon = -lon
	}

	// Validate the range BEFORE reaching Postgres: gps_positions has a CHECK
	// (lat BETWEEN -90 AND 90, lon BETWEEN -180 AND 180). Without this check,
	// a device with a faulty GPS (Latitude/Longitude are uint32, up to ~4294
	// degrees) causes a CHECK violation, the transaction fails, and the
	// terminal used to get no ack and retry the same frame indefinitely
	// (security review finding). Now it gets "message error" (2), which is
	// correct: the data is bad, there is nothing to retry.
	if lat < -90 || lat > 90 || lon < -180 || lon > 180 {
		return db.GPSPosition{}, fmt.Errorf("lat/lon out of range: lat=%v lon=%v", lat, lon)
	}

	speedKmh := float32(loc.Speed) / 10
	heading := float32(loc.Direction)
	altitude := float32(loc.Altitude)

	return db.GPSPosition{
		Time:     t.UTC(),
		Lat:      lat,
		Lon:      lon,
		SpeedKmh: &speedKmh,
		Heading:  &heading,
		Altitude: &altitude,
	}, nil
}

// deviceStatusFromLocation extracts ignition (ACC) and external power from
// the STATUS field of a 0x0200 -- ALREADY decoded by go-jt808 as a standard
// part of JT/T 808-2019 (not a vendor extension, unlike most GT06 status
// bits). The library's Electricity is "0-normal 1-disconnected" (see its
// comment in t_0x0200_location_item.go); it is inverted into
// "power_connected" so the column reads positively (true = power OK),
// consistent with the project's other boolean columns.
func deviceStatusFromLocation(loc *model.T0x0200) (ignitionOn, powerConnected bool) {
	return loc.StatusSignDetails.ACC, !loc.StatusSignDetails.Electricity
}

type alarmEvent struct {
	alarmType string
	severity  string
}

// alarmsFromSignDetails translates the standard JT/T 808-2019 alarm bits
// (already decoded by the library) into `alarms` rows. It covers the full
// standard bit set, including basic ADAS/DMS ones (collision, lane change,
// blind spot, tire pressure). The vendor-specific 0x64/0x65 additional info
// (distance/speed of the vehicle ahead, deviation type, etc.) is deferred
// until there is a real device to validate exactly what payload that vendor
// sends -- the standard leaves those IDs open to each vendor's
// interpretation.
func alarmsFromSignDetails(d model.AlarmSignDetails) []alarmEvent {
	var events []alarmEvent
	add := func(present bool, alarmType, severity string) {
		if present {
			events = append(events, alarmEvent{alarmType: alarmType, severity: severity})
		}
	}

	add(d.EmergencyAlarm, "emergency", "critical")
	add(d.OverSpeed, "over_speed", "warning")
	add(d.FatigueDriving, "fatigue_driving", "warning")
	add(d.DangerousAlarm, "dangerous_driving", "critical")
	add(d.GNSSModuleFault, "gnss_module_fault", "warning")
	add(d.GNSSAntennaFault, "gnss_antenna_fault", "warning")
	add(d.GNSSAntennaShortCircuit, "gnss_antenna_short_circuit", "warning")
	add(d.TerminalPowerSupply, "terminal_power_undervoltage", "warning")
	add(d.TerminalPowerSupplyShutdown, "terminal_power_shutdown", "critical")
	add(d.TerminalLCDFault, "terminal_lcd_fault", "info")
	add(d.TTSModuleFault, "tts_module_fault", "info")
	add(d.CameraFault, "camera_fault", "warning")
	add(d.ICCardModuleFault, "ic_card_module_fault", "info")
	add(d.OverSpeedAlarm, "over_speed_warning", "warning")
	add(d.FatigueDrivingAlarm, "fatigue_driving_warning", "warning")
	add(d.ViolationDrivingAlarm, "violation_driving_warning", "warning")
	add(d.TirePressureAlarm, "tire_pressure_warning", "warning")
	add(d.RightTurnBlindAreaAlarm, "right_turn_blind_area_warning", "warning")
	add(d.DrivingTimeout, "daily_driving_timeout", "warning")
	add(d.OverTimeStop, "overtime_parking", "info")
	add(d.InOutArea, "area_in_out", "info")
	add(d.InOutLine, "route_in_out", "info")
	add(d.SectionDrivingTime, "section_driving_time_abnormal", "warning")
	add(d.LineDeviation, "route_deviation", "warning")
	add(d.VSSFault, "vss_fault", "warning")
	add(d.OilLevelAbnormality, "fuel_level_abnormal", "warning")
	add(d.StealCar, "vehicle_theft", "critical")
	add(d.LaneDeviation, "illegal_ignition", "critical")
	add(d.LaneOffset, "illegal_displacement", "critical")
	add(d.CollisionAlarm, "collision_warning", "critical")
	add(d.SideSlipAlarm, "rollover_warning", "critical")
	add(d.LaneOpeningAlarm, "illegal_door_open", "warning")

	return events
}

func alarmTypeNames(events []alarmEvent) []string {
	names := make([]string, len(events))
	for i, e := range events {
		names[i] = e.alarmType
	}
	return names
}

// gnssSatellites returns the fix's satellite count (0x0200 additional 0x31),
// or -1 if the terminal does not report it (optional in the standard).
func gnssSatellites(loc *model.T0x0200) int {
	if a, ok := loc.Additions[consts.A0x31GNSSPositionNum]; ok {
		return int(a.Content.GNSSPositionNum)
	}
	return -1
}
