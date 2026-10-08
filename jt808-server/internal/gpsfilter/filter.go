// Package gpsfilter cleans GPS positions BEFORE they are stored, the same way
// for every protocol (JT808, GT06 and any future one): drift of a parked
// unit, impossible jumps from signal bounce (multipath), and creeping noise
// speed.
//
// Why it exists: a GPS sitting still in its box reported 7 km/h and a
// "dancing" position -- the receiver estimates position and speed even when
// the antenna has poor sky visibility, and that noise ended up as real
// movement: fake kilometers in reports, false geofence enters/exits,
// overspeed alarms, and "idling" instead of "stopped".
//
// Design -- deliberately NOT a couple of fixed thresholds:
//
//   - Per-unit state machine: PARKED or MOVING. A parked unit has an ANCHOR
//     (where it really is) and an uncertainty RADIUS. Readings inside the
//     radius are stored at the anchor with speed 0 (drift, not movement).
//   - The radius ADAPTS: it starts from signal quality (fewer satellites =
//     larger radius) and grows with the scatter actually observed for that
//     unit (EWMA of its readings' distance to the anchor), capped. A bad
//     antenna gets a larger radius than a good one.
//   - The anchor is REFINED: the mean of the drift cloud approaches the real
//     position as more readings arrive.
//   - Leaving "parked" requires accumulated EVIDENCE, not a single reading:
//     outside the radius, moving away in a consistent direction, with high
//     Doppler speed (the receiver's speed is fairly reliable when it really
//     moves), or very far from the anchor. With ignition OFF more evidence is
//     required (drift is more likely) but it is still accepted -- a tow truck
//     or a theft is still detected.
//   - Entering "parked" is decided by real sustained DISPLACEMENT (readings
//     clustered for one minute), not by the receiver's speed -- drift can
//     report 5-15 km/h while still -- or by ignition off at low speed.
//   - INCOHERENT jumps (physically impossible implied speed, or contradicting
//     the receiver's own speed: it says 0 km/h but the position implies
//     200 km/h) are HELD, not dropped outright: if the next reading continues
//     from the new place (leaving a tunnel), both are stored; if it returns to
//     the previous place, the jump was a bounce and is dropped. If the unit
//     reappears far away coherently (it travelled without signal), it is
//     accepted.
//   - Independent of reporting FREQUENCY: decisions use distances and speeds
//     computed over the real time between readings, and anything
//     time-dependent scales with the reporting interval the filter learns for
//     EACH unit (EWMA): departure evidence expires after max(3 min,
//     3 intervals) and the "is it stopped?" window is bounded by time, not by
//     count. Tested with devices at 1 s, 10 s, 30 s and 300 s.
//   - None of this deletes the real data: every modified reading keeps the
//     original in gps_positions.raw (reported lat/lon/speed, the reason, the
//     distance to the anchor and the radius used), for auditing or
//     reprocessing with different criteria.
//   - Late readings (the device flushing its buffer after a signal gap) are
//     stored as is, without moving the state -- they are real history.
//
// State lives in memory per unit: each unit talks to ONE process over one TCP
// connection, so it need not be shared. If the process restarts, the unit is
// re-seeded from its last stored position (see Seed) or relearns its state in
// 1-2 readings. Units that stop reporting are forgotten automatically.
package gpsfilter

import (
	"math"
	"sync"
	"time"

	"github.com/google/uuid"
)

// Sample is a GPS reading as received from the device.
type Sample struct {
	Time     time.Time
	Lat      float64
	Lon      float64
	SpeedKmh *float32
	Heading  *float32
	// Satellites: satellites used in the fix; < 0 = the protocol does not
	// report it.
	Satellites int
	// Ignition: nil = unknown.
	Ignition *bool
}

// Action is what the caller must do with the reading.
type Action int

const (
	// Store: store Decision.Position (may be corrected).
	Store Action = iota
	// Hold: do not store yet -- an impossible jump pending confirmation by
	// the next reading.
	Hold
	// Drop: do not store (exact duplicate or discarded bounce).
	Drop
)

