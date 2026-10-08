// Package gt06videobridge implements videobridge.Protocol for the RTMP push
// video of GT06 dashcams (Jimi IoT JC261/JC400): GT06 telemetry (the same
// binary protocol already supported for plain GPS trackers) but live video
// over RTMP PUSH directly from the device to ZLMediaKit, unlike JT1078 (pull,
// see internal/jt1078bridge).
//
// No type in this package is visible outside it except through the
// videobridge.Protocol interface (see protocol.go): this package does not
// know jt1078bridge, and jt1078bridge does not know this package.
package gt06videobridge

import (
	"context"
	"errors"
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/openmdvr/openmdvr/jt808-server/internal/commands"
	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
	"github.com/openmdvr/openmdvr/jt808-server/internal/videobridge"
)

// Config is what this protocol needs to operate -- never the JT1078 fields
// (PublicIP, ListenPort, ZLMPlayURLFormat...).
type Config struct {
	// GT06VideoApp is the RTMP "app" name this protocol recognizes as GT06
	// video (the device publishes under rtmp://<host>/<GT06VideoApp>/...,
	// configured on the device via the RSERVICE command). It is also what
	// Protocol.App() returns, and therefore the key the Dispatcher routes to
	// this protocol by. Leaving it empty disables this integration entirely
	// (see the NewDispatcher guardrail in videobridge).
	GT06VideoApp string
	// ZLMGT06PlayBaseURL is the HTTP base (scheme and host, NO trailing
	// slash) used to build a GT06 video stream's playback URL once
	// on_publish confirms it: "<base>/<app>/<stream>.live.flv". It cannot be
	// built ahead of time: the real "stream" is decided by the device
	// firmware and only known when on_publish reports it.
	// Legacy HTTP-FLV/mpegts.js path, kept until WebRTC fully replaces it.
	ZLMGT06PlayBaseURL string
	// ZLMWebrtcPlayBaseURL is the public HTTP base (scheme+host, no trailing
	// slash) used to build the WHEP signaling URL -- same idea as
	// ZLMGT06PlayBaseURL (the real app/stream are also unknown until
	// on_publish confirms them), but WHEP uses query params (?app=&stream=)
	// instead of a path.
	ZLMWebrtcPlayBaseURL string
}

// gt06VideoStartupTimeout: how long to wait, after sending the
// "request_video" command (text RTMP,ON,INOUT -- see gt06server/commands.go),
// for the device to REALLY start publishing RTMP and on_publish to confirm it
// (see AuthorizePublish). Generous but bounded: the device already confirmed
// the command on its GT06 channel (SendCommand already waited for that
// 0x15/0x21); this only covers the time it takes to open the separate RTMP
// connection and start publishing.
const gt06VideoStartupTimeout = 8 * time.Second

// gt06VideoRequestBudget: total time a video request waits for the push
// (command + confirmation + first on_publish). Must stay below the API's
// timeout toward this bridge (video.py) and the page's.
const gt06VideoRequestBudget = 30 * time.Second

func maxDuration(a, b time.Duration) time.Duration {
	if a > b {
		return a
	}
	return b
}

