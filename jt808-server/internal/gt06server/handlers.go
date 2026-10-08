// The byte layout of the GPS block and the alarm block in this file is
// verified field by field against a real primary specification (Wanway S20,
// a documented GT06-compatible variant). PROTOCOL NUMBERS, however,
// deliberately accept TWO numberings -- Wanway S20 (0x22/0x26) AND the
// "classic" Concox numbering (0x12/0x16). Both are needed in practice: the
// first real hardware tested (a CY06-2G) used the classic numbering, not the
// one from the primary specification the byte layout was verified against --
// exactly the vendor fragmentation the GT06 ecosystem is known for. V1 covers
// the core subset (login, position, heartbeat, alarms, command replies,
// camera event reports); any other protocol number is ignored without a
// reply, and its format is never guessed.
package gt06server

import (
	"context"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"regexp"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
	"github.com/openmdvr/openmdvr/jt808-server/internal/gpsfilter"
)

const (
	protoLogin     = 0x01
	protoHeartbeat = 0x13
	protoPosition  = 0x22 // "Positioning data (UTC)" -- Wanway S20 numbering
	protoAlarm     = 0x26 // "Alarm data (UTC)" -- Wanway S20 numbering

	// protoPositionClassic/protoAlarmClassic: "classic" Concox numbering
	// (0x12 GPS+LBS, 0x16 alarm) -- confirmed against real hardware: a
	// physical CY06-2G sends these numbers, NOT the Wanway S20 ones. The GPS
	// block (date+sats+lat+lon+speed+course, 18 bytes) and the alarm block
	// (terminal info+voltage+gsm+alarm/lang, last 5 bytes) have the SAME
	// structure in both numberings -- only the outer protocol number
	// changes -- so they reuse the same handlers without duplicating parsing.
	protoPositionClassic = 0x12
	protoAlarmClassic    = 0x16

	// protoCommand/protoCommandReply: section VI of the Concox protocol
	// document -- 0x80 server->terminal (SMS-style command), 0x15
	// terminal->server (reply). See commands.go for the codec and the
	// Dispatcher that uses them (remote commands).
	protoCommand      = 0x80
	protoCommandReply = 0x15

	// protoCommandReplyJC261: found on real JC261 hardware (RTMP video) --
	// this firmware does NOT answer the 0x80 command with a 0x15 as the
	// protocol document says; it answers with protocol number 0x21 (payload
	// captured from real hardware: `00 00 00 00 01` + ASCII "RTMP:OK!" after
	// sending "RTMP,ON,INOUT#"). As with the two position/alarm numberings,
	// BOTH are supported instead of assuming one, because the real GT06
	// ecosystem is fragmented by vendor/firmware.
	protoCommandReplyJC261 = 0x21

	// protoVideoEventReport: the JC261 spontaneously (without being asked)
	// sends protocol number 0x95 every time it records an event clip
	// (collision/impact/panic button, detected by the device itself, NOT a
	// platform request). Decoded byte by byte from a payload captured on
	// real hardware: a short binary header (no confirmed specification --
	// probably event coordinates/metadata; discovery phase, see
	// handleVideoEventReport) followed by a comma-separated ASCII list with
	// the EXACT names of the files the device recorded, e.g.
	// "EVENT_490154203237518_00000000_2026_09_16_08_10_10_I_24.ts,
	// EVENT_..._F_23.ts" -- I/F are the same two channels the dashboard
	// exposes (Interior/cabin and Front). Without a handler these events
	// fell into the "unsupported, ignored" default below and real alarms
	// were silently discarded.
	protoVideoEventReport = 0x95
)

// videoEventPattern extracts the fields of a JC261 event file name -- see
// protoVideoEventReport. The numeric field after the IMEI (e.g. "00000000")
// may be a firmware-specific event type code, not yet confirmed (one real
// sample is not enough to map it) -- it is logged as is, its meaning is
// never assumed.
// No ^/$ anchors on purpose: the payload carries SEVERAL comma-separated
// names in one string (see protoVideoEventReport). FindAllStringSubmatch
// finds each "EVENT_...ts" occurrence separately without splitting on comma
// first, and ".ts" unambiguously delimits the end of each entry.
var videoEventPattern = regexp.MustCompile(
	`EVENT_(\d{15})_(\d+)_(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_([A-Za-z])_(\d+)\.ts`,
)

var imeiPattern = regexp.MustCompile(`^[0-9]{15}$`)

