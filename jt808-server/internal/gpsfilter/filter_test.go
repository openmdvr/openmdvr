package gpsfilter

import (
	"math"
	"math/rand"
	"testing"
	"time"

	"github.com/google/uuid"
)

// Reference point for the tests.
const baseLat, baseLon = 32.49910, -116.92128

var t0 = time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)

func boolp(b bool) *bool { return &b }

// offset moves (lat,lon) by dist meters along the given bearing (degrees).
func offset(lat, lon, dist, bearing float64) (float64, float64) {
	rad := math.Pi / 180
	dLat := dist * math.Cos(bearing*rad) / 111320
	dLon := dist * math.Sin(bearing*rad) / (111320 * math.Cos(lat*rad))
	return lat + dLat, lon + dLon
}

func sample(t time.Time, lat, lon, speed float64, ign *bool, sats int) Sample {
	return Sample{Time: t, Lat: lat, Lon: lon, SpeedKmh: f32(speed), Heading: f32(90), Satellites: sats, Ignition: ign}
}

type run struct {
	t      *testing.T
	f      *Filter
	id     uuid.UUID
	stored []Position
	holds  int
	drops  int
}

func newRun(t *testing.T) *run {
	return &run{t: t, f: New(Defaults()), id: uuid.New()}
}

func (r *run) feed(s Sample) Decision {
	d := r.f.Process(r.id, s)
	r.stored = append(r.stored, d.Released...)
	switch d.Action {
	case Store:
		r.stored = append(r.stored, d.Position)
	case Hold:
		r.holds++
	case Drop:
		r.drops++
	}
	return d
}

// Realistic drift of a stationary unit: position wandering up to ~20 m and
// a reported speed of 0-9 km/h.
func driftSample(rng *rand.Rand, t time.Time, ign *bool) Sample {
	lat, lon := offset(baseLat, baseLon, rng.Float64()*20, rng.Float64()*360)
	return sample(t, lat, lon, rng.Float64()*9, ign, 5)
}

func TestStationaryDriftIsSnappedWithIgnitionOn(t *testing.T) {
	r := newRun(t)
	rng := rand.New(rand.NewSource(1))
	for i := 0; i < 90; i++ { // 30 min, every 20 s
		r.feed(driftSample(rng, t0.Add(time.Duration(i)*20*time.Second), boolp(true)))
	}
	// After the first minute (learning) no stored reading may have speed or
	// be farther from the real spot than the drift.
	for i, p := range r.stored {
		if p.Time.Before(t0.Add(90 * time.Second)) {
			continue
		}
		if p.SpeedKmh == nil || *p.SpeedKmh != 0 {
			t.Fatalf("reading %d stored with speed %v while stationary", i, *p.SpeedKmh)
		}
		if d := haversineM(baseLat, baseLon, p.Lat, p.Lon); d > 20 {
			t.Fatalf("reading %d stored %.1f m from the real spot", i, d)
		}
		if p.Raw == nil || p.Raw["src"] == nil {
			t.Fatalf("reading %d corrected without keeping the original in raw", i)
		}
	}
}

func TestStationaryDriftWithRandomOutliersNeverDeparts(t *testing.T) {
	r := newRun(t)
	rng := rand.New(rand.NewSource(7))
	for i := 0; i < 120; i++ {
		ts := t0.Add(time.Duration(i) * 20 * time.Second)
		s := driftSample(rng, ts, boolp(true))
		if i%5 == 4 { // 60-90 m outlier in a random direction
			s.Lat, s.Lon = offset(baseLat, baseLon, 60+rng.Float64()*30, rng.Float64()*360)
			s.SpeedKmh = f32(12)
		}
		r.feed(s)
	}
	for i, p := range r.stored {
		if p.Time.Before(t0.Add(90 * time.Second)) {
			continue
		}
		if d := haversineM(baseLat, baseLon, p.Lat, p.Lon); d > 25 {
			t.Fatalf("isolated outlier stored as real movement (reading %d at %.1f m)", i, d)
		}
	}
}

