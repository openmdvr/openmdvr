package gt06server

import "sync"

// Registry locates a device's active connSession by IMEI, so an external
// Dispatcher can find the device's open TCP connection and write an
// unsolicited frame to it (remote commands). Same pattern as
// internal/session.Registry (JT808/video) but a separate type to keep the
// blast radius minimal: the JT808 registry is NOT reused (connSession is a
// completely different type from session.Session).
type Registry struct {
	mu     sync.RWMutex
	byIMEI map[string]*connSession
}

func NewRegistry() *Registry {
	return &Registry{byIMEI: make(map[string]*connSession)}
}

// Register associates imei with sess, replacing any previous entry (a
// reconnect of the same device must point to the new connection).
func (r *Registry) Register(imei string, sess *connSession) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.byIMEI[imei] = sess
}

// Unregister removes the entry ONLY if it still points to sess, so closing an
// OLD connection never removes the entry of a NEW connection for the same
// IMEI (fast reconnect). Same rule as session.Registry.Unregister.
func (r *Registry) Unregister(imei string, sess *connSession) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.byIMEI[imei] == sess {
		delete(r.byIMEI, imei)
	}
}

func (r *Registry) Get(imei string) (*connSession, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	s, ok := r.byIMEI[imei]
	return s, ok
}