// handleFrame dispatches a frame already delimited (by packetReader) to the
// right handler. It returns the response frame to send (nil if no reply is
// needed) and whether the connection must close.
func handleFrame(ctx context.Context, pool *pgxpool.Pool, sess *connSession, remote string, raw []byte, clipRequester ClipRequester) (response []byte, closeConn bool) {
	pf := parseFrame(raw)
	if !pf.CRCValid {
		// An invalid CRC on a frame that ALREADY passed framing (valid
		// header/length/trailer) is line noise, not necessarily an attack --
		// it is dropped without replying or closing the connection, like a
		// JT808 frame with an invalid checksum.
		log.Printf("gt06: %s: invalid CRC on protocol=0x%02X, dropped", remote, pf.ProtocolNumber)
		return nil, false
	}

	switch pf.ProtocolNumber {
	case protoLogin:
		return handleLogin(ctx, pool, sess, remote, pf)
	case protoHeartbeat:
		return handleHeartbeat(ctx, pool, sess, remote, pf)
	case protoPosition, protoPositionClassic:
		return handlePosition(ctx, pool, sess, remote, pf)
	case protoAlarm, protoAlarmClassic:
		return handleAlarm(ctx, pool, sess, remote, pf)
	case protoCommandReply, protoCommandReplyJC261:
		handleCommandReply(remote, sess, pf)
		return nil, false
	case protoVideoEventReport:
		return handleVideoEventReport(ctx, pool, sess, remote, pf, clipRequester)
	default:
		// Out of v1 scope (coverage deferred on purpose, see
		// docs/architecture.md). Most of these messages need no server ACK
		// for the device to keep working -- only login/heartbeat/position/
		// alarm do.
		//
		// The payload is logged in hex (not just "ignored") so unknown
		// vendor messages can be decoded from real bytes instead of guessed.
		log.Printf("gt06: %s: unsupported protocol number 0x%02X (out of v1 scope), ignored, payload=%s", remote, pf.ProtocolNumber, hex.EncodeToString(pf.Payload))
		return nil, false
	}
}

// parseIMEI extracts the IMEI from the login payload: 8 bytes that hex-dump
// to 16 characters -- the FIRST is dropped (padding) and the remaining 15 are
// the real IMEI. Verified byte by byte against the primary specification's
// example (5.1.1.4): IMEI 123456789012345 is sent as
// 0x01 0x23 0x45 0x67 0x89 0x01 0x23 0x45. There is no "strip leading zeros"
// gotcha like JT808 BCD (see migration 0026) -- this is a direct hex dump,
// not BCD arithmetic with normalization.
func parseIMEI(payload []byte) (string, bool) {
	if len(payload) < 8 {
		return "", false
	}
	full := hex.EncodeToString(payload[:8]) // 16 hex characters
	imei := full[1:]                        // drop the first padding nibble -> 15
	if !imeiPattern.MatchString(imei) {
		// A nibble outside 0-9 (A-F) yields a letter here -- not a valid
		// IMEI; the same defensive check the API applies on provisioning
		// (schemas.py::_GT06_IMEI_RE).
		return "", false
	}
	return imei, true
}

func handleLogin(ctx context.Context, pool *pgxpool.Pool, sess *connSession, remote string, pf parsedFrame) ([]byte, bool) {
	imei, ok := parseIMEI(pf.Payload)
	if !ok {
		log.Printf("gt06: %s: login with unreadable IMEI, closing connection", remote)
		return nil, true
	}

	var dev db.Device
	err := db.WithBypass(ctx, pool, func(ctx context.Context, tx pgx.Tx) error {
		var lookupErr error
		dev, lookupErr = db.LookupDeviceByIMEI(ctx, tx, imei)
		return lookupErr
	})
	switch {
	case errors.Is(err, db.ErrDeviceNotFound):
		// No ACK, connection closed -- the base spec defines no NACK format
		// with a reason (unlike JT808 0x8100 code 4) and inventing one is not
		// worth it. Provisioning is bypass-only; no hints for someone probing
		// IMEIs blindly.
		//
		// Accepted residual risk (found and confirmed live by a security
		// review): the presence/absence of the ACK is still a binary oracle
		// distinguishing a provisioned+active IMEI from the rest -- there is
		// no way around it without a NACK format the base protocol does not
		// define. Low exploitability: a 15-digit Luhn-checked IMEI is not a
		// secret, and GT06 has no cryptographic auth anyway (same trust model
		// as JT808 -- see the scope note in internal/session/session.go).
		log.Printf("gt06: %s: login rejected (imei not provisioned): imei=%s", remote, imei)
		return nil, true
	case err != nil:
		log.Printf("gt06: %s: error resolving device for login imei=%s: %v", remote, imei, err)
		return nil, true
	case dev.Status != "active":
		log.Printf("gt06: %s: login rejected (device inactive): imei=%s status=%s", remote, imei, dev.Status)
		return nil, true
	}

	sess.IMEI = imei
	sess.DeviceID = dev.ID
	sess.TenantID = dev.TenantID
	sess.Authenticated = true
	log.Printf("gt06: device authenticated: imei=%s device_id=%s tenant_id=%s", imei, dev.ID, dev.TenantID)

	// Login ACK: same protocol number, no content, same serial -- verified
	// against the primary specification (5.1.2, "the protocol number in the
	// response packet is the same as the protocol number of the data packet
	// sent by the terminal").
	return encodeFrame(protoLogin, nil, pf.Serial), false
}