// sendRequestVideo sends "request_video" and, if the device's single command
// slot is busy (a photo in progress, an unanswered RTMP,OFF), retries every
// second until the deadline instead of failing immediately with "a command
// is already pending" (which surfaced to users as an error).
func (b *Bridge) sendRequestVideo(ctx context.Context, imei string, deadline time.Time) (string, error) {
	for {
		timeout := 15 * time.Second
		if left := time.Until(deadline); left < timeout {
			timeout = left
		}
		if timeout < time.Second {
			return "", commands.ErrCommandTimeout
		}
		reply, err := b.sender.SendCommand(ctx, imei, "request_video", timeout)
		if !errors.Is(err, commands.ErrCommandBusy) || time.Until(deadline) < 2*time.Second {
			return reply, err
		}
		select {
		case <-time.After(time.Second):
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
}

// gt06KnownChannels: camera channels confirmed against real hardware (JC261,
// dual-camera dashcam -- front=0, cabin=1). "RTMP,ON,INOUT#" starts BOTH at
// once with a single command; this list is used to clear tracking for ALL
// channels when the device stops on its own or on the time limit of EITHER
// one, since there is no command to stop them separately. If a future model
// of this family has more cameras, this list is the only place to extend.
var gt06KnownChannels = []uint8{0, 1}

// gt06StreamInfo is what on_publish confirms about a real push: the EXACT
// app/stream ZLMediaKit reported -- never assumed ahead of time (the exact
// path the JC261/JC400 firmware builds is only known from real traffic).
// RequestVideo uses them to build the real playback URL.
type gt06StreamInfo struct {
	App    string
	Stream string
}

// gt06PendingVideo correlates an in-flight "request_video" command with the
// real on_publish confirmation -- same purpose as pendingRequests for
// JT1078, but correlated by (IMEI, channel): the JC261 publishes an
// INDEPENDENT RTMP stream per camera (see gt06KnownChannels), so each channel
// needs its own waiters. Without this, requesting channel 1 while channel 0
// was already awaited could resolve the wrong waiter with the first
// on_publish to arrive, whatever camera it was for.
//
// inFlight (per IMEI, NOT per channel) avoids sending a redundant
// "RTMP,ON,INOUT#" when two channels are requested at about the same time:
// the real command starts BOTH cameras at once, so only the first request
// needs to send it -- the second just waits for its own channel, which the
// same in-flight command will confirm.
type gt06PendingVideo struct {
	mu sync.Mutex
	// waiters: ALL requests waiting for the push of an (imei, channel).
	// Several at once (two tabs, the dock and the panel, a retry) share the
	// same wait and the same command instead of failing as "busy".
	waiters  map[string][]chan gt06StreamInfo
	inFlight map[string]bool
	// lastStart: when the device confirmed the last "request_video". That
	// command starts BOTH cameras, so a request for the OTHER channel
	// arriving a few seconds later (the command already finished but its
	// RTMP push has not arrived yet) must wait for that push instead of
	// sending a redundant RTMP,ON. Seen in the field: the redundant command
	// occupied the device's single command slot and the next stop_video
	// failed with "a command is already pending".
	lastStart map[string]time.Time
}

func newGT06PendingVideo() *gt06PendingVideo {
	return &gt06PendingVideo{
		waiters:   make(map[string][]chan gt06StreamInfo),
		inFlight:  make(map[string]bool),
		lastStart: make(map[string]time.Time),
	}
}

func gt06PendingKey(imei string, channel uint8) string {
	return fmt.Sprintf("%s|%d", imei, channel)
}

// await registers a new waiter for (imei, channel) and returns the channel
// (buffer 1) where the confirmation will arrive. Several waiters for the
// same (imei, channel) coexist and are all notified together.
//
// Security finding (F3b): an earlier version UNCONDITIONALLY REPLACED the
// waiter of an IMEI that already had one. With two nearly simultaneous
// requests for the SAME device+channel, the second `await` overwrote the
// first one's channel; the second SendCommand failed with ErrCommandBusy and
// its own `cancel` deleted THAT waiter (the only one left in the map). When
// the device really answered the FIRST command and started publishing,
// `notify` found NOBODY to notify: the stream was orphaned, publishing
// indefinitely with no time limit. Now waiters are a list and each request
// only ever removes its own (see cancel).
func (p *gt06PendingVideo) await(imei string, channel uint8) (ch chan gt06StreamInfo) {
	key := gt06PendingKey(imei, channel)
	p.mu.Lock()
	defer p.mu.Unlock()
	ch = make(chan gt06StreamInfo, 1)
	p.waiters[key] = append(p.waiters[key], ch)
	return ch
}

// cancel removes the given Go channel from the (imei, channel) waiters (so
// it never removes another request's waiter). Always called when leaving
// RequestVideo, successful or not, so no orphan entries are left.
func (p *gt06PendingVideo) cancel(imei string, channel uint8, ch chan gt06StreamInfo) {
	key := gt06PendingKey(imei, channel)
	p.mu.Lock()
	defer p.mu.Unlock()
	list := p.waiters[key]
	for i, c := range list {
		if c == ch {
			list = append(list[:i], list[i+1:]...)
			break
		}
	}
	if len(list) == 0 {
		delete(p.waiters, key)
	} else {
		p.waiters[key] = list
	}
}

// notify delivers the on_publish confirmation to the (imei, channel)
// waiters, if any, and reports whether anyone was really waiting. A push
// WITHOUT an in-flight waiter (the device reconnected on its own after a
// network drop, the other camera of the same command confirming first, or
// RequestVideo already gave up on a timeout) has nobody to notify --
// delivered=false tells AuthorizePublish it must take over tracking this
// stream itself (see startVideoTracking), so that NO authorized push is left
// without a time limit.
func (p *gt06PendingVideo) notify(imei string, channel uint8, info gt06StreamInfo) (delivered bool) {
	key := gt06PendingKey(imei, channel)
	p.mu.Lock()
	list := p.waiters[key]
	delete(p.waiters, key)
	p.mu.Unlock()
	for _, ch := range list {
		select {
		case ch <- info:
			delivered = true
		default:
		}
	}
	return delivered
}

// tryStartCommand marks a "request_video" command as in flight for the WHOLE
// imei (it affects ALL channels at once; a single "RTMP,ON,INOUT#" starts both
// cameras). Returns false if one was already in progress (or just
// confirmed), so a second channel requested at about the same time does not
// send a redundant command and simply waits for the confirmation the first
// one will bring for both cameras.
func (p *gt06PendingVideo) tryStartCommand(imei string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.inFlight[imei] {
		return false
	}
	if t, ok := p.lastStart[imei]; ok && time.Since(t) < gt06VideoStartupTimeout {
		return false
	}
	p.inFlight[imei] = true
	return true
}

// clearStarted forgets the last confirmed start. Called when sending
// stop_video: after a stop, a new request must ALWAYS send request_video
// again instead of waiting for a push that will never come.
func (p *gt06PendingVideo) clearStarted(imei string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.lastStart, imei)
}

// markStarted records that the device just confirmed a "request_video" (see
// lastStart).
func (p *gt06PendingVideo) markStarted(imei string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.lastStart[imei] = time.Now()
}

// finishCommand clears the tryStartCommand flag. Called with `defer` around
// the WHOLE wait of the caller that sent the command (not just the send), so
// it covers the full window in which a real reply could still arrive.
func (p *gt06PendingVideo) finishCommand(imei string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.inFlight, imei)
}

