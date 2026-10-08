package videobridge

import (
	"context"
	"log"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
)

// LiveMeter is the CENTRAL live-view time meter -- the single place that
// decides how much viewing time a tenant consumes and when it runs out,
// regardless of protocol (JT1078, GT06/RTMP, or a future one).
//
// Why it exists: previously each stream recorded its time ONCE at the end,
// so while several sessions were open the monthly balance reflected none of
// them -- with two cameras open the balance dropped as if there were only
// one, and the quota check when opening another camera (and the periodic
// recheck) saw the balance from before they were opened. Time was also
// billed for streams nobody asked to watch (the second camera
// "RTMP,ON,INOUT#" turns on by itself, or a video start used for a fallback
// preview photo).
//
// Design:
//   - Only BILLABLE sessions count: ones a viewer really requested
//     (RequestVideo, not an Auto start). Each open camera is its own session:
//     two cameras consume twice as fast.
//   - Checkpoints: every checkpointEvery, the elapsed slice of each session is
//     written to usage_events. usage_events remains the ONLY ledger (billing
//     and quota read from it), the balance the API sees lags by at most one
//     checkpoint, and a process crash loses at most that slice (resilience
//     with no new infrastructure).
//   - Balance = quota - consumed in DB - not-yet-written slice of open
//     sessions. Used when requesting video (TenantVideoLimits) and in the
//     enforcement tick.
//   - Enforcement: a single goroutine checks the tenants with open sessions
//     every tickEvery (cost O(active sessions), independent of fleet size)
//     and, at zero, cuts ALL of the tenant's sessions. Quota data is cached
//     per tenant (refreshEvery) to avoid querying Postgres on every tick.
//   - Graceful shutdown: Close writes the pending slice of every session
//     before exiting.
type LiveMeter struct {
	pool *pgxpool.Pool

	mu       sync.Mutex
	nextID   uint64
	sessions map[uint64]*meterSession
	tenants  map[uuid.UUID]*tenantQuota

	tickEvery       time.Duration
	checkpointEvery time.Duration
	refreshEvery    time.Duration
	now             func() time.Time

	// flush is replaced in tests (no Postgres).
	flush func(ctx context.Context, s meterSession, seconds int) error
	// loadQuota is replaced in tests.
	loadQuota func(ctx context.Context, tenantID uuid.UUID) (quota, consumed int, err error)

	stopOnce sync.Once
	stop     chan struct{}
	done     chan struct{}
}

type meterSession struct {
	id        uint64
	tenantID  uuid.UUID
	deviceID  uuid.UUID
	deviceKey string
	channel   uint8
	flushedTo time.Time // how far it is already written to usage_events
	cut       func(reason string)
	cutSent   bool
}

type tenantQuota struct {
	quota    int
	consumed int // what is already in the DB (updated locally on each flush)
	loadedAt time.Time
}

// MeterHandle identifies an open session in the meter. The zero value is not
// a session (Stop on it is a no-op).
type MeterHandle uint64

const (
	defaultMeterTick       = 2 * time.Second
	defaultMeterCheckpoint = 30 * time.Second
	defaultMeterRefresh    = 60 * time.Second
)

func NewLiveMeter(pool *pgxpool.Pool) *LiveMeter {
	m := &LiveMeter{
		pool:            pool,
		sessions:        make(map[uint64]*meterSession),
		tenants:         make(map[uuid.UUID]*tenantQuota),
		tickEvery:       defaultMeterTick,
		checkpointEvery: defaultMeterCheckpoint,
		refreshEvery:    defaultMeterRefresh,
		now:             time.Now,
		stop:            make(chan struct{}),
		done:            make(chan struct{}),
	}
	m.flush = m.flushToDB
	m.loadQuota = m.loadQuotaFromDB
	return m
}

