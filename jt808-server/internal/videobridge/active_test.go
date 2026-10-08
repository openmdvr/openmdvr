package videobridge

import "testing"

// ActiveStreams tests -- in particular MarkActiveAuto/ClaimAuto, which fix a
// real dual-camera issue: "RTMP,ON,INOUT#" starts both cameras at once, so
// requesting channel 0 made on_publish SILENTLY start tracking channel 1
// (nobody asked) with its cut-off timer running from THAT instant. A later
// real request for channel 1 got less time than its session should, without
// having watched a second. See the ActiveEntry.Auto docstring.
//
// State is an OPAQUE payload (any): these tests use their own struct
// (fakeProtocolState) instead of importing gt06videobridge's real
// gt06StreamInfo, precisely to prove ActiveStreams is genuinely
// protocol-agnostic.

type fakeProtocolState struct {
	App    string
	Stream string
}

func TestActiveStreams_MarkActiveAuto_SetsAutoAndState(t *testing.T) {
	a := NewActiveStreams()
	state := fakeProtocolState{App: "live", Stream: "1/490154203237518"}
	a.MarkActiveAuto("gt06:490154203237518:1", "http://zlm/live/1.live.flv", 60, func() {}, state)

	e, ok := a.Entry("gt06:490154203237518:1")
	if !ok {
		t.Fatal("entry not found after MarkActiveAuto")
	}
	if !e.Auto {
		t.Error("Auto = false, want true")
	}
	got, ok := e.State.(fakeProtocolState)
	if !ok || got != state {
		t.Errorf("State = %v, want %+v", e.State, state)
	}
}

func TestActiveStreams_MarkActive_LeavesStateNilAndAutoFalse(t *testing.T) {
	// The JT1078 path (explicit request, never "auto") must leave no State --
	// confirms the field is optional, not something every protocol fills.
	a := NewActiveStreams()
	a.MarkActive("13800000099_1", "url", 60, func() {})

	e, ok := a.Entry("13800000099_1")
	if !ok {
		t.Fatal("entry not found after MarkActive")
	}
	if e.Auto {
		t.Error("Auto = true after MarkActive, want false")
	}
	if e.State != nil {
		t.Errorf("State = %v, want nil", e.State)
	}
}

func TestActiveStreams_ClaimAuto_ReplacesTimerAndClearsFlag(t *testing.T) {
	a := NewActiveStreams()
	oldCancelled := false
	state := fakeProtocolState{App: "live", Stream: "1/490154203237518"}
	a.MarkActiveAuto("gt06:490154203237518:1", "http://zlm/live/1.live.flv", 60, func() { oldCancelled = true }, state)

	newCancelled := false
	oldCancel, claimed := a.ClaimAuto("gt06:490154203237518:1", 60, func() { newCancelled = true })
	if !claimed {
		t.Fatal("ClaimAuto returned ok=false on a real Auto entry")
	}
	if oldCancel == nil {
		t.Fatal("ClaimAuto did not return the old cancel")
	}
	oldCancel() // real callers always invoke it -- verify it IS the old one
	if !oldCancelled {
		t.Error("the returned cancel was not the old timer")
	}
	if newCancelled {
		t.Error("the NEW cancel fired by itself -- it must stay alive until its own cut")
	}

	e, ok := a.Entry("gt06:490154203237518:1")
	if !ok {
		t.Fatal("entry disappeared after ClaimAuto")
	}
	if e.Auto {
		t.Error("Auto still true after ClaimAuto, want false (already claimed)")
	}
	if e.PlayURL != "http://zlm/live/1.live.flv" {
		t.Errorf("PlayURL changed after ClaimAuto: %q", e.PlayURL)
	}
}

func TestActiveStreams_ClaimAuto_FailsWhenNotAuto(t *testing.T) {
	// An EXPLICIT request (plain MarkActive) must not be able to "claim
	// itself" -- it already has its own real timer.
	a := NewActiveStreams()
	a.MarkActive("gt06:490154203237518:0", "url", 60, func() {})

	_, claimed := a.ClaimAuto("gt06:490154203237518:0", 60, func() {})
	if claimed {
		t.Error("ClaimAuto claimed an entry that was NOT Auto")
	}
}

func TestActiveStreams_ClaimAuto_FailsWhenNotFound(t *testing.T) {
	a := NewActiveStreams()
	_, claimed := a.ClaimAuto("does-not-exist", 60, func() {})
	if claimed {
		t.Error("ClaimAuto claimed a nonexistent entry")
	}
}

func TestActiveStreams_ClaimAuto_SecondCallerLosesRace(t *testing.T) {
	// Two nearly simultaneous requests for the same auto-tracked channel:
	// only the first must win -- the second must get ok=false so it does not
	// cancel the FRESH timer the first one just set.
	a := NewActiveStreams()
	state := fakeProtocolState{App: "live", Stream: "1/490154203237518"}
	a.MarkActiveAuto("gt06:490154203237518:1", "url", 60, func() {}, state)

	_, firstClaimed := a.ClaimAuto("gt06:490154203237518:1", 60, func() {})
	if !firstClaimed {
		t.Fatal("the first ClaimAuto should win")
	}
	_, secondClaimed := a.ClaimAuto("gt06:490154203237518:1", 60, func() {})
	if secondClaimed {
		t.Error("the second ClaimAuto (same channel, already claimed) should not win")
	}
}

func TestActiveStreams_SharedMapAcrossDifferentKeySchemes(t *testing.T) {
	// Why generics are NOT used here: a single ActiveStreams serves ALL
	// protocols at once, with key schemes that never collide. This test uses
	// a JT1078-shaped key ("<terminalID>_<ch>") and a GT06-shaped key
	// ("gt06:<imei>:<ch>") in the SAME map.
	a := NewActiveStreams()
	a.MarkActive("13800000099_1", "jt1078-url", 60, func() {})
	a.MarkActiveAuto("gt06:490154203237518:0", "gt06-url", 90, func() {}, fakeProtocolState{App: "live"})

	jt1078Entry, ok := a.Entry("13800000099_1")
	if !ok || jt1078Entry.PlayURL != "jt1078-url" {
		t.Errorf("JT1078 entry = %+v, ok=%v", jt1078Entry, ok)
	}
	gt06Entry, ok := a.Entry("gt06:490154203237518:0")
	if !ok || gt06Entry.PlayURL != "gt06-url" || !gt06Entry.Auto {
		t.Errorf("GT06 entry = %+v, ok=%v", gt06Entry, ok)
	}
}
