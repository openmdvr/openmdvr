package session

import "sync"

// Registry locates a device's active Session by TerminalID, so an external
// HTTP handler (e.g. a live video request) can find the device's open JT808
// connection and send it an active command (0x9101).
type Registry struct {
	mu    sync.RWMutex
	byTID map[string]*Session
}

func NewRegistry() *Registry {
	return &Registry{byTID: make(map[string]*Session)}
}

// Register associates terminalID with sess, replacing any previous entry (a
// reconnect of the same device must point to the new connection).
func (r *Registry) Register(terminalID string, sess *Session) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.byTID[terminalID] = sess
}

// Unregister removes the entry ONLY if it still points to sess, so closing an
// OLD connection never removes the entry of a NEW connection from the same
// device that already replaced it (fast reconnect).
func (r *Registry) Unregister(terminalID string, sess *Session) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.byTID[terminalID] == sess {
		delete(r.byTID, terminalID)
	}
}

func (r *Registry) Get(terminalID string) (*Session, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	s, ok := r.byTID[terminalID]
	return s, ok
}
