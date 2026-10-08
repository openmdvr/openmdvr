package jt1078bridge

import (
	"fmt"
	"sync"
	"time"
)

// pendingKey identifies an in-flight video request: a device can have
// several channels (cameras) requesting video at once, each with its own
// stream into ZLMediaKit.
type pendingKey struct {
	terminalID string
	channel    uint8
}

func streamID(terminalID string, channel uint8) string {
	return fmt.Sprintf("%s_%d", terminalID, channel)
}

type pendingEntry struct {
	streamID  string
	zlmPort   int
	createdAt time.Time
	// maxSeconds is the tenant's seconds limit (tenants.max_live_view_seconds,
	// already resolved by RequestVideo). It travels to relay.go to start the
	// cut-off timer exactly when the device REALLY starts sending video, not
	// when the 0x9101 is merely sent (the device may take a while to
	// connect).
	maxSeconds int
}

// pendingRequests remembers, between asking ZLMediaKit for a stream
// (openRtpServer) and the device actually connecting its video to our JT1078
// port, which ZLM stream_id/port that incoming connection belongs to. The
// only way to correlate them is the SIM+channel carried in the JT1078 packet
// itself (see relay.go).
type pendingRequests struct {
	mu      sync.Mutex
	entries map[pendingKey]pendingEntry
}

func newPendingRequests() *pendingRequests {
	return &pendingRequests{entries: make(map[pendingKey]pendingEntry)}
}

func (p *pendingRequests) put(terminalID string, channel uint8, e pendingEntry) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.entries[pendingKey{terminalID, channel}] = e
}

// take returns the pending entry and removes it -- each device JT1078
// connection consumes its own request; it is not reusable for a later
// reconnect (that must request a new 0x9101).
func (p *pendingRequests) take(terminalID string, channel uint8) (pendingEntry, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	key := pendingKey{terminalID, channel}
	e, ok := p.entries[key]
	if ok {
		delete(p.entries, key)
	}
	return e, ok
}

// sweepOlderThan clears requests that never connected (the device did not
// answer the 0x9101, or it never arrived) -- otherwise an orphan pending
// entry would live in memory forever.
func (p *pendingRequests) sweepOlderThan(d time.Duration) {
	p.mu.Lock()
	defer p.mu.Unlock()
	cutoff := time.Now().Add(-d)
	for k, e := range p.entries {
		if e.createdAt.Before(cutoff) {
			delete(p.entries, k)
		}
	}
}
