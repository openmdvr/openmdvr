package gt06videobridge

import (
	"context"
	"testing"
)

// --- extractGT06Stream / canonical format (F4) ---

func TestExtractGT06Stream_AcceptsCanonicalForms(t *testing.T) {
	// "<channel>/<imei>" is the REAL format confirmed against a physical
	// JC261: it publishes two simultaneous streams, one per camera
	// ("0/<imei>" and "1/<imei>"). An earlier assumption (IMEI as a prefix)
	// was wrong and was corrected with real evidence.
	cases := []struct {
		stream      string
		wantChannel uint8
	}{
		{"490154203237518", 0}, // no channel prefix -- defaults to 0
		{"0/490154203237518", 0},
		{"1/490154203237518", 1},
		{"12/490154203237518", 12}, // 2-digit channel, just in case
	}
	for _, c := range cases {
		channel, imei, ok := extractGT06Stream(c.stream)
		if !ok || imei != "490154203237518" || channel != c.wantChannel {
			t.Errorf("extractGT06Stream(%q) = (%d, %q, %v), want (%d, 490154203237518, true)", c.stream, channel, imei, ok, c.wantChannel)
		}
	}
}

func TestExtractGT06Stream_RejectsNonCanonicalForms(t *testing.T) {
	// Security finding F4: the unanchored version (`\d{15}` as a substring)
	// authorized each of these -- each created a DIFFERENT RTMP stream for
	// the same IMEI (unbounded, unbilled) and let the attacker's alias cancel
	// the cut-off timer of the victim's legitimate session. These cases stay
	// invalid with the real "<channel>/<imei>" format too.
	for _, stream := range []string{
		"garbage_490154203237518_evil", // the IMEI is neither the whole nor the suffix after "channel/"
		"AAA490154203237518",           // the IMEI is not the exact suffix
		"4901542032375181234",          // 19 digits: not exactly 15
		"garbage/490154203237518",      // the "channel" must be 1-2 digits, not text
		"0/490154203237518/extra",      // nothing after the IMEI
		"no-imei-here",
		"",
	} {
		if _, _, ok := extractGT06Stream(stream); ok {
			t.Errorf("extractGT06Stream(%q) = ok, want rejection", stream)
		}
	}
}

func TestParseStream_DelegatesToExtractGT06Stream(t *testing.T) {
	b := newTestBridge("live")
	deviceKey, channel, ok := b.ParseStream("live", "1/490154203237518")
	if !ok || deviceKey != "490154203237518" || channel != 1 {
		t.Errorf("ParseStream = (%q, %d, %v), want (490154203237518, 1, true)", deviceKey, channel, ok)
	}
}

// --- AuthorizePublish: rejections that do NOT touch Postgres ---

func TestAuthorizePublish_RejectsNoIMEIInStream(t *testing.T) {
	b := newTestBridge("live")
	err := b.AuthorizePublish(context.Background(), "live", "no-imei-here")
	if err == nil {
		t.Error("stream without a recognizable IMEI authorized, want error")
	}
}

func TestAuthorizePublish_RejectsAliasedIMEI(t *testing.T) {
	// F4: a valid IMEI that is NOT the canonical suffix must be rejected
	// BEFORE touching the database -- no Postgres needed (pool is nil in
	// newTestBridge) because the rejection happens in extractGT06Stream.
	b := newTestBridge("live")
	err := b.AuthorizePublish(context.Background(), "live", "garbage_490154203237518_evil")
	if err == nil {
		t.Error("stream with a non-canonical IMEI authorized, want error")
	}
}

// --- HandleStreamNotFound: GT06 never triggers anything, only confirms what is active ---

func TestHandleStreamNotFound_WaitsWhenAlreadyActive(t *testing.T) {
	// Field regression: the shared hook used the JT1078 parser regardless
	// of app, so it ALWAYS failed for a gt06_video stream_id -- rejecting the
	// very viewer most likely watching at that moment (real race between
	// on_publish authorizing the push and ZLMediaKit finishing registering
	// the MediaSource). With an active entry for that imei+channel, waiting
	// is now allowed instead of rejected.
	b := newTestBridge("live")
	b.active.MarkActive(gt06StreamKey("490154203237518", 0), "http://zlm/live/0.live.flv", 60, func() {})

	if !b.HandleStreamNotFound(context.Background(), "live", "0/490154203237518") {
		t.Error("allow = false with an active entry for this channel, want true (wait)")
	}
}