// ErrDeviceNotConnected is returned when the requested imei has no active
// GT06 session in this process, or the device did not confirm the RTMP push
// in time -- video cannot be requested from an unavailable device.
type ErrDeviceNotConnected struct{ IMEI string }

func (e ErrDeviceNotConnected) Error() string {
	return fmt.Sprintf("gt06videobridge: imei %s has no active connection or did not confirm the RTMP push in time", e.IMEI)
}

// ErrLiveViewQuotaExhausted is returned when the tenant has used up its
// MONTHLY live-view seconds quota.
type ErrLiveViewQuotaExhausted struct{ IMEI string }

func (e ErrLiveViewQuotaExhausted) Error() string {
	return fmt.Sprintf("gt06videobridge: imei %s: monthly live-view quota exhausted", e.IMEI)
}

// Bridge orchestrates GT06 RTMP push video: ask the device to start via a
// text command, wait for the real on_publish confirmation, and enforce the
// session time limit. Implements videobridge.Protocol (see protocol.go).
type Bridge struct {
	photos PhotoCapturer
	// meter is the central live-view time meter (SetLiveMeter).
	meter  *videobridge.LiveMeter
	cfg    Config
	active *videobridge.ActiveStreams
	zlm    *videobridge.ZLMClient
	pool   *pgxpool.Pool
	sender commands.Sender
	wait   *gt06PendingVideo
}

// New creates the GT06 video protocol. active/zlm are the SHARED
// videobridge.Dispatcher objects (same *ActiveStreams JT1078 sees; keys never
// collide across protocols), never instances of its own. pool is the
// server's Postgres pool (connected as app_user). sender is the GT06 command
// dispatcher (cmd/server/main.go, the same one used for
// engine_stop/engine_resume). Nil-safe: if not provided (tests, or a
// deployment without GT06), RequestVideo fails with a clear error instead of
// a nil pointer panic.
func New(cfg Config, active *videobridge.ActiveStreams, zlm *videobridge.ZLMClient, pool *pgxpool.Pool, sender commands.Sender) *Bridge {
	return &Bridge{
		cfg:    cfg,
		active: active,
		zlm:    zlm,
		pool:   pool,
		sender: sender,
		wait:   newGT06PendingVideo(),
	}
}