// Position is what finally gets stored.
type Position struct {
	Time     time.Time
	Lat      float64
	Lon      float64
	SpeedKmh *float32
	Heading  *float32
	// Raw goes to gps_positions.raw: nil if the reading was stored as
	// received (the normal case, no extra space).
	Raw map[string]any
}

// Decision is the result of Process.
type Decision struct {
	Action Action
	// Position is only valid with Action == Store.
	Position Position
	// Released: readings that were held and are now confirmed real -- the
	// caller must store them BEFORE Position (they are older).
	Released []Position
	// Reason: human-readable reason for logs (empty in the normal case).
	Reason string
}

// Config holds the filter parameters; Defaults() is what production uses.
// Exposed so tests (and future tuning against real data) need not touch the
// logic.
type Config struct {
	// Implied speed above which two readings cannot both be real (km/h), and
	// the minimum distance to consider it a jump.
	MaxPlausibleSpeedKmh float64
	MinJumpDistanceM     float64

	// Uncertainty radius of a parked unit (meters).
	MinRadiusM float64
	MaxRadiusM float64
	// Multiplier of the observed scatter for the radius.
	ScatterFactor float64

	// Speed below which the unit counts as "stopped" (km/h).
	StopSpeedKmh float64
	// Speed below which, while moving, 0 is reported (creeping / traffic
	// light noise).
	NoiseSpeedKmh float64
	// Doppler speed that on its own is strong evidence of movement.
	StrongSpeedKmh float64
	// Sustained time with clustered readings (low or doubtful speed) to go
	// from "moving" to "parked".
	ParkAfter time.Duration

	// Evidence needed to leave "parked", depending on ignition.
	EvidenceToMove       int
	EvidenceToMoveIgnOff int
	// A movement-candidate reading expires if more than this (or 3 of the
	// unit's reporting intervals, whichever is greater) passes without
	// another one confirming it.
	CandidateExpiry time.Duration
	// Minimum rate (m/s) at which distance from the anchor must grow to
	// count as a consistent departure (see parked()).
	MinDepartureMps float64
	// Cap on readings in the "is it stopped?" window (memory only; the
	// window is bounded by TIME, not count, so it works the same for a
	// device reporting every 1 s and one every 5 min).
	MaxWindowSamples int

	// Units without readings for longer than this are forgotten (bounded
	// memory).
	ForgetAfter time.Duration
}

// Defaults returns the production configuration.
func Defaults() Config {
	return Config{
		MaxPlausibleSpeedKmh: 250,
		MinJumpDistanceM:     300,
		MinRadiusM:           15,
		MaxRadiusM:           80,
		ScatterFactor:        2.5,
		StopSpeedKmh:         5,
		NoiseSpeedKmh:        3,
		StrongSpeedKmh:       20,
		ParkAfter:            60 * time.Second,
		EvidenceToMove:       2,
		EvidenceToMoveIgnOff: 3,
		CandidateExpiry:      3 * time.Minute,
		MaxWindowSamples:     600,
		MinDepartureMps:      0.5,
		ForgetAfter:          6 * time.Hour,
	}
}

type mode int

const (
	modeUnknown mode = iota
	modeParked
	modeMoving
)

type point struct {
	t        time.Time
	lat, lon float64
	speed    float64
}

type deviceState struct {
	mode mode

	// Last STORED reading (reference for jumps and late readings).
	last        point
	hasLast     bool
	lastHeading *float32

	// Parked.
	anchorLat, anchorLon float64
	anchorN              int
	scatterM             float64 // EWMA of the readings' distance to the anchor

	// Evidence for leaving "parked".
	evidence int
	cand     *point

	// Moving: recent readings at low or doubtful speed.
	recent []point

	// Held impossible jump.
	held *heldSample

	// intervalS: THIS unit's typical reporting interval (EWMA, seconds).
	// Each device reports at its own pace (1 s, 10 s, 5 min...) and anything
	// time-dependent scales with it.
	intervalS float64

	lastUpdate time.Time
}

type heldSample struct {
	pos    Position
	sample Sample
}

// Filter is safe for concurrent use.
type Filter struct {
	cfg     Config
	mu      sync.Mutex
	devices map[uuid.UUID]*deviceState
	now     func() time.Time
	calls   int
}

