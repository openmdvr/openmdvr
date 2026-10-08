package jt808server

import (
	"context"
	"errors"
	"io"
	"log"
	"net"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/cuteLittleDevil/go-jt808/protocol/jt808"

	"github.com/openmdvr/openmdvr/jt808-server/internal/datausage"
	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
	"github.com/openmdvr/openmdvr/jt808-server/internal/session"
)

// idleTimeout is how long to wait without receiving any byte before closing
// the connection. Each terminal configures its own JT808 heartbeat interval
// (typically 30s-5min); this leaves ample margin without keeping dead
// connections open forever (per-connection memory/goroutines matter with
// thousands of concurrent devices). 12 min rather than 5: with a heartbeat
// set to 5 min, cutting at exactly 5 min is a race that drops the connection
// every cycle (seen in practice with GT06, see gt06server/conn.go).
const idleTimeout = 12 * time.Minute

const readBufferSize = 4096

// maxFrameBufferSize bounds the frame reassembly buffer
// (FrameReader.historyData). Without it, a peer that opens a frame with 0x7e
// and never closes it makes the buffer grow on every conn.Read -- confirmed
// in a security review: a single such connection took the process from ~3MB
// to hundreds of MB of RSS with no sign of stabilizing, and since it is
// memory of the WHOLE process (not per connection), it brings the server down
// for all tenants, not just the attacker. The largest JT808 body the codec
// produces in a single frame is maxBodyLength=1000 (see
// protocol/jt808/packet_codec.go); with room for the header (2019: up to ~21
// bytes) and worst-case escaping (every byte could double), a legitimate
// frame should never come close to this limit.
const maxFrameBufferSize = 8192

// dataUsageFlushInterval: how often the accumulated usage of a long-lived
// connection is flushed (see internal/datausage). Without it, a long-lived
// connection would only report its usage on close, which can take
// hours/days. The final flush (defer, below) always covers the last partial
// slice.
const dataUsageFlushInterval = 5 * time.Minute

// flushDataUsage writes the CountingConn's accumulated delta to the monthly
// rollup (record_device_data_usage) -- only once the device is known
// (sess.DeviceID is set on registration, see processFrame/handleRegister).
// Uses context.Background(), NEVER the server ctx: this accounting write must
// not fail just because the process is shutting down.
func flushDataUsage(pool *pgxpool.Pool, conn *datausage.CountingConn, sess *session.Session, remote string) {
	rx, tx := conn.TakeDelta()
	if sess.DeviceID == uuid.Nil {
		return
	}
	if err := db.RecordDeviceDataUsage(context.Background(), pool, sess.DeviceID, rx, tx); err != nil {
		log.Printf("jt808: %s: recording data usage: %v", remote, err)
	}
}

func handleConn(ctx context.Context, pool *pgxpool.Pool, registry *session.Registry, conn *datausage.CountingConn) {
	remote := conn.RemoteAddr().String()
	sess := session.New(conn)
	defer func() {
		_ = conn.Close()
		flushDataUsage(pool, conn, sess, remote)
		if sess.TerminalID != "" {
			registry.Unregister(sess.TerminalID, sess)
		}
		// A panic in any goroutine (including this one) brings down the WHOLE
		// process unless recovered here -- a single device sending bytes that
		// trigger an out-of-range index while parsing (our bug or the
		// protocol library's) would close the connection of ALL tenants, not
		// just its own. recover() confines the damage to this one connection:
		// it is closed and logged, and the server keeps serving everyone else.
		if r := recover(); r != nil {
			log.Printf("jt808: %s: recovered panic, closing this connection: %v", remote, r)
		}
		log.Printf("jt808: connection closed: %s", remote)
	}()
	log.Printf("jt808: connection accepted: %s", remote)

	reader := jt808.NewFrameReader()
	buf := make([]byte, readBufferSize)
	lastDataUsageFlush := time.Now()

	for {
		if err := conn.SetReadDeadline(time.Now().Add(idleTimeout)); err != nil {
			log.Printf("jt808: %s: error setting read deadline: %v", remote, err)
			return
		}

		n, err := conn.Read(buf)
		if err != nil {
			logReadError(remote, err)
			return
		}
		data := buf[:n]

		if time.Since(lastDataUsageFlush) >= dataUsageFlushInterval {
			flushDataUsage(pool, conn, sess, remote)
			lastDataUsageFlush = time.Now()
		}

		if frame, ok := reader.FeedSingleComplete(data); ok {
			if !processFrame(ctx, pool, registry, sess, remote, frame) {
				return
			}
			continue
		}

		reader.Append(data)
		if reader.Pending() > maxFrameBufferSize {
			log.Printf("jt808: %s: reassembly buffer exceeds %d bytes without closing a frame, closing connection", remote, maxFrameBufferSize)
			return
		}
		for {
			frame, ok := reader.PopFrame()
			if !ok {
				break
			}
			if !processFrame(ctx, pool, registry, sess, remote, frame) {
				return
			}
		}
	}
}

