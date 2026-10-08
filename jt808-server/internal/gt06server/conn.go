package gt06server

import (
	"context"
	"errors"
	"io"
	"log"
	"net"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/openmdvr/openmdvr/jt808-server/internal/datausage"
	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
)

// idleTimeout: same rule as jt808server/conn.go -- a real GT06 tracker sends
// a heartbeat every 30s-5min depending on its configuration. With exactly
// 5 min and a JC261 sending heartbeats every 5 min, the server cut the
// connection every cycle and the camera took up to 3 min to come back;
// live video and clip uploads failed in that gap. 12 min tolerates one
// missed heartbeat from a device configured at 5 min, without keeping dead
// connections open indefinitely.
const idleTimeout = 12 * time.Minute

// writeTimeout is used both for the read loop's ACKs (below) and for active
// commands (commands.go) -- one constant, one rule for "how long to wait for
// the socket to accept a write" across the package.
const writeTimeout = 10 * time.Second

const readBufferSize = 4096

// dataUsageFlushInterval: same as jt808server/conn.go.
const dataUsageFlushInterval = 5 * time.Minute

// flushDataUsage: same rule as jt808server/conn.go (see the reasoning
// there). Uses context.Background(), never the server ctx, so accounting is
// not lost during shutdown.
func flushDataUsage(pool *pgxpool.Pool, conn *datausage.CountingConn, sess *connSession, remote string) {
	rx, tx := conn.TakeDelta()
	if sess.DeviceID == uuid.Nil {
		return
	}
	if err := db.RecordDeviceDataUsage(context.Background(), pool, sess.DeviceID, rx, tx); err != nil {
		log.Printf("gt06: %s: recording data usage: %v", remote, err)
	}
}

func handleConn(ctx context.Context, pool *pgxpool.Pool, registry *Registry, clipRequester ClipRequester, conn *datausage.CountingConn) {
	remote := conn.RemoteAddr().String()
	sess := &connSession{conn: conn}
	// registeredIMEI (rather than a plain bool) is deliberate -- security
	// review finding (F5): if this connection re-authenticated with a
	// different IMEI (handleLogin overwrites sess.IMEI on every successful
	// login), unregistering with sess.IMEI on close would remove the NEW
	// IMEI's entry and leave the OLD one orphaned in the Registry forever
	// (growing without bound, with future commands to the old IMEI writing to
	// a closed socket). Store the IMEI actually registered and unregister
	// with THAT, never sess.IMEI at close time.
	registeredIMEI := ""
	defer func() {
		_ = conn.Close()
		flushDataUsage(pool, conn, sess, remote)
		if registeredIMEI != "" {
			registry.Unregister(registeredIMEI, sess)
		}
		// A panic while parsing one device's traffic must not take down the
		// whole process for every tenant -- same rule as jt808server/conn.go
		// and jt1078bridge/conn.go.
		if r := recover(); r != nil {
			log.Printf("gt06: %s: recovered panic, closing this connection: %v", remote, r)
		}
		log.Printf("gt06: connection closed: %s", remote)
	}()
	log.Printf("gt06: connection accepted: %s", remote)

	reader := &packetReader{}
	buf := make([]byte, readBufferSize)
	lastDataUsageFlush := time.Now()

	for {
		if err := conn.SetReadDeadline(time.Now().Add(idleTimeout)); err != nil {
			log.Printf("gt06: %s: error setting read deadline: %v", remote, err)
			return
		}

		n, err := conn.Read(buf)
		if err != nil {
			logReadError(remote, err)
			return
		}

		if time.Since(lastDataUsageFlush) >= dataUsageFlushInterval {
			flushDataUsage(pool, conn, sess, remote)
			lastDataUsageFlush = time.Now()
		}

		frames, err := reader.Feed(buf[:n])
		if err != nil {
			// Corrupt framing (or an incomplete packet over the size cap) --
			// GT06 has no agreed way to resynchronize mid-stream, so this
			// connection is closed (the device reconnects by itself).
			log.Printf("gt06: %s: %v, closing connection", remote, err)
			return
		}

		for _, frame := range frames {
			resp, closeConn := handleFrame(ctx, pool, sess, remote, frame, clipRequester)
			// Register as soon as login resolves IMEI/tenant/device -- the
			// same moment jt808server/conn.go registers its Session (the
			// first time the device's real identifier is known), never
			// earlier. Only the FIRST time (registeredIMEI == "") -- a later
			// re-login with another IMEI on the same connection does not
			// register again (see registeredIMEI above).
			if registeredIMEI == "" && sess.Authenticated && sess.IMEI != "" {
				registry.Register(sess.IMEI, sess)
				registeredIMEI = sess.IMEI
			}
			if resp != nil {
				if err := sess.writeFrame(resp, writeTimeout); err != nil {
					log.Printf("gt06: %s: error writing response: %v", remote, err)
					return
				}
			}
			if closeConn {
				return
			}
		}
	}
}

func logReadError(remote string, err error) {
	if errors.Is(err, io.EOF) {
		return
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		log.Printf("gt06: %s: closing due to inactivity (%s without data)", remote, idleTimeout)
		return
	}
	log.Printf("gt06: %s: read error: %v", remote, err)
}