// Start opens a billable session. cut is called (once, outside any lock) if
// the tenant's balance runs out while the session is still open. Nil-safe:
// on a nil meter it returns the zero handle.
func (m *LiveMeter) Start(tenantID, deviceID uuid.UUID, deviceKey string, channel uint8, cut func(reason string)) MeterHandle {
	if m == nil {
		return 0
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.nextID++
	m.sessions[m.nextID] = &meterSession{
		id: m.nextID, tenantID: tenantID, deviceID: deviceID, deviceKey: deviceKey,
		channel: channel, flushedTo: m.now(), cut: cut,
	}
	return MeterHandle(m.nextID)
}

// StartFor resolves deviceKey -> device/tenant with the given Protocol and
// opens the session. If the lookup fails the session is not opened (it is
// logged): a meter error never blocks video.
func (m *LiveMeter) StartFor(ctx context.Context, proto Protocol, deviceKey string, channel uint8, cut func(reason string)) MeterHandle {
	if m == nil || m.pool == nil {
		return 0
	}
	var tenantID, deviceID uuid.UUID
	err := db.WithBypass(ctx, m.pool, func(ctx context.Context, tx pgx.Tx) error {
		dev, err := proto.LookupDevice(ctx, tx, deviceKey)
		if err != nil {
			return err
		}
		tenantID, deviceID = dev.TenantID, dev.ID
		return nil
	})
	if err != nil {
		log.Printf("videobridge: meter: could not open session for %s channel %d: %v", deviceKey, channel, err)
		return 0
	}
	return m.Start(tenantID, deviceID, deviceKey, channel, cut)
}

// Stop closes the session and writes its last slice. Idempotent.
func (m *LiveMeter) Stop(h MeterHandle) {
	if m == nil || h == 0 {
		return
	}
	m.mu.Lock()
	s, ok := m.sessions[uint64(h)]
	if !ok {
		m.mu.Unlock()
		return
	}
	delete(m.sessions, uint64(h))
	now := m.now()
	seconds := int(now.Sub(s.flushedTo).Seconds() + 0.5)
	snap := *s
	m.mu.Unlock()
	if seconds > 0 {
		m.writeTramo(snap, seconds)
	}
}

// unflushedLocked sums the not-yet-written seconds of the tenant's sessions.
// Call with m.mu held.
func (m *LiveMeter) unflushedLocked(tenantID uuid.UUID, now time.Time) (seconds int, active int) {
	var total float64
	for _, s := range m.sessions {
		if s.tenantID == tenantID {
			total += now.Sub(s.flushedTo).Seconds()
			active++
		}
	}
	return int(total + 0.5), active
}

// Unflushed returns the seconds consumed by open sessions that are not yet
// in usage_events, and how many sessions are open.
func (m *LiveMeter) Unflushed(tenantID uuid.UUID) (seconds int, active int) {
	if m == nil {
		return 0, 0
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.unflushedLocked(tenantID, m.now())
}

// Run performs enforcement and checkpoints until Close. Call once.
func (m *LiveMeter) Run() {
	defer close(m.done)
	tick := time.NewTicker(m.tickEvery)
	defer tick.Stop()
	lastCheckpoint := m.now()
	for {
		select {
		case <-m.stop:
			m.checkpoint(true)
			return
		case <-tick.C:
			if m.now().Sub(lastCheckpoint) >= m.checkpointEvery {
				m.checkpoint(false)
				lastCheckpoint = m.now()
			}
			m.enforce()
		}
	}
}

// Close writes the pending slice of every session and stops Run. Waits at
// most timeout.
func (m *LiveMeter) Close(timeout time.Duration) {
	if m == nil {
		return
	}
	m.stopOnce.Do(func() { close(m.stop) })
	select {
	case <-m.done:
	case <-time.After(timeout):
		log.Printf("videobridge: meter: shutdown did not finish within %s", timeout)
	}
}

// checkpoint writes the elapsed slice of every open session.
func (m *LiveMeter) checkpoint(final bool) {
	type item struct {
		s       meterSession
		seconds int
	}
	m.mu.Lock()
	now := m.now()
	var items []item
	for _, s := range m.sessions {
		// Whole seconds only: the remainder carries over to the next slice,
		// so nothing is over-rounded or lost between checkpoints.
		whole := int(now.Sub(s.flushedTo).Seconds())
		if final {
			whole = int(now.Sub(s.flushedTo).Seconds() + 0.5)
		}
		if whole <= 0 {
			continue
		}
		s.flushedTo = s.flushedTo.Add(time.Duration(whole) * time.Second)
		items = append(items, item{s: *s, seconds: whole})
	}
	m.mu.Unlock()
	for _, it := range items {
		m.writeTramo(it.s, it.seconds)
	}
}

func (m *LiveMeter) writeTramo(s meterSession, seconds int) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	writeStart := m.now()
	if err := m.flush(ctx, s, seconds); err != nil {
		log.Printf("videobridge: meter: could not record %ds of live video for %s channel %d: %v", seconds, s.deviceKey, s.channel, err)
		return
	}
	m.mu.Lock()
	// If the quota was reloaded from the DB AFTER this write started, it
	// already includes this slice: do not add it twice.
	if tq, ok := m.tenants[s.tenantID]; ok && tq.loadedAt.Before(writeStart) {
		tq.consumed += seconds
	}
	m.mu.Unlock()
}

// enforce cuts every session of a tenant whose balance reached zero.
func (m *LiveMeter) enforce() {
	m.mu.Lock()
	tenants := map[uuid.UUID]bool{}
	for _, s := range m.sessions {
		tenants[s.tenantID] = true
	}
	m.mu.Unlock()

	for tenantID := range tenants {
		tq, err := m.tenantQuota(tenantID)
		if err != nil {
			log.Printf("videobridge: meter: could not read quota for tenant %s (will retry): %v", tenantID, err)
			continue
		}
		m.mu.Lock()
		unflushed, active := m.unflushedLocked(tenantID, m.now())
		remaining := tq.quota - tq.consumed - unflushed
		// Cut half a tick before zero (scaled by open cameras) so the error
		// stays within +/- half a tick per camera instead of overshooting by
		// up to a full tick (measured: 3 s over with two cameras and a 2 s
		// tick without this adjustment).
		margin := int(float64(active) * m.tickEvery.Seconds() / 2)
		var cuts []func(string)
		if remaining <= margin {
			for _, s := range m.sessions {
				if s.tenantID == tenantID && !s.cutSent && s.cut != nil {
					s.cutSent = true
					cuts = append(cuts, s.cut)
				}
			}
		}
		m.mu.Unlock()
		for _, cut := range cuts {
			cut("monthly live-view time exhausted")
		}
	}
}

// tenantQuota returns the cached quota, reloading it from the DB every
// refreshEvery (picks up minutes added manually by the platform).
func (m *LiveMeter) tenantQuota(tenantID uuid.UUID) (tenantQuota, error) {
	m.mu.Lock()
	tq, ok := m.tenants[tenantID]
	fresh := ok && m.now().Sub(tq.loadedAt) < m.refreshEvery
	var cached tenantQuota
	if ok {
		cached = *tq
	}
	m.mu.Unlock()
	if fresh {
		return cached, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	quota, consumed, err := m.loadQuota(ctx, tenantID)
	if err != nil {
		if ok {
			return cached, nil // stale data is better than not cutting at all
		}
		return tenantQuota{}, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	nt := &tenantQuota{quota: quota, consumed: consumed, loadedAt: m.now()}
	m.tenants[tenantID] = nt
	// Forget tenants with no sessions so the map does not grow without bound.
	for id := range m.tenants {
		if id == tenantID {
			continue
		}
		if _, active := m.unflushedLocked(id, m.now()); active == 0 {
			delete(m.tenants, id)
		}
	}
	return *nt, nil
}

// Balance returns a tenant's real balance (fresh DB read minus what is not
// yet written) and how many sessions it has open.
func (m *LiveMeter) Balance(ctx context.Context, tenantID uuid.UUID) (remaining int, active int, err error) {
	quota, consumed, err := m.loadQuota(ctx, tenantID)
	if err != nil {
		return 0, 0, err
	}
	unflushed, active := m.Unflushed(tenantID)
	return quota - consumed - unflushed, active, nil
}

func (m *LiveMeter) loadQuotaFromDB(ctx context.Context, tenantID uuid.UUID) (quota, consumed int, err error) {
	err = db.WithBypass(ctx, m.pool, func(ctx context.Context, tx pgx.Tx) error {
		var err error
		if quota, err = db.GetTenantLiveViewQuotaSeconds(ctx, tx, tenantID); err != nil {
			return err
		}
		consumed, err = db.GetTenantLiveViewSecondsConsumedThisMonth(ctx, tx, tenantID)
		return err
	})
	return quota, consumed, err
}

func (m *LiveMeter) flushToDB(ctx context.Context, s meterSession, seconds int) error {
	return db.WithBypass(ctx, m.pool, func(ctx context.Context, tx pgx.Tx) error {
		return db.InsertUsageEvent(ctx, tx, s.tenantID, s.deviceID, "live_view", 0, map[string]any{
			"schema":     "rtc",
			"duration_s": seconds,
			"channel":    s.channel,
			// Slice measured by the central meter (checkpoint or session
			// close); a long session is several rows that add up.
			"source": "live_meter",
		})
	})
}
