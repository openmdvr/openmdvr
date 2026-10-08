package jt1078bridge

import (
	"context"

	"github.com/jackc/pgx/v5"

	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
	"github.com/openmdvr/openmdvr/jt808-server/internal/videobridge"
)

// Compile-time check: *Bridge must fully implement videobridge.Protocol.
var _ videobridge.Protocol = (*Bridge)(nil)

// This file implements videobridge.Protocol for JT1078 -- the adapter that
// lets JT808 camera video live behind the same videobridge.Dispatcher as
// gt06videobridge, without either package knowing about the other.

// Name/App implement videobridge.Protocol.
func (b *Bridge) Name() string { return "jt808" }
func (b *Bridge) App() string  { return jt1078InternalRTPApp }

// ParseStream implements videobridge.Protocol.
func (b *Bridge) ParseStream(app, stream string) (deviceKey string, channel uint8, ok bool) {
	return parseStreamID(stream)
}

// StreamName implements videobridge.Protocol -- inverse of ParseStream.
func (b *Bridge) StreamName(deviceKey string, channel uint8) string {
	return streamID(deviceKey, channel)
}

// LookupDevice implements videobridge.Protocol.
func (b *Bridge) LookupDevice(ctx context.Context, tx pgx.Tx, deviceKey string) (db.Device, error) {
	return db.LookupDeviceByTerminalID(ctx, tx, deviceKey)
}

// AuthorizePublish implements videobridge.Protocol -- JT1078 always
// authorizes without touching the database. This protocol's "app"
// (jt1078InternalRTPApp, "rtp") is the INTERNAL RTP push this bridge makes
// into ZLMediaKit (see relay.go/OpenRTPServer), never reachable from outside
// the docker network (ZLMediaKit's rtp_proxy ports -- 10000 + 30000-35000 --
// are not exposed publicly, see docker-compose.yml). The real barrier on that
// path is network isolation, not this hook. Critical security finding (F1):
// the first version of the on_publish gate rejected any app other than the
// GT06 video app, which would have broken ALL existing JT1078 video.
func (b *Bridge) AuthorizePublish(ctx context.Context, app, stream string) error {
	return nil
}

// HandleStreamNotFound implements videobridge.Protocol -- JT1078 is pull: it
// triggers its own signaling (0x9101, via RequestVideo) and allows waiting if
// that succeeded.
func (b *Bridge) HandleStreamNotFound(ctx context.Context, app, stream string) (allow bool) {
	terminalID, channel, ok := parseStreamID(stream)
	if !ok {
		return false
	}
	_, _, _, _, err := b.RequestVideo(ctx, terminalID, channel)
	return err == nil
}

// HandleStreamStopped implements videobridge.Protocol -- no-op for JT1078:
// relay.go already handles closing a stream when its own socket breaks.
func (b *Bridge) HandleStreamStopped(app, stream string) {}

// HandleIdleStream implements videobridge.Protocol.
func (b *Bridge) HandleIdleStream(deviceKey string, channel uint8) (tracked bool) {
	_, active := b.active.Entry(streamID(deviceKey, channel))
	return active
}

// IsPublishing implements videobridge.Protocol -- pure query, same check as
// HandleIdleStream without any side effect.
func (b *Bridge) IsPublishing(deviceKey string, channel uint8) bool {
	_, active := b.active.Entry(streamID(deviceKey, channel))
	return active
}

// LiveViewClaimed implements videobridge.Protocol. JT1078 does not currently
// distinguish a photo-only start from a live one (the preview photo is not
// enabled for jt808 in the frontend, see SNAPSHOT_ENABLED_PROTOCOLS); the
// snapshot flow still asks ZLMediaKit for real viewers before stopping,
// which covers the case without this signal.
func (b *Bridge) LiveViewClaimed(deviceKey string, channel uint8) bool {
	return false
}