func handleHeartbeat(ctx context.Context, pool *pgxpool.Pool, sess *connSession, remote string, pf parsedFrame) ([]byte, bool) {
	if !sess.Authenticated {
		// A heartbeat before login should not happen with a real device --
		// close the connection instead of continuing without a resolved
		// tenant_id/device_id.
		return nil, true
	}
	err := db.WithBypass(ctx, pool, func(ctx context.Context, tx pgx.Tx) error {
		// Field testing with a real JC261 showed that toggling the ignition
		// produced ZERO alarm frames (0x26/0x16), only 0x13 heartbeats (plus
		// vendor-specific protocol numbers 0x94/0xE0/0xE1, out of v1 scope).
		// Turning the engine on/off is not an ALARM condition -- it makes
		// sense for this hardware to report ACC/power state on the ROUTINE
		// status channel (heartbeat). The GT06 heartbeat payload (5.4) has the
		// SAME Terminal Information Content(1)+Voltage(1)+GSM(1)+Language(2)
		// structure handleAlarm reads (see alarmTrailerLen), WITHOUT any
		// variable-size GPS/LBS block in front (unlike 0x26), so here
		// terminal_info is simply the FIRST byte, not the fifth from the end.
		// Bit layout: see parseTerminalInfo. The raw byte is logged so a
		// firmware with a different layout can be diagnosed from evidence.
		if len(pf.Payload) >= 1 {
			terminalInfoByte := pf.Payload[0]
			ignitionOn, powerConnected := parseTerminalInfo(terminalInfoByte)
			sess.lastIgnition = &ignitionOn
			if err := db.UpdateDeviceStatus(ctx, tx, sess.DeviceID, &ignitionOn, &powerConnected); err != nil {
				return err
			}
			log.Printf("gt06: %s: heartbeat terminal_info=0x%02X ignition=%v power=%v imei=%s", remote, terminalInfoByte, ignitionOn, powerConnected, sess.IMEI)
			return nil
		}
		log.Printf("gt06: %s: heartbeat without payload (len=%d), last_seen only: imei=%s", remote, len(pf.Payload), sess.IMEI)
		return db.TouchLastSeen(ctx, tx, sess.DeviceID)
	})
	if err != nil {
		log.Printf("gt06: %s: error processing heartbeat imei=%s: %v", remote, sess.IMEI, err)
		return nil, true
	}
	// Verified against 5.4.2: same protocol number (0x13), no content.
	return encodeFrame(protoHeartbeat, nil, pf.Serial), false
}

// gpsBlockLen is the fixed size of the GPS block shared by position (0x22)
// and alarm (0x26) messages -- IDENTICAL in both, as the primary
// specification states explicitly (5.3.1: "the alarm packet is composed of
// the terminal information added on the positioning packet, and the coding
// format is also the same"). Layout: date/time(6) + satellites(1) +
// latitude(4) + longitude(4) + speed(1) + course/status(2).
const gpsBlockLen = 6 + 1 + 4 + 4 + 1 + 2

// gpsSatellites returns the satellites used in the fix (low nibble of
// payload[6], 5.2.1.5 of the Concox specification), or -1 if the payload is
// too short.
func gpsSatellites(payload []byte) int {
	if len(payload) < 7 {
		return -1
	}
	return int(payload[6] & 0x0F)
}

