package videobridge

import (
	"context"

	"github.com/jackc/pgx/v5"

	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
)

// Protocol implements the decisions specific to ONE video protocol (JT1078
// pull, GT06/RTMP push, or any future one). Everything else (one-time
// tickets, the ZLMediaKit API client, the active-stream registry, tenant
// limit resolution) lives in this package and is shared by reference: each
// Protocol implementation is built with a pointer to the SAME
// *ActiveStreams/*TicketStore the Dispatcher uses.
//
// Adding a new video protocol (another dashcam model, another transport)
// means implementing this interface in a new package and registering it in
// NewDispatcher -- no changes here or in existing protocols. Same pattern as
// internal/commands.Sender.
//
// ZLMediaKit only accepts ONE global URL per hook type (on_publish,
// on_play, ...) in config.ini -- there is no per-app hook URL -- so the
// Dispatcher receives every hook and picks the owning Protocol by the
// payload's "app" field. This interface is the narrow contract that dispatch
// needs; no stream_id format or concrete app name leaks outside the
// implementation.
type Protocol interface {
	// Name is this protocol's public vocabulary ("jt808", "gt06_video"),
	// used by POST /video-tickets to pick who a ticket is minted for.
	Name() string

	// App is the ZLMediaKit RTMP/RTP namespace this protocol owns -- the
	// Dispatcher's routing key, since ZLM has no per-app hook URL.
	App() string

	// ParseStream extracts (deviceKey, channel) from a stream ZLMediaKit
	// reports under this protocol's app. deviceKey is opaque outside the
	// implementation (JT808 terminalID, GT06 IMEI, ...). ok=false if the
	// stream does not have the expected format.
	ParseStream(app, stream string) (deviceKey string, channel uint8, ok bool)

	// StreamName is the INVERSE of ParseStream: builds the stream name
	// ZLMediaKit uses for deviceKey/channel. Used by the snapshot flow
	// (snapshot.go) to build the internal playback URL passed to getSnap
	// without knowing any protocol's format.
	StreamName(deviceKey string, channel uint8) string

	// LookupDevice resolves deviceKey -> device/tenant INSIDE the bypass
	// transaction the caller already opened (never opens its own). Used by
	// the shared on_flow_report (billing) and by TenantVideoLimits.
	LookupDevice(ctx context.Context, tx pgx.Tx, deviceKey string) (db.Device, error)

	// AuthorizePublish is the on_publish gate -- the ONLY real defense of a
	// public RTMP/RTP port. JT1078 always returns nil (its App() is the
	// bridge's own internal RTP push into ZLMediaKit, unreachable from
	// outside the docker network). A push protocol like GT06 validates for
	// real (device protocol, active tenant) and keeps its own internal
	// bookkeeping, never exposed to this package.
	AuthorizePublish(ctx context.Context, app, stream string) error

	// HandleStreamNotFound reacts to on_stream_not_found. A pull protocol
	// (JT1078) triggers its own RequestVideo and returns allow=true on
	// success. A push protocol (GT06) never triggers anything: it only
	// returns allow=true if there is ALREADY an active/pending entry for that
	// device/channel (the real race between on_publish and
	// on_stream_not_found).
	HandleStreamNotFound(ctx context.Context, app, stream string) (allow bool)

	// HandleStreamStopped reacts to on_stream_changed(regist=false): a stream
	// that stopped publishing on the device side. No-op for a pull protocol
	// (its own connection handles closing); a push protocol marks the channel
	// inactive here.
	HandleStreamStopped(app, stream string)

	// HandleIdleStream reacts to on_stream_none_reader FOR A STREAM THE
	// PROTOCOL ALREADY KNOWS AS ACTIVE -- the "is it in my ActiveStreams?"
	// check lives INSIDE the implementation, never in the Dispatcher. Returns
	// tracked=false if unknown, so the Dispatcher answers "close nothing"
	// without making protocol decisions. GT06 uses this call to check whether
	// the OTHER channel of the same device is still active before sending its
	// stop command (a single command stops both cameras).
	HandleIdleStream(deviceKey string, channel uint8) (tracked bool)

	// IsPublishing is a PURE query (unlike HandleIdleStream, which also sends
	// real commands to the device): "is this device/channel really publishing
	// right now?" per the same *ActiveStreams. Used by the snapshot flow
	// (snapshot.go) to wait for on_publish to confirm a real session before
	// asking ZLMediaKit for a frame. NEVER reuse HandleIdleStream for this
	// polling: its contract is "no readers, stop it", so calling it in a wait
	// loop would trigger the real stop (stop_video) too early.
	IsPublishing(deviceKey string, channel uint8) bool

	// LiveViewClaimed is another PURE query: "did someone explicitly ask to
	// watch this device/channel LIVE?" (as opposed to a stream started only
	// for a preview photo, or one the device started on its own). The
	// snapshot flow uses it to NEVER stop a stream a real viewer is watching
	// or about to watch -- otherwise the preview photo would cut the live
	// video of the same camera being opened.
	LiveViewClaimed(deviceKey string, channel uint8) bool
}
