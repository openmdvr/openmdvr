package videobridge

import (
	"context"
	"sync"
)

// ActiveStreams remembers which stream_ids are currently receiving real
// video -- ONE map shared by ALL video protocols (keys never collide across
// protocols: each Protocol defines its own scheme, e.g. "<terminalID>_<ch>"
// for JT1078 or "gt06:<imei>:<ch>" for GT06). This matters in real
// scenarios:
//  1. A second viewer requests video for the same device/channel while the
//     first one is already watching.
//  2. ZLMediaKit fires on_stream_not_found in a legitimate race window right
//     after the push starts: its internal "stream ready to play" record can
//     lag its "source exists" record, and a player connecting in that
//     instant sees "not found" and fires the hook.
//
// Without this check (for JT1078), RequestVideo closed and reopened
// ZLMediaKit's RTP receiver and sent a new 0x9101 on every call, which kills
// the device's existing TCP connection (the old receiver is closed under it,
// "broken pipe" on the bridge side). Confirmed by reproducing the bug and
// reading the ZLMediaKit logs: "RtpProcess.cpp onDetach | 255(Server
// shutdown)" triggered by our own closeRtpServer call, itself triggered by
// on_stream_not_found, seconds into a push that was flowing normally. See
// jt808-server/README.md.
//
// Cancel: each active stream has a background timer that cuts it when the
// tenant's per-session limit expires. MarkInactive cancels that timer
// whenever the stream ends for ANY other reason (device disconnected,
// network error) -- otherwise the old timer could fire later and cut a NEW
// session reusing the same stream_id.
type ActiveStreams struct {
	mu      sync.Mutex
	streams map[string]ActiveEntry
}

type ActiveEntry struct {
	PlayURL    string
	MaxSeconds int
	Cancel     context.CancelFunc
	// Auto=true means THIS entry was created without any user explicitly
	// requesting it yet. Generic concept, not protocol-specific. Example: on
	// a dual-camera JC261, "RTMP,ON,INOUT#" starts both cameras at once, so
	// on_publish starts tracking the camera NOBODY asked for, with its
	// cut-off timer running from THAT instant. If the user asks for that
	// camera 20s later, they would get LESS time than their session allows
	// without having watched a second of it. The owning Protocol uses this
	// flag to "claim" the entry with a fresh timer on the first REAL request,
	// without sending any new command to the device (it is already
	// streaming) -- see ClaimAuto.
	Auto bool
	// State is an OPAQUE payload for videobridge, owned EXCLUSIVELY by the
	// Protocol that set it via MarkActiveAuto -- videobridge never inspects
	// it, only stores and returns it. GT06 stores its gt06StreamInfo there
	// (real RTMP app/stream, so it can restart the cut-off timer without
	// asking anyone again); JT1078 never uses it (nil) because it never
	// needs to claim its own entry -- one viewer = one channel per stream_id.
	State any
}

func NewActiveStreams() *ActiveStreams {
	return &ActiveStreams{streams: make(map[string]ActiveEntry)}
}

func (a *ActiveStreams) MarkActive(streamID, playURL string, maxSeconds int, cancel context.CancelFunc) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.streams[streamID] = ActiveEntry{PlayURL: playURL, MaxSeconds: maxSeconds, Cancel: cancel}
}

// MarkActiveAuto is like MarkActive, but for an entry nobody has explicitly
// requested yet (see Auto above). It also stores state so a later real
// request can claim it with ClaimAuto.
func (a *ActiveStreams) MarkActiveAuto(streamID, playURL string, maxSeconds int, cancel context.CancelFunc, state any) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.streams[streamID] = ActiveEntry{PlayURL: playURL, MaxSeconds: maxSeconds, Cancel: cancel, Auto: true, State: state}
}

// ClaimAuto replaces an Auto entry's cut-off timer with a new one (fresh
// maxSeconds/cancel, starting NOW) and marks it claimed. Used when the first
// REAL request for a stream arrives after it was silently set up (see Auto).
// Returns the OLD cancel (the caller must invoke it to stop the timer that
// started while nobody was watching) and ok=false if the entry does not
// exist or is no longer Auto (someone else won the race, or it is no longer
// active) -- in that case the caller must not touch anything else.
func (a *ActiveStreams) ClaimAuto(streamID string, maxSeconds int, cancel context.CancelFunc) (oldCancel context.CancelFunc, ok bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	e, exists := a.streams[streamID]
	if !exists || !e.Auto {
		return nil, false
	}
	old := e.Cancel
	e.MaxSeconds = maxSeconds
	e.Cancel = cancel
	e.Auto = false
	a.streams[streamID] = e
	return old, true
}

// MarkInactive releases streamID and reports whether there really was an
// active entry. ZLMediaKit fires on_stream_changed(regist=false) once PER
// output protocol of the same stream (rtmp, rtsp, ts, fmp4...), so callers
// use this to log the event only once.
func (a *ActiveStreams) MarkInactive(streamID string) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	e, ok := a.streams[streamID]
	if ok && e.Cancel != nil {
		e.Cancel()
	}
	delete(a.streams, streamID)
	return ok
}

// Entry returns the full entry (URL + already-resolved seconds limit) if
// streamID is active right now. Each Protocol uses it to answer
// idempotently without re-resolving the tenant limit against the database.
func (a *ActiveStreams) Entry(streamID string) (ActiveEntry, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	e, ok := a.streams[streamID]
	return e, ok
}
