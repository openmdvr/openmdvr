package videobridge

import (
	"strconv"
	"sync"
	"time"
)

// snapshotCacheTTL is how long the SAME captured photo is served before the
// device is asked for a new one. Reloading the page, opening the same device
// in another tab or device, or two operators watching the same camera must
// NOT trigger one real capture each (every real capture powers up the
// device's video pipeline for a few seconds -- the cost this feature exists
// to avoid). A var (not const) so tests can shrink it, same as
// snapshotWaitTimeout/snapshotPollInterval in snapshot.go.
//
// Deliberately SHORTER than the frontend auto-refresh interval
// (SNAPSHOT_INTERVAL_MS = 2 min, see CameraTile.tsx): a client's own
// scheduled refresh almost always finds the cache expired and gets a fresh
// photo; what is avoided is duplication across DIFFERENT viewers of the same
// device at about the same time.
var snapshotCacheTTL = 60 * time.Second

// snapshotCacheStore is an IN-MEMORY cache shared by ALL tenants/sessions
// requesting a device photo -- never per user. Authorization already happened
// in the API for each individual request; this cache only avoids repeating
// the PHYSICAL capture when the result would be the same. Memory is bounded
// by construction: each Set() overwrites the previous entry for the same
// device+channel, so the map size is bounded by the number of DISTINCT
// device+channels ever seen, not by request count -- no periodic purge
// needed even at thousands of devices.
type snapshotCacheStore struct {
	mu      sync.RWMutex
	entries map[string]snapshotCacheEntry
}

type snapshotCacheEntry struct {
	data       []byte
	capturedAt time.Time
}

func newSnapshotCacheStore() *snapshotCacheStore {
	return &snapshotCacheStore{entries: make(map[string]snapshotCacheEntry)}
}

func snapshotCacheKey(protocol, deviceKey string, channel uint8) string {
	return protocol + "|" + deviceKey + "|" + strconv.Itoa(int(channel))
}

// Get returns the cached bytes if present and still fresh (within
// snapshotCacheTTL). An expired entry is treated as absent; a stale photo is
// never served silently.
func (s *snapshotCacheStore) Get(protocol, deviceKey string, channel uint8) ([]byte, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	e, ok := s.entries[snapshotCacheKey(protocol, deviceKey, channel)]
	if !ok || time.Since(e.capturedAt) > snapshotCacheTTL {
		return nil, false
	}
	return e.data, true
}

// Set stores (or replaces) the latest real photo for this device+channel.
// Called ONLY after a successful real capture (see handleSnapshot in
// snapshot.go), never with ZLM's placeholder image.
func (s *snapshotCacheStore) Set(protocol, deviceKey string, channel uint8, data []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.entries[snapshotCacheKey(protocol, deviceKey, channel)] = snapshotCacheEntry{data: data, capturedAt: time.Now()}
}