// webrtcPlayURL builds the WHEP signaling URL from the REAL app/stream
// on_publish already confirmed. Pure function, no embedded ticket (same as
// jt1078bridge.webrtcPlayURL).
func (b *Bridge) webrtcPlayURL(app, stream string) string {
	return fmt.Sprintf("%s/index/api/whep?app=%s&stream=%s", b.cfg.ZLMWebrtcPlayBaseURL, app, stream)
}

// RequestVideo asks a gt06_video device (Jimi IoT JC261/JC400) to start the
// RTMP push for the requested channel -- the equivalent of JT1078's
// RequestVideo for this device family, except for signaling: there is no RTP
// receiver of our own to open in advance (the device pushes RTMP directly to
// ZLMediaKit, not to this process). Instead the "request_video" command is
// sent over the already authenticated GT06 connection (same channel as
// engine_stop/engine_resume) and we WAIT for on_publish to confirm the push
// really started.
//
// Two cameras, one command: "RTMP,ON,INOUT#" starts BOTH channels at once. If
// the requested channel is already active (idempotent) or a command triggered
// by the OTHER channel is ALREADY in flight, nothing new is sent; we only
// wait for THIS channel's confirmation.
func (b *Bridge) RequestVideo(ctx context.Context, imei string, channel uint8) (playURL string, webrtcURL string, maxSeconds int, quotaRemainingSeconds int, err error) {
	return b.RequestVideoFor(ctx, imei, channel, false)
}

