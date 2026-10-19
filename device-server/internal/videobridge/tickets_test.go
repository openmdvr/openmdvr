package videobridge

import (
	"testing"
	"time"
)

// testApp is an arbitrary RTMP/RTP namespace for these tests. TicketStore is
// protocol-agnostic, so the real value does not matter here.
const testApp = "rtp"

// The ticket store is the core of the playback-authorization fix: if
// Consume() were not truly single-use, or an expired ticket still worked,
// the video URL would become shareable again -- exactly what it exists to
// prevent.

func TestTicketStore_MintConsumeSingleUse(t *testing.T) {
	s := NewTicketStore()
	tok, err := s.Mint("tenant-1", "13800000099", testApp, 1)
	if err != nil {
		t.Fatalf("mint: %v", err)
	}
	if len(tok) != 64 { // 32 bytes of entropy in hex
		t.Errorf("token length = %d, want 64", len(tok))
	}

	tk := s.Consume(tok)
	if tk == nil {
		t.Fatal("first consume returned nil, want the ticket")
	}
	if tk.TenantID != "tenant-1" || tk.TerminalID != "13800000099" || tk.Channel != 1 {
		t.Errorf("ticket = %+v, want tenant-1/13800000099/1", tk)
	}

	// The critical property: SINGLE USE. A second consume of the same token
	// (a shared URL, a reconnect with the old URL) must fail.
	if again := s.Consume(tok); again != nil {
		t.Errorf("second consume of the same token = %+v, want nil", again)
	}
}

func TestTicketStore_ConsumeUnknownAndEmpty(t *testing.T) {
	s := NewTicketStore()
	if tk := s.Consume("this-token-never-existed"); tk != nil {
		t.Errorf("consume of unknown token = %+v, want nil", tk)
	}
	// Empty token: what arrives when the URL has no ?token= or an empty
	// query string -- same silent rejection, no special branch.
	if tk := s.Consume(""); tk != nil {
		t.Errorf("consume of empty token = %+v, want nil", tk)
	}
}

func TestTicketStore_ExpiredTicketNotUsable(t *testing.T) {
	s := NewTicketStore()
	tok, err := s.Mint("tenant-1", "13800000099", testApp, 1)
	if err != nil {
		t.Fatalf("mint: %v", err)
	}

	// Expire the ticket by hand (same package) instead of waiting 90s of
	// wall clock -- the behavior under test is "a ticket past ExpiresAt does
	// not work", not the passing of time.
	s.mu.Lock()
	e := s.tickets[tok]
	e.ExpiresAt = time.Now().Add(-time.Second)
	s.tickets[tok] = e
	s.mu.Unlock()

	if tk := s.Consume(tok); tk != nil {
		t.Errorf("consume of expired ticket = %+v, want nil", tk)
	}
	// It must also be gone from the map (popped even if expired) -- no
	// second chance for anyone.
	s.mu.Lock()
	_, stillThere := s.tickets[tok]
	s.mu.Unlock()
	if stillThere {
		t.Error("expired ticket still in the map after consume")
	}
}

func TestTicketStore_MintEvictsExpired(t *testing.T) {
	s := NewTicketStore()
	old, err := s.Mint("tenant-1", "13800000099", testApp, 1)
	if err != nil {
		t.Fatalf("mint: %v", err)
	}
	s.mu.Lock()
	e := s.tickets[old]
	e.ExpiresAt = time.Now().Add(-time.Minute)
	s.tickets[old] = e
	s.mu.Unlock()

	// A later mint sweeps expired tickets -- the map does not grow without
	// bound with tickets nobody consumed (video requested, player never
	// opened).
	if _, err := s.Mint("tenant-1", "13800000099", testApp, 2); err != nil {
		t.Fatalf("second mint: %v", err)
	}
	s.mu.Lock()
	_, stillThere := s.tickets[old]
	s.mu.Unlock()
	if stillThere {
		t.Error("expired ticket was not swept by the next mint")
	}
}

func TestTicketStore_TokensAreUnique(t *testing.T) {
	s := NewTicketStore()
	seen := make(map[string]bool)
	for i := 0; i < 100; i++ {
		tok, err := s.Mint("tenant-1", "13800000099", testApp, 1)
		if err != nil {
			t.Fatalf("mint #%d: %v", i, err)
		}
		if seen[tok] {
			t.Fatalf("repeated token at iteration #%d -- crypto/rand should not collide", i)
		}
		seen[tok] = true
	}
}
