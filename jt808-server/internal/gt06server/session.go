package gt06server

import (
	"fmt"
	"net"
	"sync"
	"time"

	"github.com/google/uuid"
)

// connSession is the state of ONE GT06 connection; it lives as long as that
// TCP connection. Because this package supports remote commands, it is not
// self-contained: it registers in its own Registry (registry.go) so an
// external Dispatcher (commands.go) can find it and write an active command
// to it. mu protects exactly those two contention points (the physical
// socket write and the pending command), as in internal/session.Session.
type connSession struct {
	IMEI          string
	DeviceID      uuid.UUID
	TenantID      uuid.UUID
	Authenticated bool
	// lastIgnition is the last reported ignition state (heartbeat or alarm).
	// The GPS quality filter uses it as evidence (with ignition off it
	// requires more before accepting a departure). Only touched by this
	// connection's handlers (one goroutine).
	lastIgnition *bool

	mu             sync.Mutex
	conn           net.Conn
	platformSerial uint16
	// pending is the active command awaiting a reply on THIS connection, if
	// any. GT06 has no reliable command/reply correlation ID: the 4-byte
	// "Server Flag Bit" (section 6.1.5 of the protocol document) is in theory
	// echoed "unchanged" by the device, but in practice common server
	// implementations always send it as zero and never rely on it. So
	// correlation is per connection: one command in flight per device, and
	// the next 0x15 arriving on this SAME connection is its reply.
	//
	// SECURITY FINDING (reproduced with a test): if a timeout cleared
	// `pending` immediately, a LATE reply to the previous command could
	// resolve the NEXT one (e.g. a late 0x15 for "engine stop" arriving after
	// "engine resume" was sent, marking resume as "success" without the
	// device ever confirming it) -- the worst possible error for a command
	// that cuts fuel to a real vehicle. So `pending` is NEVER cleared
	// proactively when the caller's timeout expires (see commands.go); it is
	// released only when ITS OWN reply arrives (handleCommandReply) or after
	// maxAbandonedPendingAge (self-healing for a device that never replies).
	// Meanwhile a second attempt on the same connection is rejected
	// (ErrCommandBusy) instead of risking ambiguous correlation -- isolation
	// by TIME, not by an untrustworthy message ID. See canTakeOver for the
	// narrow, keyword-checked exceptions.
	pending *pendingCommand

	// seenVideoEvents deduplicates camera event reports (protocol 0x95, see
	// handleVideoEventReport) within THIS TCP connection -- the device has
	// been observed re-sending the same batch of files more than once
	// (unconfirmed whether due to a missing ACK or otherwise, see
	// protoVideoEventReport). Only read/written by this connection's read
	// goroutine (like IMEI/DeviceID above, never touched by the external
	// Dispatcher), so it needs no mu. A real device reconnect can still
	// repeat the same event -- an accepted v1 limitation, documented in the
	// handler.
	seenVideoEvents map[string]struct{}
}

type pendingCommand struct {
	ch        chan string
	createdAt time.Time
	// keyword: command name ("RTMP", "PICTURE", "DYD"...), see
	// commandKeyword. abandonAt: when the caller stops waiting for the reply
	// (its own timeout). Both feed the safe slot handover, see canTakeOver
	// in commands.go.
	keyword   string
	abandonAt time.Time
}

// maxAbandonedPendingAge is deliberately generous (well above the longest
// timeout any real caller uses, currently 15s in
// internal/commands.commandTimeout) so a connection is never "busy" forever
// if the device simply never sends another 0x15 (e.g. firmware that does
// not support the command at all).
//
// ACCEPTED TRADE-OFF (from the security review of GT06 video, Jimi IoT
// JC261/JC400): since request_video/stop_video reuse this SAME single
// `pending` slot per connection, a stuck video request (device never
// answering the 0x15) could block "engine stop" for up to these 2 minutes.
// This constant is deliberately NOT differentiated per command type:
// shortening it only for video would reopen the slot before that command's
// own late reply could arrive, and if a NEW command (e.g. engine_stop) had
// taken the slot meanwhile, the late video 0x15 would resolve IT as
// "success" without the device ever confirming it -- exactly the critical
// finding this mechanism exists to prevent. Engine-cut safety outweighs
// live-view availability. (canTakeOver later added a keyword-checked
// handover that narrows this window without reopening that finding.)
const maxAbandonedPendingAge = 2 * time.Minute

// writeFrame serializes socket access -- both the read loop's normal ACKs
// (conn.go) and active commands (commands.go) go through here; neither
// writes to conn directly.
func (s *connSession) writeFrame(frame []byte, timeout time.Duration) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.conn == nil {
		return fmt.Errorf("gt06: session has no active connection")
	}
	if err := s.conn.SetWriteDeadline(time.Now().Add(timeout)); err != nil {
		return fmt.Errorf("gt06: setting write deadline: %w", err)
	}
	_, err := s.conn.Write(frame)
	return err
}

// nextSerial returns the next PLATFORM sequence number (for frames the
// server initiates, such as an active command). It is never used to reply to
// something the device sent -- those echo the incoming serial (see
// handlers.go, encodeFrame(protocolNumber, nil, pf.Serial)).
func (s *connSession) nextSerial() uint16 {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.platformSerial++
	return s.platformSerial
}