// RequestVideoFor is RequestVideo with an explicit purpose. forSnapshot =
// true is a start ONLY to capture the preview photo: the entry stays Auto
// (no claimed session clock of its own, it never charges time to a real
// viewer) and, if the stream was already active, it does not claim it. That
// way a live viewer arriving later does claim it with a fresh clock, and the
// snapshot flow knows NOT to stop it (videobridge.Protocol.LiveViewClaimed).
func (b *Bridge) RequestVideoFor(ctx context.Context, imei string, channel uint8, forSnapshot bool) (playURL string, webrtcURL string, maxSeconds int, quotaRemainingSeconds int, err error) {
	id := gt06StreamKey(imei, channel)

	// Idempotent: a second request while the stream is already active must
	// not resend the command (needlessly reopening the device's push); it only
	// confirms the current URL and refreshes the informational quota. Covers
	// "I already requested this channel" (Auto=false: nothing to do, it has
	// its own clock running) AND "the OTHER channel requested it and
	// on_publish already set up tracking for this one too, but nobody asked
	// for it on purpose yet" (Auto=true).
	//
	// Dual-camera case: "RTMP,ON,INOUT#" starts both at once, so requesting
	// channel 0 makes on_publish SILENTLY track channel 1 (nobody asked) with
	// its cut-off clock running from THAT instant. If channel 1 is really
	// requested 20s later, it would get less time than its session should,
	// without having been watched. With Auto, this first REAL request claims
	// the entry with a fresh clock from NOW (ClaimAuto cancels the old timer)
	// -- without sending any new command to the device, which is already
	// streaming.
	if e, ok := b.active.Entry(id); ok {
		_, quotaRemainingSeconds, err := b.tenantVideoLimits(ctx, imei)
		if err != nil {
			log.Printf("gt06videobridge: could not resolve remaining monthly quota for %s (non-fatal, stream already active): %v", imei, err)
		} else if quotaRemainingSeconds <= 0 {
			// This path used to recompute the quota ONLY to display it,
			// gating nothing: an already active stream (e.g. the other
			// channel kept it running, or a quick reconnect finds it still
			// alive within the idle grace window) kept being served without
			// limit even with the monthly quota at zero. The real cut of THIS
			// stream is done by enforceLiveViewLimit's periodic recheck (at
			// most gt06TenantRecheckInterval late, same tolerance accepted
			// for F9); here it is enough not to hand out a URL to this
			// particular request.
			log.Printf("gt06videobridge: %s channel %d already active but monthly quota exhausted, rejecting request (the stream is cut on the next recheck)", imei, channel)
			return "", "", 0, 0, ErrLiveViewQuotaExhausted{IMEI: imei}
		}
		info, infoOK := e.State.(*gt06StreamInfo)
		if e.Auto && infoOK && info != nil && !forSnapshot {
			cutoffCtx, cancel := context.WithCancel(context.Background())
			if oldCancel, claimed := b.active.ClaimAuto(id, e.MaxSeconds, cancel); claimed {
				if oldCancel != nil {
					oldCancel()
				}
				log.Printf("gt06videobridge: %s channel %d claimed with a fresh clock (%ds) -- nobody had explicitly requested it until now", imei, channel, e.MaxSeconds)
				go b.enforceLiveViewLimit(cutoffCtx, imei, channel, *info, e.MaxSeconds, true)
			} else {
				cancel() // lost the race (another concurrent claim, or no longer active) -- do not leave the context orphaned
			}
		}
		if infoOK && info != nil {
			webrtcURL = b.webrtcPlayURL(info.App, info.Stream)
		}
		return e.PlayURL, webrtcURL, e.MaxSeconds, quotaRemainingSeconds, nil
	}

	if b.sender == nil {
		return "", "", 0, 0, fmt.Errorf("gt06videobridge: gt06 video support not configured in this deployment")
	}

	maxSeconds, quotaRemainingSeconds, err = b.tenantVideoLimits(ctx, imei)
	if err != nil {
		return "", "", 0, 0, fmt.Errorf("gt06videobridge: resolving tenant video limits (%s): %w", imei, err)
	}
	if quotaRemainingSeconds <= 0 {
		return "", "", 0, 0, ErrLiveViewQuotaExhausted{IMEI: imei}
	}

	// A single budget for the whole request. Real cellular networks have
	// multi-second spikes: better to wait a bit longer than return an error
	// the user will retry anyway.
	deadline := time.Now().Add(gt06VideoRequestBudget)

	// Several requests for the same channel share the wait (see waiters); the
	// command is sent once (tryStartCommand). F3b stays closed: each request
	// removes only ITS waiter when done.
	waitCh := b.wait.await(imei, channel)
	defer b.wait.cancel(imei, channel, waitCh)

	if b.wait.tryStartCommand(imei) {
		defer b.wait.finishCommand(imei)
		reply, err := b.sendRequestVideo(ctx, imei, deadline)
		switch {
		case err == nil:
			b.wait.markStarted(imei)
			log.Printf("gt06videobridge: request_video sent to %s (reply=%q), waiting for RTMP push channel %d", imei, reply, channel)
		case errors.Is(err, commands.ErrDeviceNotConnected):
			return "", "", 0, 0, ErrDeviceNotConnected{IMEI: imei}
		case errors.Is(err, commands.ErrCommandTimeout):
			// The command went out on the socket; the device may be slow and
			// start streaming anyway. Keep waiting for the push instead of
			// failing (and the other channel does not send a second command).
			b.wait.markStarted(imei)
			log.Printf("gt06videobridge: %s did not confirm request_video in time, still waiting for RTMP push channel %d", imei, channel)
		default:
			return "", "", 0, 0, fmt.Errorf("gt06videobridge: sending request_video to %s: %w", imei, err)
		}
	} else {
		// The OTHER channel already triggered the same command (it starts both
		// cameras at once) -- do not send a new one, just wait for THIS
		// channel's on_publish with the same confirmation.
		log.Printf("gt06videobridge: %s already has request_video in flight or just confirmed (starts both cameras), waiting for RTMP push channel %d", imei, channel)
	}

	// Note (F10 of the security review, informational, no code fix):
	// on_publish authorizes and notifies BEFORE ZLMediaKit confirms the
	// MediaSource is really registered (it may reject it afterwards, e.g.
	// "Already publishing" if the device retries very fast), so this function
	// could return a playURL for a stream that never actually existed. Low
	// impact (the player would see no video and the user would retry);
	// closing it fully would require waiting for
	// on_stream_changed(regist=true), not worth the extra complexity.
	select {
	case info := <-waitCh:
		return b.startVideoTracking(imei, channel, info, maxSeconds, quotaRemainingSeconds, forSnapshot)
	case <-time.After(maxDuration(time.Until(deadline), gt06VideoStartupTimeout)):
		// The device accepted the command (the 0x15/0x21 arrived) but this
		// channel never started publishing in time -- same error code as
		// "device not connected" on the video.py side (404 -> "the camera has
		// no active connection right now"), the right business message even
		// though the technical cause differs.
		return "", "", 0, 0, ErrDeviceNotConnected{IMEI: imei}
	case <-ctx.Done():
		return "", "", 0, 0, ctx.Err()
	}
}