// parseGPSBlock decodes the GPS block shared by 0x22 and 0x26 (the first
// gpsBlockLen bytes of the payload). It never touches what follows (LBS:
// MCC/MNC/LAC/CellID) -- variable size between messages and unused by the
// product, deliberately out of v1 scope. ok=false if the payload is shorter
// than gpsBlockLen (truncated/corrupt payload; should not happen with a valid
// CRC, but it is checked before indexing anyway).
func parseGPSBlock(payload []byte) (pos db.GPSPosition, gpsFix bool, ok bool, reason string) {
	if len(payload) < gpsBlockLen {
		return db.GPSPosition{}, false, false, fmt.Sprintf("short payload: %d < %d bytes", len(payload), gpsBlockLen)
	}

	// Date/time: 6 bytes YY MM DD HH MM SS (year relative to 2000) --
	// verified against the primary specification's example (5.2.1.4).
	year := 2000 + int(payload[0])
	month := time.Month(payload[1])
	day := int(payload[2])
	hour := int(payload[3])
	minute := int(payload[4])
	second := int(payload[5])
	// UTC: these two messages' protocol numbers are literally "Positioning
	// data (UTC)"/"Alarm data (UTC)" -- the device already reports in UTC,
	// without the optional time zone offset the login carries (deliberately
	// not parsed in v1, see parseIMEI).
	t := time.Date(year, month, day, hour, minute, second, 0, time.UTC)

	// payload[6]: GPS info length + satellite count (one nibble each,
	// 5.2.1.5) -- read by gpsSatellites for the GPS quality filter (fewer
	// satellites = larger uncertainty radius).

	latRaw := binary.BigEndian.Uint32(payload[7:11])
	lonRaw := binary.BigEndian.Uint32(payload[11:15])
	// Conversion verified against the 5.2.1.6 example: degrees = raw / 60 / 30000.
	lat := float64(latRaw) / 60.0 / 30000.0
	lon := float64(lonRaw) / 60.0 / 30000.0

	speedKmh := float32(payload[15]) // 5.2.1.8: 1 byte, 0-255 km/h directly

	// Course/status: 2 bytes, verified bit by bit against the 5.2.1.9
	// example. BYTE_1 (first byte, high bits of the 16-bit integer):
	// bit2=latitude north/south (1=north), bit3=longitude east/west
	// (1=west), bit4=GPS fix (1=positioned). BYTE_2 + the 2 low bits of
	// BYTE_1 form the 10-bit course (0-360).
	flags := binary.BigEndian.Uint16(payload[16:18])
	course := float32(flags & 0x03FF) // bits 0-9: course
	gpsFix = flags&(1<<12) != 0       // BYTE_1 bit4 -> bit12 of the uint16
	if flags&(1<<10) == 0 {
		lat = -lat // BYTE_1 bit2 -> bit10: 0 = south
	}
	if flags&(1<<11) != 0 {
		lon = -lon // BYTE_1 bit3 -> bit11: 1 = west
	}

	if lat < -90 || lat > 90 || lon < -180 || lon > 180 {
		// Should never happen with well-formed bytes, but gps_positions has
		// an exact CHECK on this range (0007_timeseries_tables.sql) -- better
		// to drop here with clear context than let insert_gps_position()
		// reject it with a generic DB error.
		return db.GPSPosition{}, false, false, fmt.Sprintf(
			"lat/lon out of range: lat=%.6f lon=%.6f latRaw=%d lonRaw=%d flags=%04X blockHex=%s",
			lat, lon, latRaw, lonRaw, flags, hex.EncodeToString(payload[:gpsBlockLen]),
		)
	}

	// Security review finding: unlike lat/lon above, the timestamp had NO
	// guardrail -- payload[0] (year) is an unvalidated raw byte, so a device
	// (untrusted input; GT06 has no cryptographic auth beyond the IMEI) could
	// declare any year between 2000 and 2255. Demonstrated live: 120
	// positions with different years grew gps_positions -- a hypertable
	// SHARED by all tenants -- from 9 to 64 chunks from a single connection,
	// and a future date survives enforce_gps_position_retention()
	// indefinitely (0019_gps_retention.sql deletes `time < now() - interval`,
	// which never reaches the future), also becoming a permanent fake "last
	// position" (queries order by time DESC). maxFutureDrift tolerates real
	// field clock skew without allowing arbitrary years.
	now := time.Now().UTC()
	if t.After(now.Add(maxFutureDrift)) || t.Before(minValidTime) {
		// A ROBUSTNESS finding (not security), confirmed with real hardware:
		// a CY06-2G repeatedly sent gpsFix=1 with the date stuck at
		// 2007-01-25 (RTC never synchronized -- HH:MM:SS advances in real
		// time, only YYYY-MM-DD stays fixed) along with PERFECTLY valid
		// lat/lon (real location, 4-5 satellites). Rejecting the WHOLE block
		// threw away a real position because of a firmware defect unrelated
		// to the GPS fix. Fix: substitute the server's RECEPTION time -- the
		// real lat/lon are kept, and the security finding stays closed
		// because the stored timestamp is NEVER under device control when its
		// own is invalid (always the server's real "now"). ok=true with a
		// non-empty reason -- success with a note, not a rejection; callers
		// log it as an informational warning.
		reason = fmt.Sprintf(
			"untrusted device timestamp, using server time: t=%s now=%s rawYMDHMS=%02d-%02d-%02d %02d:%02d:%02d blockHex=%s",
			t.Format(time.RFC3339), now.Format(time.RFC3339), payload[0], payload[1], payload[2], payload[3], payload[4], payload[5],
			hex.EncodeToString(payload[:gpsBlockLen]),
		)
		t = now
	}

	return db.GPSPosition{Time: t, Lat: lat, Lon: lon, SpeedKmh: &speedKmh, Heading: &course}, gpsFix, true, reason
}