func TestRealDepartureIsDetectedQuickly(t *testing.T) {
	r := newRun(t)
	rng := rand.New(rand.NewSource(3))
	for i := 0; i < 10; i++ {
		r.feed(driftSample(rng, t0.Add(time.Duration(i)*20*time.Second), boolp(true)))
	}
	start := t0.Add(10 * 20 * time.Second)
	lat, lon := baseLat, baseLon
	for i := 1; i <= 10; i++ { // 40 km/h eastbound, one reading every 10 s
		lat, lon = offset(lat, lon, 40/3.6*10, 90)
		d := r.feed(sample(start.Add(time.Duration(i)*10*time.Second), lat, lon, 40, boolp(true), 8))
		if i >= 2 && (d.Action != Store || d.Position.Lat != lat || d.Position.Lon != lon) {
			t.Fatalf("reading %d of a real trip was not stored as-is (%+v)", i, d)
		}
	}
}

func TestSlowTrafficStaysMoving(t *testing.T) {
	r := newRun(t)
	lat, lon := baseLat, baseLon
	moved := 0
	for i := 0; i < 30; i++ { // 7 km/h traffic, one reading every 20 s
		d := r.feed(sample(t0.Add(time.Duration(i)*20*time.Second), lat, lon, 7, boolp(true), 8))
		if d.Action == Store && d.Position.Lat == lat && d.Position.Lon == lon {
			moved++
		}
		lat, lon = offset(lat, lon, 7/3.6*20, 0)
	}
	if moved < 25 {
		t.Fatalf("slow traffic: only %d/30 readings stored as-is (mistaken for drift)", moved)
	}
}

func TestTowingWithIgnitionOffIsStillDetected(t *testing.T) {
	r := newRun(t)
	rng := rand.New(rand.NewSource(5))
	for i := 0; i < 10; i++ {
		r.feed(driftSample(rng, t0.Add(time.Duration(i)*20*time.Second), boolp(false)))
	}
	start := t0.Add(10 * 20 * time.Second)
	lat, lon := baseLat, baseLon
	var last Decision
	for i := 1; i <= 8; i++ { // towed at 30 km/h, ignition off
		lat, lon = offset(lat, lon, 30/3.6*15, 45)
		last = r.feed(sample(start.Add(time.Duration(i)*15*time.Second), lat, lon, 30, boolp(false), 7))
	}
	if last.Action != Store || last.Position.Lat != lat {
		t.Fatalf("towing with ignition off was not detected as movement (%+v)", last)
	}
}

func TestMultipathSpikeIsDiscarded(t *testing.T) {
	r := newRun(t)
	rng := rand.New(rand.NewSource(9))
	for i := 0; i < 10; i++ {
		r.feed(driftSample(rng, t0.Add(time.Duration(i)*20*time.Second), boolp(true)))
	}
	sLat, sLon := offset(baseLat, baseLon, 2500, 200)
	if d := r.feed(sample(t0.Add(220*time.Second), sLat, sLon, 0, boolp(true), 4)); d.Action != Hold {
		t.Fatalf("a 2.5 km jump in 20 s should be held, got %v", d.Action)
	}
	r.feed(driftSample(rng, t0.Add(240*time.Second), boolp(true)))
	for _, p := range r.stored {
		if haversineM(baseLat, baseLon, p.Lat, p.Lon) > 100 {
			t.Fatalf("the 2.5 km outlier ended up stored")
		}
	}
}

func TestRealJumpAfterTunnelIsConfirmedAndReleased(t *testing.T) {
	r := newRun(t)
	lat, lon := baseLat, baseLon
	for i := 0; i < 5; i++ {
		lat, lon = offset(lat, lon, 80/3.6*10, 90)
		r.feed(sample(t0.Add(time.Duration(i)*10*time.Second), lat, lon, 80, boolp(true), 9))
	}
	// Exits the tunnel 3 km ahead, 30 s later (implies 360 km/h).
	jLat, jLon := offset(lat, lon, 3000, 90)
	if d := r.feed(sample(t0.Add(80*time.Second), jLat, jLon, 80, boolp(true), 9)); d.Action != Hold {
		t.Fatalf("the jump should be held first, got %v", d.Action)
	}
	nLat, nLon := offset(jLat, jLon, 80/3.6*10, 90)
	d := r.feed(sample(t0.Add(90*time.Second), nLat, nLon, 80, boolp(true), 9))
	if len(d.Released) != 1 || d.Released[0].Lat != jLat {
		t.Fatalf("the next reading confirmed the jump; it should be released (%+v)", d)
	}
	if d.Action != Store || d.Position.Lat != nLat {
		t.Fatalf("the confirming reading should be stored as-is (%+v)", d)
	}
}