// startVideoTracking builds the real playback URL (from the app/stream
// on_publish confirmed, NEVER assumed ahead of time) and starts the "active
// stream" tracking + the time-limit cut-off timer -- the ONLY place that does
// this. Called both by RequestVideo (normal path: an in-flight waiter gets
// the confirmation, autoTracked=false) and by AuthorizePublish (fallback
// path: an authorized push without any waiter -- the OTHER camera of the same
// command, a device reconnecting on its own after a network drop, or the
// original request already gave up on a timeout, see F3 of the security
// review -- autoTracked=true, see videobridge.ActiveEntry.Auto). Idempotent:
// if there is already an active entry for this (imei, channel), the timer is
// not overwritten or duplicated.
func (b *Bridge) startVideoTracking(imei string, channel uint8, info gt06StreamInfo, maxSeconds, quotaRemainingSeconds int, autoTracked bool) (playURL string, webrtcURL string, gotMaxSeconds int, gotQuota int, err error) {
	id := gt06StreamKey(imei, channel)
	webrtcURL = b.webrtcPlayURL(info.App, info.Stream)
	if e, ok := b.active.Entry(id); ok {
		return e.PlayURL, webrtcURL, e.MaxSeconds, quotaRemainingSeconds, nil
	}
	playURL = fmt.Sprintf("%s/%s/%s.live.flv", b.cfg.ZLMGT06PlayBaseURL, info.App, info.Stream)
	cutoffCtx, cancel := context.WithCancel(context.Background())
	if autoTracked {
		b.active.MarkActiveAuto(id, playURL, maxSeconds, cancel, &info)
	} else {
		b.active.MarkActive(id, playURL, maxSeconds, cancel)
	}
	go b.enforceLiveViewLimit(cutoffCtx, imei, channel, info, maxSeconds, !autoTracked)
	return playURL, webrtcURL, maxSeconds, quotaRemainingSeconds, nil
}

// gt06TenantRecheckInterval: how often enforceLiveViewLimit rechecks that the
// tenant is still active while the stream runs. Security finding F9: before
// this, a tenant switched to suspended/cancelled in the MIDDLE of a
// gt06_video session was not noticed until the final time-limit cut. Not
// frequent enough to put real load on the Postgres pool shared with all
// tenants' telemetry.
const gt06TenantRecheckInterval = 20 * time.Second