// maxFutureDrift/minValidTime bound the timestamp a GT06 may declare (see
// the finding documented in parseGPSBlock). 24h gives a badly synchronized
// device clock generous margin without allowing arbitrary years; minValidTime
// is a fixed lower bound that closes the same "chunk explosion" problem
// towards the past.
const maxFutureDrift = 24 * time.Hour

var minValidTime = time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)

func handlePosition(ctx context.Context, pool *pgxpool.Pool, sess *connSession, remote string, pf parsedFrame) ([]byte, bool) {
	if !sess.Authenticated {
		return nil, true
	}
	// The primary specification documents no reply for this message (unlike
	// login/heartbeat/alarm) -- none is invented.
	pos, gpsFix, ok, reason := parseGPSBlock(pf.Payload)
	if !ok {
		log.Printf("gt06: %s: 0x%02X dropped (%s): imei=%s len=%d payloadHex=%s", remote, pf.ProtocolNumber, reason, sess.IMEI, len(pf.Payload), hex.EncodeToString(pf.Payload))
		return nil, false
	}
	if reason != "" {
		log.Printf("gt06: %s: 0x%02X %s: imei=%s", remote, pf.ProtocolNumber, reason, sess.IMEI)
	}
	if err := persistPosition(ctx, pool, sess, remote, pos, gpsFix, gpsSatellites(pf.Payload)); err != nil {
		log.Printf("gt06: %s: error storing position imei=%s: %v", remote, sess.IMEI, err)
	}
	return nil, false
}

// persistPosition stores the position if the device has a GPS fix (gpsFix),
// and touches last_seen_at in any case -- the device is alive and talking
// even without a fix, which is already useful for the dashboard ("last
// seen").
func persistPosition(ctx context.Context, pool *pgxpool.Pool, sess *connSession, remote string, pos db.GPSPosition, gpsFix bool, sats int) error {
	return db.WithBypass(ctx, pool, func(ctx context.Context, tx pgx.Tx) error {
		if gpsFix {
			// Goes through the GPS quality filter (parked drift, incoherent
			// jumps, noise speed -- see internal/gpsfilter).
			reason, err := db.InsertGPSPositionFiltered(ctx, tx, sess.TenantID, sess.DeviceID, gpsfilter.Sample{
				Time: pos.Time, Lat: pos.Lat, Lon: pos.Lon, SpeedKmh: pos.SpeedKmh, Heading: pos.Heading,
				Satellites: sats, Ignition: sess.lastIgnition,
			}, pos.Altitude)
			if err != nil {
				return err
			}
			if reason != "" {
				log.Printf("gt06: %s: GPS filter: %s (imei=%s)", remote, reason, sess.IMEI)
			}
		} else {
			log.Printf("gt06: %s: position without a valid GPS fix, not stored (last_seen only): imei=%s", remote, sess.IMEI)
		}
		return db.TouchLastSeen(ctx, tx, sess.DeviceID)
	})
}

// alarmTrailerLen is the fixed size of the status/alarm block following
// GPS+LBS in a 0x26 message (5.3.1.14-.17): terminal information(1) +
// battery level(1) + GSM signal(1) + alarm/language(2) = 5 bytes, ALWAYS the
// last 5 bytes of the payload regardless of the variable-size LBS block
// before them -- so it is indexed from the END of the payload, never from a
// fixed offset after the GPS block (that would require knowing the exact
// MCC/MNC/LAC/CellID size, deliberately out of v1 scope).
const alarmTrailerLen = 1 + 1 + 1 + 2