func TestLateBufferedSampleIsStoredWithoutTouchingState(t *testing.T) {
	r := newRun(t)
	r.feed(sample(t0.Add(time.Minute), baseLat, baseLon, 0, boolp(false), 8))
	lat, lon := offset(baseLat, baseLon, 500, 10)
	d := r.feed(sample(t0, lat, lon, 30, boolp(true), 8))
	if d.Action != Store || d.Position.Lat != lat || d.Position.Raw["filter"] != "late" {
		t.Fatalf("a late (device-buffered) reading should be stored as-is (%+v)", d)
	}
	if d := r.feed(sample(t0.Add(time.Minute), baseLat, baseLon, 0, boolp(false), 8)); d.Action != Drop {
		t.Fatalf("an exact duplicate (same time) should be dropped, got %v", d.Action)
	}
}

func TestMovingNoiseSpeedIsZeroedButPositionKept(t *testing.T) {
	r := newRun(t)
	r.feed(sample(t0, baseLat, baseLon, 40, boolp(true), 8))
	lat, lon := offset(baseLat, baseLon, 5, 0)
	d := r.feed(sample(t0.Add(10*time.Second), lat, lon, 2, boolp(true), 8))
	if d.Position.Lat != lat || *d.Position.SpeedKmh != 0 || d.Position.Raw["filter"] != "speed_noise" {
		t.Fatalf("noise speed while moving: expected position kept and speed 0 (%+v)", d.Position)
	}
}

// Hard stop: traveling at 110 km/h, the next reading (60 s later) is
// already stopped ~1.2 km away. The reported speed (0) contradicts the
// displacement, so it is held -- but the following reading stays there and
// the real data is NOT lost.
func TestHardStopAfterHighwayIsNotLost(t *testing.T) {
	r := newRun(t)
	lat, lon := baseLat, baseLon
	for i := 0; i < 5; i++ {
		lat, lon = offset(lat, lon, 110/3.6*10, 0)
		r.feed(sample(t0.Add(time.Duration(i)*10*time.Second), lat, lon, 110, boolp(true), 9))
	}
	sLat, sLon := offset(lat, lon, 1200, 0)
	r.feed(sample(t0.Add(100*time.Second), sLat, sLon, 0, boolp(true), 9))
	r.feed(sample(t0.Add(120*time.Second), sLat, sLon, 0, boolp(true), 9))
	found := false
	for _, p := range r.stored {
		if haversineM(sLat, sLon, p.Lat, p.Lon) < 5 && p.Time.Equal(t0.Add(100*time.Second)) {
			found = true
		}
	}
	if !found {
		t.Fatal("the real hard-stop reading was lost")
	}
}

// Traveled without signal (or powered off) and reappears 20 km away,
// consistent with the elapsed time: accepted where it is, not snapped to
// the old anchor.
func TestRelocationAfterOfflineTripIsAccepted(t *testing.T) {
	r := newRun(t)
	rng := rand.New(rand.NewSource(11))
	for i := 0; i < 10; i++ {
		r.feed(driftSample(rng, t0.Add(time.Duration(i)*20*time.Second), boolp(false)))
	}
	fLat, fLon := offset(baseLat, baseLon, 20000, 120)
	d := r.feed(sample(t0.Add(40*time.Minute), fLat, fLon, 0, boolp(false), 8))
	if d.Action != Store || haversineM(fLat, fLon, d.Position.Lat, d.Position.Lon) > 1 {
		t.Fatalf("reappeared 20 km away after 40 min: should be stored where it is (%+v)", d)
	}
}

func TestStationaryDriftRobustAcrossSeeds(t *testing.T) {
	for seed := int64(100); seed < 125; seed++ {
		r := newRun(t)
		rng := rand.New(rand.NewSource(seed))
		ign := boolp(seed%2 == 0)
		for i := 0; i < 60; i++ {
			r.feed(driftSample(rng, t0.Add(time.Duration(i)*20*time.Second), ign))
		}
		for i, p := range r.stored {
			if p.Time.Before(t0.Add(90 * time.Second)) {
				continue
			}
			if *p.SpeedKmh != 0 || haversineM(baseLat, baseLon, p.Lat, p.Lon) > 20 {
				t.Fatalf("seed %d, reading %d: drift stored as movement (%.1f m, %v km/h)", seed, i, haversineM(baseLat, baseLon, p.Lat, p.Lon), *p.SpeedKmh)
			}
		}
	}
}

// --- Independence from the report rate: each scenario runs with devices
// reporting every 1 s, 10 s, 30 s and 300 s.

var rates = []time.Duration{time.Second, 10 * time.Second, 30 * time.Second, 300 * time.Second}

