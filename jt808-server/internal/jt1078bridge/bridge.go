package jt1078bridge

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/cuteLittleDevil/go-jt808/protocol/model"
	"github.com/cuteLittleDevil/go-jt808/shared/consts"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
	"github.com/openmdvr/openmdvr/jt808-server/internal/session"
	"github.com/openmdvr/openmdvr/jt808-server/internal/videobridge"
)

type Config struct {
	// ListenAddr is where this bridge listens for the devices' JT1078 VIDEO
	// connection -- a different port from JT808 signaling.
	ListenAddr string
	// PublicIP is the IP the device is told (in the 0x9101) to connect its
	// video to. It must be an IP where THIS process is reachable from the
	// device's cellular network (in production, the server's public IP; in
	// local dev with the simulator, 127.0.0.1).
	PublicIP string
	// ListenPort is the TCP port reported to the device in the 0x9101 (must
	// match ListenAddr's real port).
	ListenPort uint16

	ZLMBaseURL string
	ZLMSecret  string
	// ZLMPlayURLFormat is a template with a single %s (the stream_id) used to
	// build the playback URL returned to the requester -- e.g.
	// "http://localhost/rtp/%s.live.flv" (see the "URL rules" page of the
	// ZLMediaKit wiki for the available variants: .live.flv, .live.mp4,
	// .m3u8, etc.)
	//
	// Legacy HTTP-FLV/mpegts.js path, kept until WebRTC fully replaces it.
	ZLMPlayURLFormat string
	// ZLMWebrtcPlayBaseURL is the public HTTP base (scheme+host, no trailing
	// slash) used to build the WHEP signaling URL -- see webrtcPlayURL()
	// below. Same idea as ZLMPlayURLFormat, but WHEP uses query params
	// (?app=&stream=) instead of a %s path template.
	ZLMWebrtcPlayBaseURL string
}

// Bridge orchestrates signaling (0x9101 to the device, via the already open
// JT808 session) and video ingest (incoming JT1078 -> RTP into ZLMediaKit).
// See the package comment in rtp.go for why. It implements
// videobridge.Protocol (see protocol.go); RTMP push video from other dashcam
// families (GT06/JC261, see internal/gt06videobridge) lives in its own
// package, neither knowing about the other.
type Bridge struct {
	// meter is the central live-view time meter (SetLiveMeter).
	meter    *videobridge.LiveMeter
	cfg      Config
	zlm      *videobridge.ZLMClient
	sessions *session.Registry
	pending  *pendingRequests
	active   *videobridge.ActiveStreams
	pool     *pgxpool.Pool
}

// New creates the bridge. sessions is the SAME registry the JT808 signaling
// server uses (jt808server.New), so this bridge can find a device's open
// connection without keeping its own duplicate "which devices are connected"
// state. pool is the same Postgres pool as the JT808 server (connected as
// app_user). active/zlm are the SHARED videobridge.Dispatcher objects (the
// SAME *ActiveStreams every other registered video protocol sees -- keys
// never collide across protocols -- and the SAME *ZLMClient), built once in
// cmd/server/main.go, never instances owned by this package.
func New(cfg Config, sessions *session.Registry, pool *pgxpool.Pool, active *videobridge.ActiveStreams, zlm *videobridge.ZLMClient) *Bridge {
	return &Bridge{
		cfg:      cfg,
		zlm:      zlm,
		sessions: sessions,
		pending:  newPendingRequests(),
		active:   active,
		pool:     pool,
	}
}

// RunPendingSweeper periodically clears video requests that never connected
// (device unavailable, 0x9101 lost). Blocks until ctx is cancelled -- call it
// with `go`.
func (b *Bridge) RunPendingSweeper(ctx context.Context) {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			b.pending.sweepOlderThan(2 * time.Minute)
		}
	}
}

// ErrDeviceNotConnected is returned when the requested terminal_id has no
// active JT808 session in this process -- video cannot be requested from a
// device that is not connected.
type ErrDeviceNotConnected struct{ TerminalID string }

func (e ErrDeviceNotConnected) Error() string {
	return fmt.Sprintf("jt1078bridge: terminal %s has no active JT808 session", e.TerminalID)
}

// ErrLiveViewQuotaExhausted is returned when the tenant has used up its
// MONTHLY live-view seconds quota
// (infra/postgres/migrations/0012_tenant_live_view_quota.sql). Distinct from
// max_live_view_seconds (per-session cap): this is a cumulative balance
// consumed by real usage during the month, a hard block until the next
// calendar month or until support raises the quota manually.
type ErrLiveViewQuotaExhausted struct{ TerminalID string }

func (e ErrLiveViewQuotaExhausted) Error() string {
	return fmt.Sprintf("jt1078bridge: terminal %s: monthly live-view quota exhausted", e.TerminalID)
}