func TestHandleStreamNotFound_RejectsWhenNotActive(t *testing.T) {
	b := newTestBridge("live")
	if b.HandleStreamNotFound(context.Background(), "live", "0/490154203237518") {
		t.Error("allow = true without any active entry, want false")
	}
}

func TestHandleStreamNotFound_RejectsUnrecognizedStream(t *testing.T) {
	b := newTestBridge("live")
	if b.HandleStreamNotFound(context.Background(), "live", "no-imei-here") {
		t.Error("unrecognizable stream authorized, want false")
	}
}

func TestHandleStreamNotFound_DoesNotMatchOtherChannel(t *testing.T) {
	// An active entry for channel 0 must not authorize waiting on channel 1
	// -- they are independent streams (the JC261's two cameras).
	b := newTestBridge("live")
	b.active.MarkActive(gt06StreamKey("490154203237518", 0), "http://zlm/live/0.live.flv", 60, func() {})

	if b.HandleStreamNotFound(context.Background(), "live", "1/490154203237518") {
		t.Error("channel 0's active entry authorized waiting on channel 1")
	}
}

// --- HandleIdleStream ---

func TestHandleIdleStream_ClosesActiveStream(t *testing.T) {
	b := newTestBridge("live")
	b.active.MarkActive(gt06StreamKey("490154203237518", 0), "http://zlm/live/0/xyz.live.flv", 60, func() {})

	if !b.HandleIdleStream("490154203237518", 0) {
		t.Error("tracked = false for an active gt06 stream with no viewers, want true")
	}
}

func TestHandleIdleStream_LeavesInactiveIMEIAlone(t *testing.T) {
	b := newTestBridge("live")
	if b.HandleIdleStream("490154203237518", 0) {
		t.Error("tracked = true for an IMEI with no registered active stream")
	}
}

func TestHandleIdleStream_DoesNotAffectOtherActiveChannel(t *testing.T) {
	// Adding the camera to the dock opens a second player instance. If the
	// user closes ONLY that one (channel 1 has no viewers) while channel 0 is
	// still watched in the detail panel, channel 1 must not drag channel 0
	// down: "stop_video" (RTMP,OFF#) stops BOTH cameras at once, so it must
	// only be sent if NO other known channel is still active (sender=nil
	// here, so no real send is attempted -- this test checks the OBSERVABLE
	// effect: channel 0 with viewers is untouched).
	b := newTestBridge("live")
	b.active.MarkActive(gt06StreamKey("490154203237518", 0), "http://zlm/live/0.live.flv", 60, func() {})
	b.active.MarkActive(gt06StreamKey("490154203237518", 1), "http://zlm/live/1.live.flv", 60, func() {})

	if !b.HandleIdleStream("490154203237518", 1) {
		t.Error("tracked = false for active channel 1, want true (it MUST be closed)")
	}
	if _, active := b.active.Entry(gt06StreamKey("490154203237518", 0)); !active {
		t.Error("channel 0 (with viewers) was marked inactive -- HandleIdleStream(channel 1) should not affect it")
	}
}

// --- HandleStreamStopped ---

func TestHandleStreamStopped_ReleasesActiveEntry(t *testing.T) {
	b := newTestBridge("live")
	cancelled := false
	b.active.MarkActive(gt06StreamKey("490154203237518", 0), "url", 60, func() { cancelled = true })

	b.HandleStreamStopped("live", "0/490154203237518")

	if _, active := b.active.Entry(gt06StreamKey("490154203237518", 0)); active {
		t.Error("active entry not released after HandleStreamStopped")
	}
	if !cancelled {
		t.Error("MarkInactive did not cancel the cut-off timer")
	}
}

func TestHandleStreamStopped_DoesNotAffectOtherChannel(t *testing.T) {
	b := newTestBridge("live")
	b.active.MarkActive(gt06StreamKey("490154203237518", 0), "url", 60, func() {})
	b.active.MarkActive(gt06StreamKey("490154203237518", 1), "url", 60, func() {})

	b.HandleStreamStopped("live", "0/490154203237518")

	if _, active := b.active.Entry(gt06StreamKey("490154203237518", 1)); !active {
		t.Error("HandleStreamStopped for channel 0 also released channel 1's entry")
	}
}

func TestHandleStreamStopped_UnrecognizedStreamDoesNothing(t *testing.T) {
	b := newTestBridge("live")
	b.active.MarkActive(gt06StreamKey("490154203237518", 0), "url", 60, func() {})
	b.HandleStreamStopped("live", "no-imei-here")
	if _, active := b.active.Entry(gt06StreamKey("490154203237518", 0)); !active {
		t.Error("an unrecognizable stream released a real active entry")
	}
}
