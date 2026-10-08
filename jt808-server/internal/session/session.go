// Package session holds the state of an individual JT808 TCP connection.
package session

import (
	"fmt"
	"net"
	"sync"
	"time"

	"github.com/cuteLittleDevil/go-jt808/protocol/jt808"
	"github.com/google/uuid"
)

// Session lives as long as a TCP connection with a device. It is neither
// shared between connections nor persisted -- if the device reconnects, a new
// Session is created.
//
// Scope note on authentication (deliberate, not an oversight): both
// registration (0x0100) and authentication (0x0102) are resolved the same
// way -- by looking up the incoming message's jt808_terminal_id among the
// provisioned devices (see internal/jt808server/handlers.go). The auth code
// required by JT/T 808 is issued in the registration reply for protocol
// compatibility, but it is neither persisted nor validated on the later
// 0x0102 -- it is not a cryptographic identity mechanism. The real security
// barrier is "is this terminal_id an active device the platform provisioned
// for a tenant?", which IS always enforced (device provisioning is
// bypass-only, see 0008_rls_policies.sql -- a terminal_id cannot
// self-register under an arbitrary tenant). Hardening this later (persisted
// auth code, mutual TLS, per-device secret) is a change local to this
// package.
//
// A Session is not touched only by its own read goroutine: an HTTP handler
// (video request) may need to send the device an active command (0x9101) at
// any time. mu protects exactly those two contention points: the platform
// sequence counter and the physical socket write -- Conn must never be
// written outside WriteFramed/SendActive.
type Session struct {
	Conn net.Conn

	// TerminalID is the JT808 terminal number (typically the SIM number),
	// already decoded from BCD to text by the protocol library.
	TerminalID string

	Authenticated bool
	DeviceID      uuid.UUID
	TenantID      uuid.UUID

	mu             sync.Mutex
	platformSerial uint16
	// lastHeader is the Header of the last successfully decoded message from
	// this device. It is reused (copied, never mutated in place) to build
	// active messages to the device, because it is the only way to obtain the
	// already BCD-encoded phone number -- the jt808 package exposes no
	// "from scratch" Header constructor outside itself; it only lets you
	// decode an incoming one and reply on top of it.
	lastHeader *jt808.Header
}

func New(conn net.Conn) *Session {
	return &Session{Conn: conn}
}

// Authenticate marks the session as authenticated for a device/tenant
// already resolved in the database.
func (s *Session) Authenticate(deviceID, tenantID uuid.UUID) {
	s.Authenticated = true
	s.DeviceID = deviceID
	s.TenantID = tenantID
}

// UpdateHeader remembers the Header of the last decoded incoming message. It
// must be called after every successful decode, before building the reply.
func (s *Session) UpdateHeader(h *jt808.Header) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lastHeader = h
}

// WriteFramed serializes socket access: both this connection's normal
// read-loop replies and active commands (SendActive) go through here; neither
// writes to Conn directly.
func (s *Session) WriteFramed(packets [][]byte, timeout time.Duration) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.Conn.SetWriteDeadline(time.Now().Add(timeout)); err != nil {
		return fmt.Errorf("session: setting write deadline: %w", err)
	}
	for _, p := range packets {
		if _, err := s.Conn.Write(p); err != nil {
			return fmt.Errorf("session: writing: %w", err)
		}
	}
	return nil
}

// NextPlatformSerial returns the next sequence number for a message the
// server sends to the terminal -- used by the normal reply path (conn.go),
// which already has the Header of the request it is answering and only needs
// the sequence number.
func (s *Session) NextPlatformSerial() uint16 {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.platformSerial++
	return s.platformSerial
}

// NextPlatformSerialAndHeader returns the next platform sequence number
// together with an independent copy of the last known Header, atomically (so
// two concurrent calls never get the same sequence number). headerCopy is a
// separate value; mutating it affects neither other concurrent calls nor the
// Session state.
func (s *Session) NextPlatformSerialAndHeader() (serial uint16, headerCopy jt808.Header, ok bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.lastHeader == nil {
		return 0, jt808.Header{}, false
	}
	s.platformSerial++
	return s.platformSerial, *s.lastHeader, true
}

// SendActive sends a platform-initiated message (not a reply to something
// the device sent) -- currently used for 0x9101 (real-time video request).
// It fails if no message has been received from this device on this
// connection yet (there is no Header to start from).
func (s *Session) SendActive(msgID uint16, body []byte, timeout time.Duration) error {
	serial, header, ok := s.NextPlatformSerialAndHeader()
	if !ok {
		return fmt.Errorf("session: cannot send 0x%04x, the session has no incoming message yet", msgID)
	}
	header.ID = msgID
	header.ReplyID = 0
	header.PlatformSerialNumber = serial
	packets := header.EncodePackets(body)
	return s.WriteFramed(packets, timeout)
}
