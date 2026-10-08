package jt1078bridge

import (
	"context"
	"testing"

	"github.com/openmdvr/openmdvr/jt808-server/internal/videobridge"
)

// newTestBridge builds a Bridge with only the active-stream registry --
// enough for logic that does not touch Postgres or real JT808 sessions
// (end-to-end RequestVideo/HandleStreamNotFound need those and are verified
// against Docker and the simulator).
func newTestBridge() *Bridge {
	return &Bridge{active: videobridge.NewActiveStreams()}
}

func TestNameAndApp(t *testing.T) {
	b := newTestBridge()
	if b.Name() != "jt808" {
		t.Errorf("Name() = %q, want jt808", b.Name())
	}
	if b.App() != jt1078InternalRTPApp {
		t.Errorf("App() = %q, want %q", b.App(), jt1078InternalRTPApp)
	}
}

func TestStreamID_RoundTrip(t *testing.T) {
	cases := []struct {
		terminalID string
		channel    uint8
	}{
		{"13800000099", 0},
		{"13800000099", 1},
		{"000000000000001", 255},
	}
	for _, c := range cases {
		id := streamID(c.terminalID, c.channel)
		gotTerminal, gotChannel, ok := parseStreamID(id)
		if !ok || gotTerminal != c.terminalID || gotChannel != c.channel {
			t.Errorf("parseStreamID(streamID(%q,%d)) = (%q,%d,%v), want (%q,%d,true)",
				c.terminalID, c.channel, gotTerminal, gotChannel, ok, c.terminalID, c.channel)
		}
	}
}

func TestParseStreamID_RejectsMalformed(t *testing.T) {
	for _, id := range []string{"", "no-underscore", "13800000099_", "13800000099_notanumber", "13800000099_-1"} {
		if _, _, ok := parseStreamID(id); ok {
			t.Errorf("parseStreamID(%q) = ok, want rejection", id)
		}
	}
}

func TestParseStream_DelegatesToParseStreamID(t *testing.T) {
	b := newTestBridge()
	deviceKey, channel, ok := b.ParseStream(jt1078InternalRTPApp, "13800000099_1")
	if !ok || deviceKey != "13800000099" || channel != 1 {
		t.Errorf("ParseStream = (%q, %d, %v), want (13800000099, 1, true)", deviceKey, channel, ok)
	}
}

func TestAuthorizePublish_AlwaysAuthorizes(t *testing.T) {
	// JT1078's app is this bridge's own INTERNAL RTP push into ZLMediaKit --
	// never reachable from outside the docker network, see AuthorizePublish.
	// It must not touch Postgres (pool is nil here).
	b := newTestBridge()
	if err := b.AuthorizePublish(context.Background(), jt1078InternalRTPApp, "13800000099_1"); err != nil {
		t.Errorf("AuthorizePublish() = %v, want nil", err)
	}
}

func TestHandleIdleStream(t *testing.T) {
	b := newTestBridge()
	b.active.MarkActive("13800000099_1", "http://zlm/rtp/13800000099_1.live.flv", 60, func() {})

	if !b.HandleIdleStream("13800000099", 1) {
		t.Error("tracked = false for an active stream, want true")
	}
	if b.HandleIdleStream("13800000099", 2) {
		t.Error("tracked = true for a channel with no active entry")
	}
}

func TestHandleStreamStopped_IsANoOp(t *testing.T) {
	// relay.go already handles the real close when the JT1078 socket breaks;
	// this method only satisfies the interface and must never touch
	// ActiveStreams (an existing entry stays intact after calling it).
	b := newTestBridge()
	b.active.MarkActive("13800000099_1", "url", 60, func() {})
	b.HandleStreamStopped(jt1078InternalRTPApp, "13800000099_1")
	if _, active := b.active.Entry("13800000099_1"); !active {
		t.Error("HandleStreamStopped (which must be a no-op) released an active entry")
	}
}