// enforceLiveViewLimit cuts the RTMP push when the tenant's seconds limit is
// reached, the monthly quota runs out, or EARLIER if the tenant becomes
// inactive during the session (F9). The real cut is DOUBLE (defense in
// depth): first the device is asked to stop on its own ("stop_video", best
// effort -- not fatal if the device does not answer in time), and the stream
// is ALWAYS closed on the ZLMediaKit side as well, without waiting for the
// device's reply.
//
// "stop_video" (RTMP,OFF#) stops BOTH cameras at once -- there is no command
// to stop just one (see gt06KnownChannels) -- so when EITHER channel's limit
// is reached, the whole device is cut and tracking is released for ALL known
// channels, not just the one that triggered the cut (otherwise the other
// channel would keep a timer running on a stream that no longer exists on
// the device side).
//
// If THIS channel's stream ends earlier for any other reason (the device
// disconnected, or the other channel already cut it first),
// ActiveStreams.MarkInactive cancels ctx and this goroutine does nothing.
func (b *Bridge) enforceLiveViewLimit(ctx context.Context, imei string, channel uint8, info gt06StreamInfo, seconds int, billable bool) {
	// Only a stream a viewer really requested consumes viewing time
	// (billable); an Auto one -- the other camera RTMP,ON,INOUT# turns on by
	// itself, or a start for the fallback photo -- does not, until someone
	// claims it (ClaimAuto starts another, billable goroutine). The central
	// meter keeps count and signals via quotaCut if the tenant's balance runs
	// out (see videobridge.LiveMeter).
	quotaCut := make(chan string, 1)
	var handle videobridge.MeterHandle
	if billable {
		handle = b.meter.StartFor(context.Background(), b, imei, channel, func(reason string) {
			select {
			case quotaCut <- reason:
			default:
			}
		})
	}
	defer b.meter.Stop(handle)
	deadline := time.After(time.Duration(seconds) * time.Second)
	ticker := time.NewTicker(gt06TenantRecheckInterval)
	defer ticker.Stop()

	reason := fmt.Sprintf("tenant limit of %ds reached", seconds)
loop:
	for {
		select {
		case <-ctx.Done():
			// The channel ended for some other reason (claimed by a real
			// request -- ClaimAuto --, no viewers, or the device
			// disconnected); the defer closes the meter session.
			return
		case <-deadline:
			break loop
		case r := <-quotaCut:
			reason = r
			break loop
		case <-ticker.C:
			active, err := b.tenantActiveForIMEI(context.Background(), imei)
			if err != nil {
				log.Printf("gt06videobridge: %s: could not recheck tenant (continuing, retry in %s): %v", imei, gt06TenantRecheckInterval, err)
				continue
			}
			if !active {
				reason = "tenant became inactive during the session"
				break loop
			}
		}
	}

	log.Printf("gt06videobridge: cutting video %s (channel %d triggered the cut, RTMP,OFF# stops both cameras): %s", imei, channel, reason)

	stopCtx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
	b.wait.clearStarted(imei)
	if _, err := b.sender.SendCommand(stopCtx, imei, "stop_video", 5*time.Second); err != nil {
		log.Printf("gt06videobridge: %s did not confirm stop_video (closing server-side anyway): %v", imei, err)
	}
	stopCancel()

	if err := b.zlm.CloseMediaStream(context.Background(), info.App, info.Stream); err != nil {
		log.Printf("gt06videobridge: closing stream %s channel %d in ZLMediaKit: %v", imei, channel, err)
	}
	b.markAllChannelsInactive(imei)
}

// markAllChannelsInactive releases ActiveStreams for ALL known channels of
// this imei (see gt06KnownChannels). Used when the device is known to have
// stopped streaming ENTIRELY (stop_video, or the device dropped its whole
// GT06 connection), so no orphan timers keep running on channels whose real
// stream no longer exists. MarkInactive is a safe no-op on a missing entry.
func (b *Bridge) markAllChannelsInactive(imei string) {
	for _, ch := range gt06KnownChannels {
		b.active.MarkInactive(gt06StreamKey(imei, ch))
	}
}

// tenantVideoLimits resolves imei -> device -> tenant -> per-session limit
// and remaining monthly quota -- wraps videobridge.TenantVideoLimits passing
// itself (b implements Protocol.LookupDevice).
func (b *Bridge) tenantVideoLimits(ctx context.Context, imei string) (maxSeconds int, quotaRemainingSeconds int, err error) {
	return videobridge.TenantVideoLimits(ctx, b.pool, b.meter, b, imei)
}

// tenantActiveForIMEI resolves imei -> tenant_id -> status in a single bypass
// transaction, for enforceLiveViewLimit's periodic recheck (F9).
func (b *Bridge) tenantActiveForIMEI(ctx context.Context, imei string) (bool, error) {
	var active bool
	err := db.WithBypass(ctx, b.pool, func(ctx context.Context, tx pgx.Tx) error {
		dev, err := db.LookupDeviceByIMEI(ctx, tx, imei)
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

// gt06StreamKey is the ActiveStreams key for one channel of a gt06_video
// device -- its own prefix ("gt06:") so it can never collide with a JT1078
// stream_id ("<terminalID>_<channel>"), plus the channel to treat the JC261's
// two cameras (front=0, cabin=1, see gt06KnownChannels) as independent
// streams.
func gt06StreamKey(imei string, channel uint8) string {
	return fmt.Sprintf("gt06:%s:%d", imei, channel)
}