// alarmCodeName maps the first byte of the "Alarm/Language" field
// (5.3.1.17) to a stable alarm_type for the `alarms` table (free TEXT, no
// fixed catalog -- see infra/postgres/migrations/0007_timeseries_tables.sql).
// This is the COMPLETE set the primary specification documents for this
// field, not an arbitrary selection -- closed and stable because it comes
// from a single byte with fixed semantics, not a model-dependent bit
// combination (unlike 5.3.1.14, see parseTerminalInfo). An unlisted code is
// still recorded under a generic name (the alarm is never dropped), so the
// event is not lost even if the code comes from a different model/firmware.
var alarmCodeName = map[byte]struct {
	alarmType string
	severity  string
}{
	0x01: {"gt06_sos", "critical"},
	0x02: {"gt06_power_cut", "warning"},
	0x03: {"gt06_vibration", "warning"},
	0x04: {"gt06_geofence_enter", "info"},
	0x05: {"gt06_geofence_exit", "info"},
	0x06: {"gt06_overspeed", "warning"},
	0x09: {"gt06_movement", "warning"},
	0x0A: {"gt06_gps_blind_area_enter", "warning"},
	0x0B: {"gt06_gps_blind_area_exit", "warning"},
	0x0C: {"gt06_power_on", "info"},
	0x0E: {"gt06_external_power_low", "warning"},
	0x0F: {"gt06_external_power_low_protection", "warning"},
	0x11: {"gt06_power_off", "warning"},
	0x13: {"gt06_tampering", "critical"},
	0x14: {"gt06_door", "warning"},
	0x15: {"gt06_low_power_shutdown", "warning"},
	0x29: {"gt06_rapid_acceleration", "warning"},
	0x2C: {"gt06_collision", "critical"},
	0x2D: {"gt06_flip", "critical"},
	0x30: {"gt06_harsh_braking", "warning"},
	0x4C: {"gt06_sharp_turn", "warning"},
}

// parseTerminalInfo decodes the "Terminal Information Content" byte
// (5.3.1.14) -- the FIRST of the alarmTrailerLen bytes before
// "Alarm/Language" in every 0x26/0x16 frame (pf.Payload[len-5] in an alarm),
// and pf.Payload[0] in a 0x13 heartbeat (see handleHeartbeat -- same byte,
// no GPS/LBS block in front).
//
// Corrected against real evidence: the publicly documented "standard"
// layout for Concox/GT06 (bit0=oil/power, bit6=ACC) turned out WRONG for a
// real JC261 -- toggling the ignition several times never moved bit6 (always
// 0), while the ONLY bit that changed across the real samples (0x04 -> 0x06,
// exactly bit1) matched the actions taken. This matches the layout used by a
// widely used open-source GT06 decoder, tested against many real devices:
// bit1=IGNITION, bit2=CHARGE, bit7=BLOCKED (oil/power cut), bits3-6=embedded
// alarm code (redundant with the alarm/language byte alarmCodeName already
// translates). Real layout (bit0 = LSB):
//
//	bit0: armed/defense      0=disarmed     1=armed
//	bit1: ACC (ignition)     0=off          1=on
//	bit2: charging           0=no           1=yes
//	bit3-6: alarm type (redundant, not used here)
//	bit7: oil/power          0=normal       1=cut
//
// The raw byte is still logged (see handleAlarm/handleHeartbeat) in case a
// different firmware/model does not match this layout.
func parseTerminalInfo(b byte) (ignitionOn, powerConnected bool) {
	ignitionOn = b&(1<<1) != 0
	powerConnected = b&(1<<7) == 0
	return
}