// New creates a filter with the given configuration.
func New(cfg Config) *Filter {
	return &Filter{cfg: cfg, devices: make(map[uuid.UUID]*deviceState), now: time.Now}
}

// Default is the process-wide shared instance (all units of all protocols).
var Default = New(Defaults())

// Process decides what to do with a reading from unit deviceID.
func (f *Filter) Process(deviceID uuid.UUID, s Sample) Decision {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	if f.calls%1000 == 0 {
		f.forgetStale()
	}
	st, ok := f.devices[deviceID]
	if !ok {
		st = &deviceState{}
		f.devices[deviceID] = st
	}
	st.lastUpdate = f.now()
	return f.process(st, s)
}

// Known reports whether the filter already has state for this unit.
func (f *Filter) Known(deviceID uuid.UUID) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.devices[deviceID]
	return ok
}

// Seed gives the filter the last STORED position of a unit it does not know
// yet -- the first reading after a server restart (every deploy) or after
// ForgetAfter. Without memory, a first 10 km/h noise reading from a camera
// sitting still in an office was taken as movement, stored as is, and fired a
// critical "geofence exit". With the last stored position as the anchor, that
// reading falls inside the noise radius and is corrected like any other. It
// does nothing if the unit is already known.
func (f *Filter) Seed(deviceID uuid.UUID, t time.Time, lat, lon, speedKmh float64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.devices[deviceID]; ok {
		return
	}
	st := &deviceState{lastUpdate: f.now()}
	if speedKmh < f.cfg.StopSpeedKmh {
		f.park(st, lat, lon)
	} else {
		st.mode = modeMoving
	}
	st.last = point{t: t, lat: lat, lon: lon, speed: speedKmh}
	st.hasLast = true
	f.devices[deviceID] = st
}

func (f *Filter) forgetStale() {
	cutoff := f.now().Add(-f.cfg.ForgetAfter)
	for id, st := range f.devices {
		if st.lastUpdate.Before(cutoff) {
			delete(f.devices, id)
		}
	}
}

func speedOf(s Sample) float64 {
	if s.SpeedKmh == nil {
		return 0
	}
	return float64(*s.SpeedKmh)
}

func f32(v float64) *float32 {
	x := float32(v)
	return &x
}

func asIs(s Sample) Position {
	return Position{Time: s.Time, Lat: s.Lat, Lon: s.Lon, SpeedKmh: s.SpeedKmh, Heading: s.Heading}
}

func rawOf(s Sample, reason string) map[string]any {
	r := map[string]any{
		"filter": reason,
		"src": map[string]any{
			"lat":   s.Lat,
			"lon":   s.Lon,
			"speed": speedOf(s),
		},
	}
	if s.Satellites >= 0 {
		r["sats"] = s.Satellites
	}
	return r
}

func (f *Filter) process(st *deviceState, s Sample) Decision {
	// 1. Late or duplicate readings: real history (device buffer) or a
	//    resend. They do not move the state.
	if st.hasLast && !s.Time.After(st.last.t) {
		if s.Time.Equal(st.last.t) {
			return Decision{Action: Drop, Reason: "duplicate reading (same time)"}
		}
		p := asIs(s)
		p.Raw = map[string]any{"filter": "late"}
		return Decision{Action: Store, Position: p, Reason: "late reading, stored without affecting the state"}
	}

	// Learn the unit's reporting interval (gaps over 1 h are ignored: device
	// off, not its normal pace).
	if st.hasLast {
		if dt := s.Time.Sub(st.last.t).Seconds(); dt > 0 && dt < 3600 {
			if st.intervalS == 0 {
				st.intervalS = dt
			} else {
				st.intervalS = 0.8*st.intervalS + 0.2*dt
			}
		}
	}

	// 2. Incoherent jumps (signal bounce / multipath) -- hold until the next
	//    reading tells which of the two places is real. Never dropped
	//    outright: a real jump (leaving a tunnel, braking after a highway)
	//    confirms itself.
	var released []Position
	if st.held != nil {
		h := st.held
		st.held = nil
		dHeld := haversineM(h.sample.Lat, h.sample.Lon, s.Lat, s.Lon)
		dLast := haversineM(st.last.lat, st.last.lon, s.Lat, s.Lon)
		if dHeld < dLast && !f.suspicious(h.sample.Lat, h.sample.Lon, h.sample.Time, s) {
			// The new reading continues from the jump: it was real.
			hp := h.pos
			hp.Raw = rawOf(h.sample, "jump_confirmed")
			released = append(released, hp)
			f.resetAt(st, h.sample)
		}
		// Otherwise it returned to the previous place: the jump was a bounce
		// and is dropped (visible in the caller's log via Reason).
	}
	if st.hasLast && f.suspicious(st.last.lat, st.last.lon, st.last.t, s) {
		st.held = &heldSample{pos: asIs(s), sample: s}
		return Decision{Action: Hold, Released: released, Reason: "jump incoherent with the reported speed, held until confirmed"}
	}

	d := f.classify(st, s)
	d.Released = append(released, d.Released...)
	if d.Action == Store {
		st.last = point{t: d.Position.Time, lat: d.Position.Lat, lon: d.Position.Lon, speed: float64FromPtr(d.Position.SpeedKmh)}
		st.hasLast = true
		if s.Heading != nil && st.mode == modeMoving {
			st.lastHeading = s.Heading
		}
	}
	return d
}

