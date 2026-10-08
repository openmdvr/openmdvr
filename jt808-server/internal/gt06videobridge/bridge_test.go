package gt06videobridge

import (
	"context"
	"testing"

	"github.com/openmdvr/openmdvr/jt808-server/internal/videobridge"
)

// Tests for the GT06 video integration (Jimi IoT JC261/JC400) and the
// findings of its security review (F1/F3/F4/F5/F6/F9, see the docstrings in
// bridge.go and protocol.go). Only paths that do NOT touch
// Postgres/ZLMediaKit are tested here; the ones that do (device protocol,
// active tenant, RequestVideo beyond the early nil-sender error) are verified
// against a real Docker stack, same as gt06server/handlers_test.go.
//
// The cross-protocol parts (ticket authorization, F6 "ticket app mismatch",
// routing by App()/Name()) live in internal/videobridge/dispatcher_test.go,
// which tests them generically against ANY Protocol, GT06 included since it
// implements the same interface.

// newTestBridge builds a Bridge with GT06VideoApp set and no pool or sender
// -- enough for logic that does not touch Postgres or the network.
func newTestBridge(app string) *Bridge {
	return &Bridge{
		cfg:    Config{GT06VideoApp: app},
		active: videobridge.NewActiveStreams(),
		wait:   newGT06PendingVideo(),
	}
}

// --- Name()/App() ---

func TestNameAndApp(t *testing.T) {
	b := newTestBridge("live")
	if b.Name() != "gt06_video" {
		t.Errorf("Name() = %q, want gt06_video", b.Name())
	}
	if b.App() != "live" {
		t.Errorf("App() = %q, want live", b.App())
	}
}

// --- gt06PendingVideo ---

func TestGT06PendingVideo_NotifyDeliversToWaiter(t *testing.T) {
	p := newGT06PendingVideo()
	ch := p.await("490154203237518", 0)
	if !p.notify("490154203237518", 0, gt06StreamInfo{App: "live", Stream: "xyz"}) {
		t.Fatal("notify reported delivered=false with a real waiter in flight")
	}
	select {
	case info := <-ch:
		if info.App != "live" || info.Stream != "xyz" {
			t.Errorf("info = %+v, want {live xyz}", info)
		}
	default:
		t.Fatal("notify did not deliver to the waiter")
	}
}

func TestGT06PendingVideo_DistinguishesChannels(t *testing.T) {
	// The JC261's two cameras are INDEPENDENT streams -- a channel 0 request
	// must not be resolved (or blocked) by channel 1's confirmation, even
	// with the same IMEI.
	p := newGT06PendingVideo()
	ch0 := p.await("490154203237518", 0)
	ch1 := p.await("490154203237518", 1)

	if !p.notify("490154203237518", 1, gt06StreamInfo{App: "live", Stream: "1/490154203237518"}) {
		t.Fatal("notify(channel 1) did not find its own waiter")
	}
	select {
	case <-ch0:
		t.Fatal("channel 1's confirmation reached channel 0's waiter")
	default:
	}
	select {
	case info := <-ch1:
		if info.Stream != "1/490154203237518" {
			t.Errorf("stream = %q, want 1/490154203237518", info.Stream)
		}
	default:
		t.Fatal("channel 1's confirmation did not reach its own waiter")
	}
}

func TestGT06PendingVideo_NotifyWithoutWaiterReturnsFalse(t *testing.T) {
	// A push with no in-flight request (the device reconnected on its own,
	// or the OTHER camera of the same command confirming first) must not
	// block or panic -- notify has nobody to notify and reports
	// delivered=false so the caller (AuthorizePublish) takes over tracking
	// itself (F3).
	p := newGT06PendingVideo()
	if p.notify("490154203237518", 0, gt06StreamInfo{App: "live", Stream: "xyz"}) {
		t.Error("notify reported delivered=true with no registered waiter")
	}
}