func handleAlarm(ctx context.Context, pool *pgxpool.Pool, sess *connSession, remote string, pf parsedFrame) ([]byte, bool) {
	if !sess.Authenticated {
		return nil, true
	}

	pos, gpsFix, ok, reason := parseGPSBlock(pf.Payload)
	if len(pf.Payload) >= 5 {
		// This frame's ignition state feeds the GPS filter below.
		ign, _ := parseTerminalInfo(pf.Payload[len(pf.Payload)-5])
		sess.lastIgnition = &ign
	}
	if ok {
		if reason != "" {
			log.Printf("gt06: %s: 0x%02X %s: imei=%s", remote, pf.ProtocolNumber, reason, sess.IMEI)
		}
		if err := persistPosition(ctx, pool, sess, remote, pos, gpsFix, gpsSatellites(pf.Payload)); err != nil {
			log.Printf("gt06: %s: error storing alarm position imei=%s: %v", remote, sess.IMEI, err)
		}
	} else {
		log.Printf("gt06: %s: 0x%02X with invalid GPS block (%s), alarm still attempted: imei=%s len=%d payloadHex=%s", remote, pf.ProtocolNumber, reason, sess.IMEI, len(pf.Payload), hex.EncodeToString(pf.Payload))
	}

	if len(pf.Payload) >= alarmTrailerLen {
		// Terminal Information Content (5.3.1.14) -- first byte of the
		// trailer, 3 bytes before the alarm byte. Updated on EVERY alarm
		// frame, including periodic pings without a real alarm
		// (alarmByte=0x00) -- those are exactly what the device sends just to
		// report its status. See parseTerminalInfo for why the raw byte is
		// logged.
		terminalInfoByte := pf.Payload[len(pf.Payload)-5]
		ignitionOn, powerConnected := parseTerminalInfo(terminalInfoByte)
		if err := db.WithBypass(ctx, pool, func(ctx context.Context, tx pgx.Tx) error {
			return db.UpdateDeviceStatus(ctx, tx, sess.DeviceID, &ignitionOn, &powerConnected)
		}); err != nil {
			log.Printf("gt06: %s: error storing ignition/power imei=%s: %v", remote, sess.IMEI, err)
		} else {
			log.Printf("gt06: %s: terminal_info=0x%02X ignition=%v power=%v imei=%s", remote, terminalInfoByte, ignitionOn, powerConnected, sess.IMEI)
		}

		alarmByte := pf.Payload[len(pf.Payload)-2] // first byte of "Alarm/Language" (5.3.1.17)
		if alarmByte != 0x00 {                     // 0x00 = "Normal (no alarm)", not a real alarm
			info, known := alarmCodeName[alarmByte]
			if !known {
				info = struct {
					alarmType string
					severity  string
				}{alarmType: "gt06_unknown", severity: "info"}
				log.Printf("gt06: %s: unrecognized alarm code 0x%02X, recorded as gt06_unknown: imei=%s", remote, alarmByte, sess.IMEI)
			}
			alarmTime := time.Now().UTC()
			if ok {
				alarmTime = pos.Time
			}
			err := db.WithBypass(ctx, pool, func(ctx context.Context, tx pgx.Tx) error {
				_, err := db.InsertAlarm(ctx, tx, sess.TenantID, sess.DeviceID, alarmTime, info.alarmType, info.severity)
				return err
			})
			if err != nil {
				log.Printf("gt06: %s: error storing alarm %s imei=%s: %v", remote, info.alarmType, sess.IMEI, err)
			} else {
				log.Printf("gt06: alarm %s from imei=%s", info.alarmType, sess.IMEI)
			}
		}
	} else {
		log.Printf("gt06: %s: 0x%02X payload too short for the alarm block, no alarm recorded: imei=%s len=%d", remote, pf.ProtocolNumber, sess.IMEI, len(pf.Payload))
	}

	// The ACK must always echo the SAME protocol number the device sent
	// (Wanway 0x26 or classic Concox 0x16, whichever arrived) -- verified
	// against 5.3.2 for 0x26, and by direct observation of real hardware for
	// 0x16 (the device expects its own protocol number back, not a fixed
	// one).
	return encodeFrame(pf.ProtocolNumber, nil, pf.Serial), false
}

// videoEventGroup is ONE real event detected by the device, regardless of how
// many files/cameras recorded it -- see parseVideoEventGroups.
type videoEventGroup struct {
	imei     string
	code     string
	when     time.Time
	files    []string
	channels []string
}

// key identifies an event stably for deduplication (sess.seenVideoEvents) --
// exposed so tests can verify grouping without depending on map iteration
// order.
func (g videoEventGroup) key() string {
	return g.imei + "_" + g.code + "_" + g.when.Format("20060102150405")
}

// frontFile returns the Front camera file ("F", see videoEventPattern) in the
// group, used by the automatic clip request (RequestClipForAlarm via
// UPLOADFILE, see alarmclip.go) -- the same default channel used across the
// project (channel 0 / "Front"). If the event only carried the cabin channel
// ("I"), it falls back to the first available file instead of requesting
// nothing -- a clip from the other camera beats none.
func (g videoEventGroup) frontFile() string {
	for i, ch := range g.channels {
		if strings.EqualFold(ch, "F") {
			return g.files[i]
		}
	}
	if len(g.files) > 0 {
		return g.files[0]
	}
	return ""
}

// cabinFile returns the Cabin camera file ("I", see videoEventPattern), the
// counterpart of frontFile() for requesting the second camera. Unlike
// frontFile(), it does NOT fall back to any file if there is no "I" channel
// -- it deliberately returns "" (the caller reads it as "this event carried
// no cabin camera" and never requests the same front file twice).
func (g videoEventGroup) cabinFile() string {
	for i, ch := range g.channels {
		if strings.EqualFold(ch, "I") {
			return g.files[i]
		}
	}
	return ""
}

