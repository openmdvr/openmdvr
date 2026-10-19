// Package videobridge is the protocol-agnostic core of the live video
// system: infrastructure that EVERY video protocol (JT1078 pull, GT06/RTMP
// push, and any future one) shares -- one-time playback tickets, the HTTP
// client for the ZLMediaKit API, the active-stream registry with its cut-off
// timer, and dispatching ZLMediaKit hooks to the right Protocol
// implementation (see dispatcher.go).
//
// No concrete protocol lives here; that is exactly what this package exists
// to avoid (see internal/jt1078bridge and internal/gt06videobridge, each
// implementing Protocol without knowing about the other). Same approach as
// remote commands (internal/commands: Sender interface + per-protocol
// registry).
package videobridge

import (
	"crypto/rand"
	"encoding/hex"
	"sync"
	"time"
)

// ticketTTL is the lifetime of a video ticket: long enough for the browser
// to receive the URL, open the player and for ZLMediaKit to fire on_play
// (seconds), but short so a leaked URL dies fast. The ticket only gates the
// START of playback; an already-running session is governed by the tenant's
// per-session limit (enforced by each Protocol) and the monthly quota, not
// by this TTL. This is the usual url_expire/stream_expire split: the URL
// dies soon, the server cuts the stream.
const ticketTTL = 90 * time.Second

// Ticket is a one-time authorization to START a playback session. The API
// issues it (POST /devices/{id}/video, already authenticated with JWT + RLS
// + active tenant) and ZLMediaKit's on_play hook consumes it exactly once
// (Dispatcher.handlePlayAuth). It is a bearer token: whoever holds the URL
// can use it -- hence single-use and short-lived. Sharing the URL outside
// the authorized session is useless because the ticket dies on first use. A
// reusable signed token would only solve "guessable", while a one-time
// ticket also solves "shareable"; it mirrors the one-time ticket store
// already used for the positions SSE stream (api/app/live_positions.py).
type Ticket struct {
	// TerminalID and Channel identify the authorized stream. The on_play
	// hook verifies that the stream ZLMediaKit reports (from the URL the
	// client requested) matches EXACTLY this device/channel, not just that
	// the ticket exists. Without this, a valid ticket for one camera could be
	// reused for another by guessing its stream_id. The name "TerminalID" is
	// historical (JT1078 was the first protocol) but the value is generic:
	// each Protocol decides what goes here (JT808 terminalID or GT06 IMEI).
	TerminalID string
	Channel    uint8
	// App binds the ticket to the correct stream NAMESPACE (the issuing
	// Protocol's App()). Security finding (F6): without it, a ticket issued
	// for a jt808 device whose jt808_terminal_id NUMERICALLY matches another
	// tenant's gt06_imei (two columns with independent UNIQUE constraints --
	// nothing in the schema prevents a collision across both identifier
	// spaces) authorized playing that victim's gt06_video stream just by
	// requesting the wrong app. Verified exploitable before the fix.
	App string
	// TenantID is stored so on_play can verify ownership without a second DB
	// query in the hot path: the stream<->tenant mapping was already resolved
	// by the API when issuing the ticket (via RLS); here it is only compared.
	TenantID  string
	ExpiresAt time.Time
}

// TicketStore is the in-memory store of tickets pending consumption. It
// lives ONLY in this process: with more than one bridge replica, a ticket
// issued by one replica is not recognized by another -- same accepted and
// documented limitation as the positions SSE ticket store
// (api/app/live_positions.py). The zero value is not usable; use
// NewTicketStore.
type TicketStore struct {
	mu      sync.Mutex
	tickets map[string]Ticket
}

func NewTicketStore() *TicketStore {
	return &TicketStore{tickets: make(map[string]Ticket)}
}

// Mint creates and stores a new ticket. Returns the opaque token (URL-safe,
// generated with crypto/rand) the API puts in the playback URL's query
// string.
func (s *TicketStore) Mint(tenantID, terminalID, app string, channel uint8) (string, error) {
	// 32 bytes of entropy = 64 hex chars, same size as Python's
	// secrets.token_urlsafe(32). A shorter token could be brute-forced by
	// someone able to try many URLs against ZLMediaKit.
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	token := hex.EncodeToString(buf)

	s.mu.Lock()
	defer s.mu.Unlock()
	s.evictExpiredLocked()
	s.tickets[token] = Ticket{
		TenantID:   tenantID,
		TerminalID: terminalID,
		App:        app,
		Channel:    channel,
		ExpiresAt:  time.Now().Add(ticketTTL),
	}
	return token, nil
}

// Consume removes the ticket from the store (single use: atomic pop under
// the mutex, no race between two concurrent consumes of the same token) and
// returns it if it exists AND has not expired. Returns nil if missing,
// already used, or expired -- on_play treats all of those as "unauthorized"
// without distinguishing, to avoid leaking which check failed.
func (s *TicketStore) Consume(token string) *Ticket {
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.tickets[token]
	if !ok {
		return nil
	}
	delete(s.tickets, token)
	if time.Now().After(t.ExpiresAt) {
		return nil
	}
	tt := t // local copy so we never return a pointer into the map
	return &tt
}

// evictExpiredLocked deletes expired tickets. Called from Mint (already under
// the mutex) so normal issuance keeps the map bounded without a separate
// sweeper -- tickets are short-lived (90s) and every consume also deletes,
// so there is no path where it grows without bound. Must be called with s.mu
// held.
func (s *TicketStore) evictExpiredLocked() {
	now := time.Now()
	for k, t := range s.tickets {
		if now.After(t.ExpiresAt) {
			delete(s.tickets, k)
		}
	}
}