func TestGT06PendingVideo_SeveralWaitersShareOneConfirmation(t *testing.T) {
	// Several requests for the SAME channel (dock and panel, two tabs, a
	// retry) share the wait: the confirmation reaches ALL of them. Previously
	// the second failed as "busy". F3b stays closed: nobody overwrites
	// another's waiter.
	p := newGT06PendingVideo()
	first := p.await("imei1", 0)
	second := p.await("imei1", 0)
	if !p.notify("imei1", 0, gt06StreamInfo{App: "live", Stream: "s"}) {
		t.Fatal("notify found no waiters")
	}
	for i, ch := range []chan gt06StreamInfo{first, second} {
		select {
		case <-ch:
		default:
			t.Fatalf("the confirmation did not reach waiter %d", i)
		}
	}
}

func TestGT06PendingVideo_CancelRemovesOnlyItsOwnWaiter(t *testing.T) {
	// A request that gives up (timeout) removes ONLY its own waiter: the
	// other request for the same channel still gets the confirmation.
	p := newGT06PendingVideo()
	gone := p.await("imei1", 0)
	stay := p.await("imei1", 0)
	p.cancel("imei1", 0, gone)
	p.cancel("imei1", 0, gone) // idempotent

	if !p.notify("imei1", 0, gt06StreamInfo{App: "live", Stream: "s"}) {
		t.Fatal("cancel() of one waiter also removed the other")
	}
	select {
	case <-stay:
	default:
		t.Fatal("the remaining waiter did not get the confirmation")
	}
	select {
	case <-gone:
		t.Fatal("the cancelled waiter got the confirmation")
	default:
	}
}

func TestGT06PendingVideo_TryStartCommand_OnlyOneWinnerPerIMEI(t *testing.T) {
	// "RTMP,ON,INOUT#" starts BOTH cameras with one command -- if both
	// channels are requested at about the same time, only the first must
	// send the real command.
	p := newGT06PendingVideo()
	if !p.tryStartCommand("imei1") {
		t.Fatal("the first tryStartCommand should win")
	}
	if p.tryStartCommand("imei1") {
		t.Fatal("the second tryStartCommand (same imei, command in flight) should not win")
	}
	p.finishCommand("imei1")
	if !p.tryStartCommand("imei1") {
		t.Fatal("after finishCommand, a new tryStartCommand should win")
	}
}

// --- RequestVideo: early error without touching Postgres ---

func TestRequestVideo_NoSenderConfigured(t *testing.T) {
	b := newTestBridge("live")
	_, _, _, _, err := b.RequestVideo(context.Background(), "490154203237518", 0)
	if err == nil {
		t.Fatal("want error with no sender configured")
	}
}

// Field regression: the SECOND channel's request arrived right after the
// first already confirmed "RTMP:OK!" (command finished) but before its own
// push, and sent a redundant RTMP,ON that occupied the device's single
// command slot. Within the startup window, the other channel waits for its
// push instead of resending.
func TestGT06PendingVideo_RecentStartSuppressesRedundantCommand(t *testing.T) {
	p := newGT06PendingVideo()
	if !p.tryStartCommand("imei1") {
		t.Fatal("the first tryStartCommand should win")
	}
	p.markStarted("imei1")
	p.finishCommand("imei1")
	if p.tryStartCommand("imei1") {
		t.Fatal("right after a confirmed start, the other channel should not resend the command")
	}
	p.clearStarted("imei1") // after a stop_video, a new request MUST send the command
	if !p.tryStartCommand("imei1") {
		t.Fatal("after clearStarted, tryStartCommand should win")
	}
}

func TestLiveViewClaimed_OnlyForExplicitRequests(t *testing.T) {
	b := newTestBridge("live")
	b.active.MarkActiveAuto(gt06StreamKey("imei1", 0), "u", 60, func() {}, &gt06StreamInfo{App: "live", Stream: "0/imei1"})
	if b.LiveViewClaimed("imei1", 0) {
		t.Fatal("an Auto entry (preview photo / device-initiated start) is not a live request")
	}
	if _, ok := b.active.ClaimAuto(gt06StreamKey("imei1", 0), 60, func() {}); !ok {
		t.Fatal("ClaimAuto should claim the entry")
	}
	if !b.LiveViewClaimed("imei1", 0) {
		t.Fatal("after a live viewer's claim, LiveViewClaimed should be true")
	}
	if b.LiveViewClaimed("imei1", 1) {
		t.Fatal("the other channel is not claimed")
	}
}