// parseVideoEventGroups is the PURE part (no Postgres) of
// handleVideoEventReport, extracted so it can be unit-tested, like
// parseGPSBlock/parseIMEI in this file. now is passed explicitly (not
// time.Now()) so the date guardrail tests are deterministic.
func parseVideoEventGroups(payload []byte, now time.Time) []videoEventGroup {
	matches := videoEventPattern.FindAllStringSubmatch(string(payload), -1)
	if len(matches) == 0 {
		return nil
	}

	groups := make(map[string]*videoEventGroup)
	var order []string
	for _, m := range matches {
		imei, code := m[1], m[2]
		year, month, day := m[3], m[4], m[5]
		hour, min, sec := m[6], m[7], m[8]
		channel := m[9]
		key := imei + "_" + code + "_" + year + month + day + hour + min + sec
		g, exists := groups[key]
		if !exists {
			t, err := time.Parse("2006-01-02T15:04:05", fmt.Sprintf("%s-%s-%sT%s:%s:%s", year, month, day, hour, min, sec))
			// Same guardrail as parseGPSBlock (maxFutureDrift/minValidTime)
			// -- GT06 device clocks have proven unreliable (RTC stuck in
			// 2007), and this timestamp source is just as outside our
			// control.
			if err != nil || t.Before(minValidTime) || t.After(now.Add(maxFutureDrift)) {
				t = now
			}
			g = &videoEventGroup{imei: imei, code: code, when: t}
			groups[key] = g
			order = append(order, key)
		}
		g.files = append(g.files, m[0])
		g.channels = append(g.channels, channel)
	}

	result := make([]videoEventGroup, 0, len(order))
	for _, key := range order {
		result = append(result, *groups[key])
	}
	return result
}

// handleVideoEventReport processes the JC261's spontaneous report when it
// records an event clip (collision/impact/panic button, detected by the
// device itself) -- see protoVideoEventReport. Discovery phase (decoded from
// real samples, no confirmed specification): the binary header before the
// ASCII list is NOT interpreted yet (it may carry coordinates/severity/exact
// event type) -- only what IS unambiguous is extracted, the list of recorded
// files; the rest is never guessed. An event recorded by two cameras (e.g.
// "..._I_24.ts" + "..._F_23.ts") produces ONE alarm, not two -- grouped by
// (imei, code, timestamp), ignoring the channel.
func handleVideoEventReport(ctx context.Context, pool *pgxpool.Pool, sess *connSession, remote string, pf parsedFrame, clipRequester ClipRequester) ([]byte, bool) {
	if !sess.Authenticated {
		return nil, true
	}

	groups := parseVideoEventGroups(pf.Payload, time.Now().UTC())
	if len(groups) == 0 {
		// A header/format different from the samples decoded so far -- logged
		// raw so the pattern can be adjusted against real evidence; nothing
		// about the payload is assumed.
		log.Printf("gt06: %s: 0x%02X (camera event report) without recognizable EVENT_ files, imei=%s payloadHex=%s", remote, pf.ProtocolNumber, sess.IMEI, hex.EncodeToString(pf.Payload))
		return encodeFrame(pf.ProtocolNumber, nil, pf.Serial), false
	}

	if sess.seenVideoEvents == nil {
		sess.seenVideoEvents = make(map[string]struct{})
	}

	for _, g := range groups {
		if g.imei != sess.IMEI {
			// An IMEI different from this authenticated connection's should
			// never appear -- dropped for safety; an alarm is never recorded
			// under ANOTHER device's tenant/device.
			log.Printf("gt06: %s: camera event with imei=%s does not match the authenticated connection imei=%s, dropped", remote, g.imei, sess.IMEI)
			continue
		}
		key := g.key()
		if _, seen := sess.seenVideoEvents[key]; seen {
			continue
		}
		sess.seenVideoEvents[key] = struct{}{}

		var alarmID uuid.UUID
		err := db.WithBypass(ctx, pool, func(ctx context.Context, tx pgx.Tx) error {
			id, err := db.InsertAlarm(ctx, tx, sess.TenantID, sess.DeviceID, g.when, "gt06_camera_event", "warning")
			alarmID = id
			return err
		})
		if err != nil {
			log.Printf("gt06: %s: error storing camera event alarm imei=%s code=%s: %v", remote, sess.IMEI, g.code, err)
			continue
		}
		log.Printf("gt06: alarm gt06_camera_event from imei=%s code=%s channels=%v files=%v", sess.IMEI, g.code, g.channels, g.files)

		// Automatic clip request -- see gt06server.ClipRequester/
		// alarmclip.Bridge.RequestClipForAlarm. clipRequester is nil-safe: in
		// tests (always nil) or if main.go did not configure it, this block
		// fires nothing -- the alarm is already stored above.
		if clipRequester != nil {
			clipRequester.RequestClipForAlarm(ctx, sess.TenantID, sess.DeviceID, alarmID, sess.IMEI, g.when, g.frontFile(), g.cabinFile())
		}
	}

	return encodeFrame(pf.ProtocolNumber, nil, pf.Serial), false
}