func float64FromPtr(p *float32) float64 {
	if p == nil {
		return 0
	}
	return float64(*p)
}

// suspicious: is the jump from (lat,lon,t) to s incoherent? A short jump
// never is (that is drift, handled by classify). A long one is if it implies
// a physically impossible speed, or CONTRADICTS the receiver's own speed (it
// says 0 km/h but the position implies 200 km/h): that contradiction is the
// signature of signal bounce. The margin (2x + 60 km/h) covers real
// acceleration and braking between two readings, where instantaneous and
// average speed differ.
func (f *Filter) suspicious(lat, lon float64, t time.Time, s Sample) bool {
	d, implied := jumpFrom(lat, lon, t, s)
	if d <= f.cfg.MinJumpDistanceM {
		return false
	}
	return implied > f.cfg.MaxPlausibleSpeedKmh || implied > 2*speedOf(s)+60
}

func jumpFrom(lat, lon float64, t time.Time, s Sample) (distM, impliedKmh float64) {
	distM = haversineM(lat, lon, s.Lat, s.Lon)
	dt := s.Time.Sub(t).Seconds()
	if dt <= 0 {
		return distM, math.Inf(1)
	}
	return distM, distM / dt * 3.6
}

func (f *Filter) resetAt(st *deviceState, s Sample) {
	st.mode = modeUnknown
	st.anchorN = 0
	st.scatterM = 0
	st.evidence = 0
	st.cand = nil
	st.recent = nil
	st.last = point{t: s.Time, lat: s.Lat, lon: s.Lon, speed: speedOf(s)}
	st.hasLast = true
}

func (f *Filter) baseRadius(sats int) float64 {
	switch {
	case sats < 0:
		return 25
	case sats <= 4:
		return 40
	case sats <= 6:
		return 30
	default:
		return 20
	}
}

func (f *Filter) radius(st *deviceState, sats int) float64 {
	r := math.Max(f.baseRadius(sats), f.cfg.ScatterFactor*st.scatterM)
	return math.Min(math.Max(r, f.cfg.MinRadiusM), f.cfg.MaxRadiusM)
}

func (f *Filter) park(st *deviceState, lat, lon float64) {
	st.mode = modeParked
	st.anchorLat, st.anchorLon = lat, lon
	st.anchorN = 1
	st.scatterM = 0
	st.evidence = 0
	st.cand = nil
	st.recent = nil
}

func (f *Filter) classify(st *deviceState, s Sample) Decision {
	speed := speedOf(s)
	ignOff := s.Ignition != nil && !*s.Ignition

	switch st.mode {
	case modeUnknown:
		// No history (new unit, no stored positions): a first reading below
		// StrongSpeedKmh is taken as parked -- noise from a stationary GPS
		// can report 10 km/h. If it really moves, the next one or two
		// readings confirm it (the same evidence as any departure).
		if speed < f.cfg.StrongSpeedKmh {
			f.park(st, s.Lat, s.Lon)
			p := asIs(s)
			if speed > 0 {
				p.SpeedKmh = f32(0)
				p.Raw = rawOf(s, "parked_initial")
			}
			return Decision{Action: Store, Position: p}
		}
		st.mode = modeMoving
		return f.moving(st, s, speed, ignOff)

	case modeParked:
		return f.parked(st, s, speed, ignOff)

	default:
		return f.moving(st, s, speed, ignOff)
	}
}