// webrtcPlayURL builds the WHEP signaling URL for a known stream_id. Pure
// function, with no embedded ticket (the API adds that, same pattern as for
// the legacy FLV `url`: this bridge never knows a video request's ticket, it
// only VALIDATES it when ZLMediaKit forwards it via on_play).
func (b *Bridge) webrtcPlayURL(id string) string {
	return fmt.Sprintf("%s/index/api/whep?app=%s&stream=%s", b.cfg.ZLMWebrtcPlayBaseURL, jt1078InternalRTPApp, id)
}

// RequestVideo asks ZLMediaKit for a new RTP receiver for this
// device/channel and sends the device a 0x9101 telling it to connect to THIS
// bridge (not to ZLMediaKit directly -- see the package comment in rtp.go
// for why). Returns the playback URLs, the tenant's PER-SESSION seconds
// limit (informational for the client -- the REAL cut is done server-side by
// enforceLiveViewLimit, whatever the client does with this value), and the
// remaining MONTHLY quota in seconds (also informational).
func (b *Bridge) RequestVideo(ctx context.Context, terminalID string, channel uint8) (playURL string, webrtcURL string, maxSeconds int, quotaRemainingSeconds int, err error) {
	id := streamID(terminalID, channel)

	// If the device is ALREADY streaming this stream, do not reopen anything:
	// closing and reopening the RTP receiver would kill the device's active
	// JT1078 connection (the old receiver is closed under it) just to serve
	// the SAME URL that is already valid. See videobridge.ActiveStreams for
	// the real bug this fixes (confirmed with ZLMediaKit logs): a second
	// viewer or the on_stream_not_found hook firing in a legitimate race
	// window no longer kills the active stream. It also does not reset the
	// time-limit clock: the limit is per device stream session and is not
	// extended because someone asks for the same URL again.
	if e, ok := b.active.Entry(id); ok {
		_, quotaRemainingSeconds, err := b.tenantVideoLimits(ctx, terminalID)
		if err != nil {
			log.Printf("jt1078bridge: could not resolve remaining monthly quota for %s (non-fatal, stream already active): %v", terminalID, err)
		} else if quotaRemainingSeconds <= 0 {
			// This path used to recompute the quota ONLY to display it,
			// gating nothing: an already active stream (a second viewer, or
			// a quick reconnect within the window before ZLM closes the
			// receiver) kept being served without limit even with the monthly
			// quota at zero. The real cut of THIS stream is done by
			// enforceLiveViewLimit's periodic recheck (at most
			// jt1078TenantRecheckInterval late); here it is enough not to
			// hand out a URL to this particular request.
			log.Printf("jt1078bridge: %s already active but monthly quota exhausted, rejecting request (the stream is cut on the next recheck)", terminalID)
			return "", "", 0, 0, ErrLiveViewQuotaExhausted{TerminalID: terminalID}
		}
		return e.PlayURL, b.webrtcPlayURL(id), e.MaxSeconds, quotaRemainingSeconds, nil
	}

	sess, ok := b.sessions.Get(terminalID)
	if !ok || !sess.Authenticated {
		return "", "", 0, 0, ErrDeviceNotConnected{TerminalID: terminalID}
	}

	// Resolve the tenant's per-session limit AND monthly quota BEFORE opening
	// anything in ZLM -- if this fails or the quota is already exhausted,
	// better not to leave an orphan RTP receiver or an extra 0x9101.
	maxSeconds, quotaRemainingSeconds, err = b.tenantVideoLimits(ctx, terminalID)
	if err != nil {
		return "", "", 0, 0, fmt.Errorf("jt1078bridge: resolving tenant video limits: %w", err)
	}
	if quotaRemainingSeconds <= 0 {
		return "", "", 0, 0, ErrLiveViewQuotaExhausted{TerminalID: terminalID}
	}

	// Close any previous receiver for the same stream_id before opening a new
	// one -- a repeated request (user reloaded the video page) must not leave
	// orphan receivers piling up in ZLM.
	if err := b.zlm.CloseRTPServer(ctx, id); err != nil {
		log.Printf("jt1078bridge: closing previous RTP receiver for %s (non-fatal): %v", id, err)
	}

	opened, err := b.zlm.OpenRTPServer(ctx, id, 0)
	if err != nil {
		return "", "", 0, 0, fmt.Errorf("jt1078bridge: opening RTP receiver in ZLMediaKit: %w", err)
	}

	b.pending.put(terminalID, channel, pendingEntry{
		streamID:   id,
		zlmPort:    opened.Port,
		createdAt:  time.Now(),
		maxSeconds: maxSeconds,
	})

	p9101 := &model.P0x9101{
		ServerIPLen:  byte(len(b.cfg.PublicIP)),
		ServerIPAddr: b.cfg.PublicIP,
		TcpPort:      b.cfg.ListenPort,
		UdpPort:      0,
		ChannelNo:    channel,
		DataType:     0, // 0 = audio and video
		StreamType:   0, // 0 = main stream
	}
	if err := sess.SendActive(uint16(consts.P9101RealTimeAudioVideoRequest), p9101.Encode(), 5*time.Second); err != nil {
		return "", "", 0, 0, fmt.Errorf("jt1078bridge: sending 0x9101 to %s: %w", terminalID, err)
	}

	return fmt.Sprintf(b.cfg.ZLMPlayURLFormat, id), b.webrtcPlayURL(id), maxSeconds, quotaRemainingSeconds, nil
}

