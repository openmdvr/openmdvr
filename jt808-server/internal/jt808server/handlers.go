package jt808server

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"log"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/cuteLittleDevil/go-jt808/protocol/jt808"
	"github.com/cuteLittleDevil/go-jt808/protocol/model"
	"github.com/cuteLittleDevil/go-jt808/shared/consts"

	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
	"github.com/openmdvr/openmdvr/jt808-server/internal/gpsfilter"
	"github.com/openmdvr/openmdvr/jt808-server/internal/session"
)

// cstOffset is the fixed GMT+8 time zone JT/T 808 uses for the location
// report time field (documented in the protocol library itself).
// time.FixedZone is used instead of time.LoadLocation("Asia/Shanghai") so we
// do not depend on the Docker image shipping the system time zone database --
// GMT+8 has no daylight saving time, so a fixed offset is exact.
var cstOffset = time.FixedZone("CST", 8*60*60)

// reply is what a handler returns to the connection loop: the JT808 reply
// message ID and its already encoded body.
type reply struct {
	id   uint16
	body []byte
}

// dispatch picks the handler for each incoming message by its ID. Any
// unsupported message is answered with the standard "not supported" code
// instead of being ignored -- a JT808 terminal expects a reply to everything
// it sends and may retry indefinitely without one.
func dispatch(ctx context.Context, pool *pgxpool.Pool, sess *session.Session, jtMsg *jt808.JTMessage) (reply, error) {
	switch consts.JT808CommandType(jtMsg.Header.ID) {
	case consts.T0100Register:
		return handleRegister(ctx, pool, sess, jtMsg)
	case consts.T0102RegisterAuth:
		return handleAuth(ctx, pool, sess, jtMsg)
	case consts.T0002HeartBeat:
		return handleHeartbeat(ctx, pool, sess, jtMsg)
	case consts.T0200LocationReport:
		return handleLocation(ctx, pool, sess, jtMsg)
	default:
		return generalRespond(jtMsg, 3), nil // 3 = not supported
	}
}

func generalRespond(jtMsg *jt808.JTMessage, result byte) reply {
	p8001 := &model.P0x8001{
		RespondSerialNumber: jtMsg.Header.SerialNumber,
		RespondID:           jtMsg.Header.ID,
		Result:              result,
	}
	return reply{id: uint16(consts.P8001GeneralRespond), body: p8001.Encode()}
}

// resolveDevice looks up the provisioned device for the incoming message's
// terminal_id. See the authentication scope note in
// internal/session/session.go: both 0x0100 and 0x0102 resolve identity the
// same way.
func resolveDevice(ctx context.Context, pool *pgxpool.Pool, terminalID string) (db.Device, error) {
	var dev db.Device
	err := db.WithBypass(ctx, pool, func(ctx context.Context, tx pgx.Tx) error {
		var err error
		dev, err = db.LookupDeviceByTerminalID(ctx, tx, terminalID)
		return err
	})
	return dev, err
}

func handleRegister(ctx context.Context, pool *pgxpool.Pool, sess *session.Session, jtMsg *jt808.JTMessage) (reply, error) {
	sess.TerminalID = jtMsg.Header.TerminalPhoneNo
	dev, err := resolveDevice(ctx, pool, sess.TerminalID)

	p8100 := &model.P0x8100{RespondSerialNumber: jtMsg.Header.SerialNumber}
	switch {
	case errors.Is(err, db.ErrDeviceNotFound):
		p8100.Result = 4 // terminal not in database
		log.Printf("jt808: registration rejected (terminal not provisioned): terminal=%s", sess.TerminalID)
	case err != nil:
		return reply{}, err
	case dev.Status != "active":
		p8100.Result = 4
		log.Printf("jt808: registration rejected (device inactive): terminal=%s status=%s", sess.TerminalID, dev.Status)
	default:
		code, genErr := randomAuthCode()
		if genErr != nil {
			return reply{}, genErr
		}
		p8100.Result = 0
		p8100.AuthCode = code
		sess.Authenticate(dev.ID, dev.TenantID)
		log.Printf("jt808: device authenticated via registration: terminal=%s device_id=%s tenant_id=%s", sess.TerminalID, dev.ID, dev.TenantID)
	}
	return reply{id: uint16(consts.P8100RegisterRespond), body: p8100.Encode()}, nil
}