func (f *Filter) parked(st *deviceState, s Sample, speed float64, ignOff bool) Decision {
	r := f.radius(st, s.Satellites)
	dist := haversineM(st.anchorLat, st.anchorLon, s.Lat, s.Lon)

	candExpiry := f.cfg.CandidateExpiry
	if byRate := time.Duration(3 * st.intervalS * float64(time.Second)); byRate > candExpiry {
		candExpiry = byRate
	}
	if st.cand != nil && s.Time.Sub(st.cand.t) > candExpiry {
		st.cand = nil
		st.evidence = 0
	}

	// Reappears far away (more than any drift explains) without being
	// incoherent -- it already passed the jump filter: it moved while not
	// reporting (trip without signal, device off). Accepted as is.
	if dist > math.Max(1000, 10*r) {
		f.resetAt(st, s)
		f.park(st, s.Lat, s.Lon)
		if speed >= f.cfg.StopSpeedKmh {
			st.mode = modeMoving
		}
		p := asIs(s)
		p.Raw = rawOf(s, "relocated")
		p.Raw["dist_m"] = math.Round(dist)
		return Decision{Action: Store, Position: p, Reason: "reappeared elsewhere"}
	}

	if dist <= r && speed < f.cfg.StrongSpeedKmh {
		// Drift: inside the radius and without a convincing speed.
		if dist < r/2 {
			st.evidence = 0
			st.cand = nil
		}
		// Refine the anchor (cloud mean, decreasing weight with a floor) and
		// the observed scatter.
		w := math.Max(1/float64(st.anchorN+1), 0.05)
		st.anchorLat += (s.Lat - st.anchorLat) * w
		st.anchorLon += (s.Lon - st.anchorLon) * w
		st.anchorN++
		st.scatterM += (dist - st.scatterM) * 0.1
		return f.snap(st, s, "parked_drift", dist, r)
	}

	// Movement candidate: accumulate evidence.
	ev := 1
	if speed >= f.cfg.StrongSpeedKmh {
		ev++
	}
	if dist > 4*r {
		ev++
	}
	if st.cand != nil {
		prevDist := haversineM(st.anchorLat, st.anchorLon, st.cand.lat, st.cand.lon)
		sameDir := bearingDiff(bearing(st.anchorLat, st.anchorLon, st.cand.lat, st.cand.lon), bearing(st.anchorLat, st.anchorLon, s.Lat, s.Lon)) <= 60
		// Vehicle pace: distance must grow by at least MinDepartureMps. On a
		// device reporting every 1-2 s, drift does not jump randomly, it
		// WANDERS slowly in one direction (a correlated process) -- without
		// this check it would look like "moving away consistently". Any real
		// start is faster.
		dt := s.Time.Sub(st.cand.t).Seconds()
		fastEnough := dt > 0 && (dist-prevDist)/dt >= f.cfg.MinDepartureMps
		if sameDir && dist >= prevDist*0.8 && fastEnough {
			ev++ // moving away consistently, not bouncing around
		} else if !sameDir || dist < prevDist*0.8 {
			// Bounced the other way (typical drift): previous evidence does
			// not count, this reading starts from zero.
			st.evidence = 0
		}
	}
	st.evidence += ev
	need := f.cfg.EvidenceToMove
	if ignOff {
		need = f.cfg.EvidenceToMoveIgnOff
	}
	if st.evidence >= need {
		st.mode = modeMoving
		st.evidence = 0
		st.cand = nil
		st.recent = nil
		p := asIs(s)
		p.Raw = rawOf(s, "departure_confirmed")
		p.Raw["dist_m"] = math.Round(dist)
		p.Raw["radius_m"] = math.Round(r)
		return Decision{Action: Store, Position: p, Reason: "departure from parked confirmed"}
	}
	st.cand = &point{t: s.Time, lat: s.Lat, lon: s.Lon, speed: speed}
	return f.snap(st, s, "parked_candidate", dist, r)
}