// tenantVideoLimits resolves terminal_id -> device -> tenant -> per-session
// limit and remaining monthly quota -- wraps videobridge.TenantVideoLimits
// passing itself (b implements Protocol.LookupDevice).
func (b *Bridge) tenantVideoLimits(ctx context.Context, terminalID string) (maxSeconds int, quotaRemainingSeconds int, err error) {
	return videobridge.TenantVideoLimits(ctx, b.pool, b.meter, b, terminalID)
}

// jt1078TenantRecheckInterval: how often enforceLiveViewLimit rechecks that
// the tenant is still active while the stream runs -- same approach as
// gt06videobridge (F9 of its security review). Without it, a stream that
// stays active for any reason would never learn the tenant was suspended
// until the per-session cut. Not frequent enough to put real load on the
// Postgres pool shared with all tenants' telemetry.
const jt1078TenantRecheckInterval = 20 * time.Second

// SetLiveMeter wires the central live-view time meter (see
// videobridge.LiveMeter).
func (b *Bridge) SetLiveMeter(m *videobridge.LiveMeter) {
	b.meter = m
}

// enforceLiveViewLimit runs in its own goroutine from the moment the device
// REALLY starts sending video (relay.go, where matched becomes true) -- not
// from when the 0x9101 is sent, so the time the device takes to connect is
// not deducted. It cuts the stream server-side when whichever comes FIRST:
// the tenant's per-session limit, the monthly quota running out, or the
// tenant becoming inactive during the session -- whether or not someone is
// still watching. The mechanism (closeRtpServer -> the device gets "broken
// pipe" on its JT1078 connection -> handleVideoConn closes cleanly) is the
// same one that used to be an accidental bug (see videobridge.ActiveStreams)
// and here is the intentional cut. If the stream ends early for any other
// reason, ActiveStreams.MarkInactive cancels ctx and this goroutine does
// nothing.
func (b *Bridge) enforceLiveViewLimit(ctx context.Context, id string, terminalID string, channel uint8, seconds int) {
	// A JT1078 stream only exists if a viewer requested it (0x9101), so it
	// is always billable. The central meter keeps count and signals via
	// quotaCut if the tenant's balance runs out (videobridge.LiveMeter).
	quotaCut := make(chan string, 1)
	handle := b.meter.StartFor(context.Background(), b, terminalID, channel, func(reason string) {
		select {
		case quotaCut <- reason:
		default:
		}
	})
	defer b.meter.Stop(handle)
	deadline := time.After(time.Duration(seconds) * time.Second)
	ticker := time.NewTicker(jt1078TenantRecheckInterval)
	defer ticker.Stop()

	reason := fmt.Sprintf("tenant limit of %ds reached", seconds)
loop:
	for {
		select {
		case <-ctx.Done():
			// The stream ended for some other reason (MarkInactive: no
			// viewers, the device disconnected, or a new stream_id was
			// requested); the defer closes the meter session.
			return
		case <-deadline:
			break loop
		case r := <-quotaCut:
			reason = r
			break loop
		case <-ticker.C:
			active, err := b.tenantActiveForTerminal(context.Background(), terminalID)
			if err != nil {
				log.Printf("jt1078bridge: %s: could not recheck tenant (continuing, retry in %s): %v", terminalID, jt1078TenantRecheckInterval, err)
				continue
			}
			if !active {
				reason = "tenant became inactive during the session"
				break loop
			}
		}
	}
	log.Printf("jt1078bridge: cutting %s: %s", id, reason)
	if err := b.zlm.CloseRTPServer(context.Background(), id); err != nil {
		log.Printf("jt1078bridge: closing %s: %v", id, err)
	}
	b.active.MarkInactive(id)
}

// tenantActiveForTerminal resolves terminal_id -> tenant_id -> status in a
// single bypass transaction, for enforceLiveViewLimit's periodic recheck --
// same pattern as gt06videobridge.tenantActiveForIMEI.
func (b *Bridge) tenantActiveForTerminal(ctx context.Context, terminalID string) (bool, error) {
	var active bool
	err := db.WithBypass(ctx, b.pool, func(ctx context.Context, tx pgx.Tx) error {
		dev, err := db.LookupDeviceByTerminalID(ctx, tx, terminalID)
		if err != nil {
			return err
		}
		status, err := db.GetTenantStatus(ctx, tx, dev.TenantID)
		if err != nil {
			return err
		}
		active = status == "active"
		return nil
	})
	return active, err
}