// processFrame decodes and dispatches an already delimited frame. Returns
// false if the connection must be closed (I/O error while replying or an
// unrecoverable internal error); a merely invalid frame (checksum, format) is
// logged and discarded without closing the connection -- a field device with
// line noise must not lose its session over a single corrupt packet.
func processFrame(ctx context.Context, pool *pgxpool.Pool, registry *session.Registry, sess *session.Session, remote string, frame []byte) bool {
	jtMsg := jt808.NewJTMessage()
	if err := jtMsg.Decode(frame); err != nil {
		log.Printf("jt808: %s: invalid frame discarded: %v", remote, err)
		return true
	}

	sess.UpdateHeader(jtMsg.Header)
	if sess.TerminalID == "" && jtMsg.Header.TerminalPhoneNo != "" {
		sess.TerminalID = jtMsg.Header.TerminalPhoneNo
		registry.Register(sess.TerminalID, sess)
	}

	rep, err := dispatch(ctx, pool, sess, jtMsg)
	if err != nil {
		log.Printf("jt808: %s: error processing message 0x%04x: %v", remote, jtMsg.Header.ID, err)
		// Reply "failure" (1) instead of not replying at all: a JT808
		// terminal expects an ack for every message and retries indefinitely
		// without one. Without this reply, a transient DB error (a momentary
		// pool disconnect, for example) would leave the device waiting
		// without knowing its data was NOT stored. Several MDVRs only clear
		// their local buffer (SD card) after a success ack, so a missing ack
		// is safer than a false one, but an explicit "failure" is better than
		// silence: it gives the firmware the right signal to retry with its
		// own logic instead of relying only on its own timeout.
		// Note: this always replies with the general 0x8001 response format,
		// even if the incoming message was 0x0100 (normally answered with
		// 0x8100). A deliberate simplicity trade-off for an already rare case
		// (a DB failure during registration, not a normal rejection -- those
		// already reply 0x8100 correctly in handleRegister without going
		// through here): an ack of an "unexpected shape" is still better than
		// no ack, to avoid indefinite retries.
		rep = generalRespond(jtMsg, 1)
	}

	jtMsg.Header.ReplyID = rep.id
	jtMsg.Header.PlatformSerialNumber = sess.NextPlatformSerial()
	packets := jtMsg.Header.EncodePackets(rep.body)

	// WriteFramed serializes against any active send (0x9101) an HTTP
	// handler might be sending on this same connection in parallel (see
	// internal/session/session.go) -- sess.Conn is never written to directly
	// outside this method.
	if err := sess.WriteFramed(packets, 10*time.Second); err != nil {
		log.Printf("jt808: %s: error writing reply: %v", remote, err)
		return false
	}
	return true
}

func logReadError(remote string, err error) {
	if errors.Is(err, io.EOF) {
		return
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		log.Printf("jt808: %s: closing due to inactivity (%s without data)", remote, idleTimeout)
		return
	}
	log.Printf("jt808: %s: read error: %v", remote, err)
}