func (f *Filter) snap(st *deviceState, s Sample, reason string, dist, r float64) Decision {
	raw := rawOf(s, reason)
	raw["dist_m"] = math.Round(dist)
	raw["radius_m"] = math.Round(r)
	return Decision{
		Action: Store,
		Position: Position{
			Time:     s.Time,
			Lat:      st.anchorLat,
			Lon:      st.anchorLon,
			SpeedKmh: f32(0),
			Heading:  st.lastHeading,
			Raw:      raw,
		},
	}
}

func (f *Filter) moving(st *deviceState, s Sample, speed float64, ignOff bool) Decision {
	p := asIs(s)
	if speed > 0 && speed < f.cfg.NoiseSpeedKmh {
		p.SpeedKmh = f32(0)
		p.Raw = rawOf(s, "speed_noise")
	}

	// Ignition off and (almost) stopped: parked now.
	if ignOff && speed < f.cfg.StopSpeedKmh {
		f.park(st, s.Lat, s.Lon)
		return Decision{Action: Store, Position: p}
	}

	// High Doppler speed: real movement, no doubt.
	if speed >= f.cfg.StrongSpeedKmh {
		st.recent = nil
		return Decision{Action: Store, Position: p}
	}

	// Low or doubtful speed (drift can report 5-15 km/h while still): decided
	// by real DISPLACEMENT. If readings stay clustered within the radius for
	// ParkAfter, the unit is stopped whatever the receiver says; real slow
	// traffic advances and does not stay clustered.
	st.recent = append(st.recent, point{t: s.Time, lat: s.Lat, lon: s.Lon, speed: speed})
	if len(st.recent) > f.cfg.MaxWindowSamples {
		st.recent = st.recent[len(st.recent)-f.cfg.MaxWindowSamples:]
	}
	if len(st.recent) >= 2 && s.Time.Sub(st.recent[0].t) >= f.cfg.ParkAfter {
		cLat, cLon := centroid(st.recent)
		r := f.radius(st, s.Satellites)
		grouped := true
		for _, q := range st.recent {
			if haversineM(cLat, cLon, q.lat, q.lon) > r {
				grouped = false
				break
			}
		}
		if grouped {
			f.park(st, cLat, cLon)
			return f.snap(st, s, "parked_detected", haversineM(cLat, cLon, s.Lat, s.Lon), r)
		}
		// It really moved: the window slides.
		for len(st.recent) > 1 && s.Time.Sub(st.recent[0].t) >= f.cfg.ParkAfter {
			st.recent = st.recent[1:]
		}
	}
	return Decision{Action: Store, Position: p}
}

func centroid(ps []point) (float64, float64) {
	var sumLat, sumLon float64
	for _, p := range ps {
		sumLat += p.lat
		sumLon += p.lon
	}
	n := float64(len(ps))
	return sumLat / n, sumLon / n
}

const earthRadiusM = 6371000.0

func haversineM(lat1, lon1, lat2, lon2 float64) float64 {
	rad := math.Pi / 180
	dLat := (lat2 - lat1) * rad
	dLon := (lon2 - lon1) * rad
	a := math.Sin(dLat/2)*math.Sin(dLat/2) + math.Cos(lat1*rad)*math.Cos(lat2*rad)*math.Sin(dLon/2)*math.Sin(dLon/2)
	return 2 * earthRadiusM * math.Asin(math.Min(1, math.Sqrt(a)))
}

func bearing(lat1, lon1, lat2, lon2 float64) float64 {
	rad := math.Pi / 180
	y := math.Sin((lon2-lon1)*rad) * math.Cos(lat2*rad)
	x := math.Cos(lat1*rad)*math.Sin(lat2*rad) - math.Sin(lat1*rad)*math.Cos(lat2*rad)*math.Cos((lon2-lon1)*rad)
	return math.Mod(math.Atan2(y, x)/rad+360, 360)
}

func bearingDiff(a, b float64) float64 {
	d := math.Abs(a - b)
	if d > 180 {
		d = 360 - d
	}
	return d
}