// CORRELATED drift (slow random walk, like a real high-rate receiver)
// bounded to ~20 m, junk speed 0-9 km/h.
type walkDrift struct {
	rng    *rand.Rand
	dx, dy float64
}

func (w *walkDrift) next(t time.Time, ign *bool) Sample {
	w.dx += (w.rng.Float64() - 0.5) * 1.5
	w.dy += (w.rng.Float64() - 0.5) * 1.5
	if d := math.Hypot(w.dx, w.dy); d > 20 { // stays within the ~20 m cloud
		w.dx, w.dy = w.dx*20/d, w.dy*20/d
	}
	lat, lon := offset(baseLat, baseLon, w.dy, 0)
	lat, lon = offset(lat, lon, w.dx, 90)
	return sample(t, lat, lon, w.rng.Float64()*9, ign, 6)
}

func TestDriftSnappedAtAnyReportRate(t *testing.T) {
	for _, rate := range rates {
		for _, ign := range []bool{true, false} {
			r := newRun(t)
			w := &walkDrift{rng: rand.New(rand.NewSource(int64(rate)))}
			n := int(2*time.Hour/rate) + 3
			if n > 4000 {
				n = 4000
			}
			for i := 0; i < n; i++ {
				r.feed(w.next(t0.Add(time.Duration(i)*rate), boolp(ign)))
			}
			// After learning (the larger of 90 s and 2 readings).
			warm := t0.Add(maxDur(90*time.Second, 2*rate))
			for i, p := range r.stored {
				if p.Time.Before(warm) {
					continue
				}
				if *p.SpeedKmh != 0 || haversineM(baseLat, baseLon, p.Lat, p.Lon) > 20 {
					t.Fatalf("every %s, ignition=%v, reading %d: drift stored as movement (%.1f m, %v km/h)",
						rate, ign, i, haversineM(baseLat, baseLon, p.Lat, p.Lon), *p.SpeedKmh)
				}
			}
		}
	}
}

func TestRealTripKeptAtAnyReportRate(t *testing.T) {
	for _, rate := range rates {
		r := newRun(t)
		w := &walkDrift{rng: rand.New(rand.NewSource(99))}
		warmN := int(maxDur(5*time.Minute, 3*rate) / rate)
		for i := 0; i < warmN; i++ {
			r.feed(w.next(t0.Add(time.Duration(i)*rate), boolp(true)))
		}
		start := t0.Add(time.Duration(warmN) * rate)
		lat, lon := baseLat, baseLon
		kept, total := 0, 0
		for i := 1; i <= 12; i++ { // 45 km/h northbound
			lat, lon = offset(lat, lon, 45/3.6*rate.Seconds(), 0)
			d := r.feed(sample(start.Add(time.Duration(i)*rate), lat, lon, 45, boolp(true), 8))
			total++
			if d.Action == Store && d.Position.Lat == lat && d.Position.Lon == lon {
				kept++
			}
		}
		if kept < total-2 {
			t.Fatalf("every %s: real trip, only %d/%d readings stored as-is", rate, kept, total)
		}
	}
}

func TestParksAfterTripAtAnyReportRate(t *testing.T) {
	for _, rate := range rates {
		r := newRun(t)
		lat, lon := baseLat, baseLon
		i := 0
		for ; i < 10; i++ {
			lat, lon = offset(lat, lon, 50/3.6*rate.Seconds(), 90)
			r.feed(sample(t0.Add(time.Duration(i)*rate), lat, lon, 50, boolp(true), 8))
		}
		// Stops with the engine on; the GPS keeps wandering and
		// reporting 6 km/h.
		rng := rand.New(rand.NewSource(3))
		stopLat, stopLon := lat, lon
		stopAt := t0.Add(time.Duration(i) * rate)
		var last Decision
		for k := 0; k < int(maxDur(10*time.Minute, 6*rate)/rate); k++ {
			jLat, jLon := offset(stopLat, stopLon, rng.Float64()*12, rng.Float64()*360)
			last = r.feed(sample(stopAt.Add(time.Duration(k)*rate), jLat, jLon, 6, boolp(true), 8))
		}
		if *last.Position.SpeedKmh != 0 {
			t.Fatalf("every %s: after 10 min stopped with engine on, speed is still %v", rate, *last.Position.SpeedKmh)
		}
	}
}

func maxDur(a, b time.Duration) time.Duration {
	if a > b {
		return a
	}
	return b
}