func handleAuth(ctx context.Context, pool *pgxpool.Pool, sess *session.Session, jtMsg *jt808.JTMessage) (reply, error) {
	sess.TerminalID = jtMsg.Header.TerminalPhoneNo
	dev, err := resolveDevice(ctx, pool, sess.TerminalID)

	result := byte(0)
	switch {
	case errors.Is(err, db.ErrDeviceNotFound):
		result = 1
		log.Printf("jt808: authentication rejected (terminal not provisioned): terminal=%s", sess.TerminalID)
	case err != nil:
		return reply{}, err
	case dev.Status != "active":
		result = 1
		log.Printf("jt808: authentication rejected (device inactive): terminal=%s status=%s", sess.TerminalID, dev.Status)
	default:
		sess.Authenticate(dev.ID, dev.TenantID)
		log.Printf("jt808: device authenticated via 0x0102: terminal=%s device_id=%s tenant_id=%s", sess.TerminalID, dev.ID, dev.TenantID)
	}
	return generalRespond(jtMsg, result), nil
}

func handleHeartbeat(ctx context.Context, pool *pgxpool.Pool, sess *session.Session, jtMsg *jt808.JTMessage) (reply, error) {
	if !sess.Authenticated {
		return generalRespond(jtMsg, 1), nil
	}
	err := db.WithBypass(ctx, pool, func(ctx context.Context, tx pgx.Tx) error {
		return db.TouchLastSeen(ctx, tx, sess.DeviceID)
	})
	if err != nil {
		return reply{}, err
	}
	return generalRespond(jtMsg, 0), nil
}

func handleLocation(ctx context.Context, pool *pgxpool.Pool, sess *session.Session, jtMsg *jt808.JTMessage) (reply, error) {
	if !sess.Authenticated {
		return generalRespond(jtMsg, 1), nil
	}

	loc := &model.T0x0200{}
	if err := safeParseLocation(loc, jtMsg); err != nil {
		log.Printf("jt808: unreadable 0x0200 from terminal=%s: %v", sess.TerminalID, err)
		return generalRespond(jtMsg, 2), nil // 2 = message error
	}

	pos, err := toGPSPosition(loc)
	if err != nil {
		log.Printf("jt808: 0x0200 with invalid data from terminal=%s: %v", sess.TerminalID, err)
		return generalRespond(jtMsg, 2), nil
	}
	alarms := alarmsFromSignDetails(loc.AlarmSignDetails)
	ignitionOn, powerConnected := deviceStatusFromLocation(loc)

	err = db.WithBypass(ctx, pool, func(ctx context.Context, tx pgx.Tx) error {
		// Position: only with a valid GPS fix ("Location" bit of STATUS, same
		// rule as GT06 -- without a fix the terminal repeats the last position
		// or sends zeros, not real data) and always through the GPS quality
		// filter (parked drift, incoherent jumps, noise speed -- see
		// internal/gpsfilter). Alarms and status are recorded even without a
		// fix.
		if loc.StatusSignDetails.Location {
			reason, err := db.InsertGPSPositionFiltered(ctx, tx, sess.TenantID, sess.DeviceID, gpsfilter.Sample{
				Time: pos.Time, Lat: pos.Lat, Lon: pos.Lon, SpeedKmh: pos.SpeedKmh, Heading: pos.Heading,
				Satellites: gnssSatellites(loc), Ignition: &ignitionOn,
			}, pos.Altitude)
			if err != nil {
				return err
			}
			if reason != "" {
				log.Printf("jt808: GPS filter terminal=%s: %s", sess.TerminalID, reason)
			}
		} else {
			log.Printf("jt808: 0x0200 without GPS fix from terminal=%s, position not stored (alarms/status only)", sess.TerminalID)
		}
		for _, a := range alarms {
			if _, err := db.InsertAlarm(ctx, tx, sess.TenantID, sess.DeviceID, pos.Time, a.alarmType, a.severity); err != nil {
				return err
			}
		}
		// UpdateDeviceStatus also touches last_seen_at, so no separate
		// TouchLastSeen round-trip is needed (see db/devices.go). STATUS is a
		// mandatory JT/T 808-2019 field, so ignitionOn/powerConnected are
		// almost always a real signal, not "no data".
		return db.UpdateDeviceStatus(ctx, tx, sess.DeviceID, &ignitionOn, &powerConnected)
	})
	if err != nil {
		return reply{}, err
	}
	if len(alarms) > 0 {
		log.Printf("jt808: %d alarm(s) from terminal=%s: %v", len(alarms), sess.TerminalID, alarmTypeNames(alarms))
	}
	return generalRespond(jtMsg, 0), nil
}

func randomAuthCode() (string, error) {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return hex.EncodeToString(buf), nil
}
