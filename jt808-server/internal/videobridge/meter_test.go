package videobridge

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *fakeClock) now() time.Time { c.mu.Lock(); defer c.mu.Unlock(); return c.t }
func (c *fakeClock) add(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

type fakeLedger struct {
	mu     sync.Mutex
	rows   map[uuid.UUID]int // seconds written per tenant
	quota  map[uuid.UUID]int
	writes int
}

func newTestMeter(t *testing.T) (*LiveMeter, *fakeClock, *fakeLedger) {
	t.Helper()
	clock := &fakeClock{t: time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)}
	ledger := &fakeLedger{rows: map[uuid.UUID]int{}, quota: map[uuid.UUID]int{}}
	m := NewLiveMeter(nil)
	m.now = clock.now
	m.flush = func(_ context.Context, s meterSession, seconds int) error {
		ledger.mu.Lock()
		defer ledger.mu.Unlock()
		ledger.rows[s.tenantID] += seconds
		ledger.writes++
		return nil
	}
	m.loadQuota = func(_ context.Context, id uuid.UUID) (int, int, error) {
		ledger.mu.Lock()
		defer ledger.mu.Unlock()
		return ledger.quota[id], ledger.rows[id], nil
	}
	return m, clock, ledger
}

// Two open cameras consume double: the balance drops 2 s per second.
func TestMeter_TwoCamerasConsumeDouble(t *testing.T) {
	m, clock, ledger := newTestMeter(t)
	tenant := uuid.New()
	ledger.quota[tenant] = 600
	m.Start(tenant, uuid.New(), "imei", 0, nil)
	m.Start(tenant, uuid.New(), "imei", 1, nil)
	clock.add(30 * time.Second)

	remaining, active, err := m.Balance(context.Background(), tenant)
	if err != nil {
		t.Fatal(err)
	}
	if active != 2 || remaining != 600-60 {
		t.Fatalf("remaining=%d active=%d, want 540 and 2", remaining, active)
	}
}

// Checkpoints write the slice to the ledger without losing or duplicating
// seconds, and closing writes the remainder.
func TestMeter_CheckpointAndStopAreExact(t *testing.T) {
	m, clock, ledger := newTestMeter(t)
	tenant := uuid.New()
	ledger.quota[tenant] = 10_000
	h := m.Start(tenant, uuid.New(), "t", 1, nil)

	clock.add(30*time.Second + 400*time.Millisecond)
	m.checkpoint(false)
	if ledger.rows[tenant] != 30 {
		t.Fatalf("after checkpoint = %d, want 30 (whole seconds only)", ledger.rows[tenant])
	}
	clock.add(12*time.Second + 200*time.Millisecond)
	m.Stop(h)
	if ledger.rows[tenant] != 43 { // 30 + 0.4 + 12.2 = 42.6 -> 43 on close
		t.Fatalf("total = %d, want 43", ledger.rows[tenant])
	}
	m.Stop(h) // idempotent
	if u, a := m.Unflushed(tenant); u != 0 || a != 0 {
		t.Fatalf("something left open: %d s, %d sessions", u, a)
	}
}

// When the balance runs out ALL of the tenant's sessions are cut (once each)
// and never another tenant's.
func TestMeter_EnforceCutsAllSessionsOfTenant(t *testing.T) {
	m, clock, ledger := newTestMeter(t)
	a, b := uuid.New(), uuid.New()
	ledger.quota[a] = 20
	ledger.quota[b] = 10_000
	var mu sync.Mutex
	cuts := map[string]int{}
	cut := func(name string) func(string) {
		return func(string) { mu.Lock(); cuts[name]++; mu.Unlock() }
	}
	m.Start(a, uuid.New(), "a", 0, cut("a0"))
	m.Start(a, uuid.New(), "a", 1, cut("a1"))
	m.Start(b, uuid.New(), "b", 0, cut("b0"))

	clock.add(8 * time.Second) // a: 16 s of 20 consumed (margin: 2 cameras x 1 s)
	m.enforce()
	if len(cuts) != 0 {
		t.Fatalf("cut too early: %v", cuts)
	}
	clock.add(1 * time.Second) // a: 18 s, within the half-tick-per-camera margin
	m.enforce()
	m.enforce()
	if cuts["a0"] != 1 || cuts["a1"] != 1 || cuts["b0"] != 0 {
		t.Fatalf("cuts = %v, want a0=1 a1=1 b0=0", cuts)
	}
}

// Minutes added manually are picked up on the next quota reload.
func TestMeter_PicksUpQuotaIncrease(t *testing.T) {
	m, clock, ledger := newTestMeter(t)
	tenant := uuid.New()
	ledger.quota[tenant] = 5
	fired := 0
	m.Start(tenant, uuid.New(), "x", 0, func(string) { fired++ })
	m.enforce() // loads the quota (5)
	ledger.quota[tenant] = 3600
	clock.add(defaultMeterRefresh + time.Second) // 61 s consumed, but the reload sees 3600
	m.enforce()
	if fired != 0 {
		t.Fatalf("cut even though minutes were added")
	}
}

// A nil meter (tests in other packages) breaks nothing.
func TestMeter_NilSafe(t *testing.T) {
	var m *LiveMeter
	h := m.Start(uuid.New(), uuid.New(), "k", 0, nil)
	m.Stop(h)
	if u, a := m.Unflushed(uuid.New()); u != 0 || a != 0 {
		t.Fatal("nil must return zero")
	}
}

// Run writes the pending slice on close.
func TestMeter_CloseFlushesPending(t *testing.T) {
	m, clock, ledger := newTestMeter(t)
	m.tickEvery = time.Hour
	tenant := uuid.New()
	m.Start(tenant, uuid.New(), "k", 0, nil)
	go m.Run()
	clock.add(7 * time.Second)
	m.Close(2 * time.Second)
	ledger.mu.Lock()
	defer ledger.mu.Unlock()
	if ledger.rows[tenant] != 7 {
		t.Fatalf("on close %d s were written, want 7", ledger.rows[tenant])
	}
}