// Slow-reporting device (every 5 min) with ignition off moving slowly
// (towed in traffic, reporting 5 km/h): departure evidence must not expire
// between readings. A fixed 3-minute expiry would reset it on every reading
// and the departure would never be confirmed.
func TestSlowReportingTowIsDetected(t *testing.T) {
	r := newRun(t)
	rate := 5 * time.Minute
	for i := 0; i < 4; i++ {
		r.feed(sample(t0.Add(time.Duration(i)*rate), baseLat, baseLon, 0, boolp(false), 8))
	}
	start := t0.Add(4 * rate)
	lat, lon := baseLat, baseLon
	var d Decision
	for i := 1; i <= 2; i++ {
		lat, lon = offset(lat, lon, 400, 30)
		d = r.feed(sample(start.Add(time.Duration(i)*rate), lat, lon, 5, boolp(false), 8))
	}
	if d.Action != Store || d.Position.Lat != lat {
		t.Fatalf("slow device moving with ignition off: departure should be confirmed by the 2nd reading (%+v)", d)
	}
}

// First reading after a server restart: with the last stored position as the
// seed, noise from a stationary GPS (10 km/h, 12 m from the stored point) is
// corrected instead of being taken as movement. Without the seed this caused
// false geofence-exit events.
func TestSeed_FirstReadingAfterRestartIsDrift(t *testing.T) {
	f := New(Defaults())
	id := uuid.New()
	base := time.Date(2026, 9, 25, 18, 0, 0, 0, time.UTC)
	f.Seed(id, base, 32.49900, -116.92030, 0)
	speed := float32(10)
	d := f.Process(id, Sample{Time: base.Add(30 * time.Minute), Lat: 32.49902 + 0.0001, Lon: -116.92028, SpeedKmh: &speed, Satellites: 5})
	if d.Action != Store || d.Position.SpeedKmh == nil || *d.Position.SpeedKmh != 0 {
		t.Fatalf("the noise reading should be stored at 0 km/h on the anchor: %+v", d)
	}
	if dist := haversineM(32.49900, -116.92030, d.Position.Lat, d.Position.Lon); dist > 10 { // the anchor is averaged with the noise cloud
		t.Fatalf("stored %.1f m from the known point, should stay on the anchor", dist)
	}
}

// Seed does not override the state of a unit the filter already knows.
func TestSeed_DoesNotOverrideKnownDevice(t *testing.T) {
	f := New(Defaults())
	id := uuid.New()
	base := time.Date(2026, 9, 25, 18, 0, 0, 0, time.UTC)
	zero := float32(0)
	f.Process(id, Sample{Time: base, Lat: 32.5, Lon: -116.9, SpeedKmh: &zero, Satellites: 8})
	f.Seed(id, base.Add(-time.Hour), 10, 10, 0)
	if !f.Known(id) {
		t.Fatal("the unit should remain known")
	}
	d := f.Process(id, Sample{Time: base.Add(10 * time.Second), Lat: 32.5, Lon: -116.9, SpeedKmh: &zero, Satellites: 8})
	if d.Action != Store || math.Abs(d.Position.Lat-32.5) > 0.001 {
		t.Fatalf("the stale seed overrode the real state: %+v", d)
	}
}

// With no history or seed, a slow first reading (typical noise) is taken as
// parked; a real departure is confirmed by the following readings.
func TestUnknown_SlowFirstReadingIsParkedThenRealDepartureConfirmed(t *testing.T) {
	f := New(Defaults())
	id := uuid.New()
	base := time.Date(2026, 9, 25, 18, 0, 0, 0, time.UTC)
	slow := float32(10)
	d := f.Process(id, Sample{Time: base, Lat: 32.5, Lon: -116.9, SpeedKmh: &slow, Satellites: 6})
	if d.Action != Store || *d.Position.SpeedKmh != 0 {
		t.Fatalf("a slow first reading should be parked: %+v", d)
	}
	fast := float32(40)
	moved := false
	for i := 1; i <= 4; i++ {
		d = f.Process(id, Sample{Time: base.Add(time.Duration(i*10) * time.Second), Lat: 32.5 + float64(i)*0.001, Lon: -116.9, SpeedKmh: &fast, Satellites: 8})
		if d.Action == Store && d.Position.SpeedKmh != nil && *d.Position.SpeedKmh > 30 {
			moved = true
			break
		}
	}
	if !moved {
		t.Fatal("a real departure should be confirmed within a few readings")
	}
}
