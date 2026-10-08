package gt06videobridge

import (
	"context"
	"fmt"
	"log"
	"regexp"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
	"github.com/openmdvr/openmdvr/jt808-server/internal/videobridge"
)

// Compile-time check: *Bridge must fully implement videobridge.Protocol.
var _ videobridge.Protocol = (*Bridge)(nil)

// gt06IMEIPattern recognizes the TWO RTMP stream formats confirmed against
// real hardware (JC261): the bare IMEI (`<imei>`, implicit channel 0) and
// `<channel>/<imei>`. A real dual-camera JC261 (front + cabin) publishes TWO
// simultaneous RTMP streams in the second format on receiving
// "RTMP,ON,INOUT#", one per channel (e.g. "0/490154203237518" and
// "1/490154203237518" arriving ~1s apart).
//
// Both branches are anchored at both ends (security finding F4): an alias
// like "garbage_<imei>_evil", a channel with text instead of 1-2 digits, or a
// run of more than 15 digits matches NO branch. Previously each alias created
// a DIFFERENT RTMP stream for the SAME IMEI (unbounded, unbilled), and
// on_stream_changed's markInactive (keyed by IMEI only) let the ATTACKER's
// alias cancel the cut-off timer of the victim's LEGITIMATE session just by
// publishing and stopping its own alias.
var gt06IMEIPattern = regexp.MustCompile(`^(?:(\d{1,2})/)?(\d{15})$`)

// extractGT06Stream applies gt06IMEIPattern and returns channel + IMEI if the
// stream has the canonical format, ok=false otherwise. Without an explicit
// channel prefix the channel is 0 (single-camera device).
func extractGT06Stream(stream string) (channel uint8, imei string, ok bool) {
	m := gt06IMEIPattern.FindStringSubmatch(stream)
	if m == nil {
		return 0, "", false
	}
	if m[1] != "" {
		n, err := strconv.Atoi(m[1])
		if err != nil || n < 0 || n > 255 {
			return 0, "", false
		}
		channel = uint8(n)
	}
	return channel, m[2], true
}

// Name/App implement videobridge.Protocol -- see that interface's docstring
// for the rationale of each.
func (b *Bridge) Name() string { return "gt06_video" }
func (b *Bridge) App() string  { return b.cfg.GT06VideoApp }

// ParseStream implements videobridge.Protocol.
func (b *Bridge) ParseStream(app, stream string) (deviceKey string, channel uint8, ok bool) {
	ch, imei, ok := extractGT06Stream(stream)
	return imei, ch, ok
}

// LookupDevice implements videobridge.Protocol.
func (b *Bridge) LookupDevice(ctx context.Context, tx pgx.Tx, deviceKey string) (db.Device, error) {
	return db.LookupDeviceByIMEI(ctx, tx, deviceKey)
}

// StreamName implements videobridge.Protocol -- inverse of ParseStream. It
// always includes the explicit channel (e.g. "0/<imei>"): gt06IMEIPattern
// accepts both forms (with/without a channel prefix when it is 0), but being
// explicit avoids any ambiguity for the caller.
func (b *Bridge) StreamName(deviceKey string, channel uint8) string {
	return fmt.Sprintf("%d/%s", channel, deviceKey)
}

// AuthorizePublish implements videobridge.Protocol -- the ONLY real defense
// of the public surface this protocol exposes (ZLMediaKit's RTMP port):
// without it, anyone reaching that port could push a fake stream under any
// name. Risk model: EXACTLY the one already accepted for GT06 login (IMEI
// provisioned-or-not as the only barrier, no cryptographic auth -- the base
// protocol has none). No stronger mechanism is invented here because the
// device cannot receive a dynamic ticket the way a browser does (see
// videobridge.Dispatcher.handlePlayAuth).
//
// The Dispatcher already guarantees this is only called for app==b.App(), so
// no "does this belong to another protocol?" check is needed here.
func (b *Bridge) AuthorizePublish(ctx context.Context, app, stream string) error {
	channel, imei, ok := extractGT06Stream(stream)
	if !ok {
		return fmt.Errorf("gt06videobridge: stream without canonical IMEI: %q", stream)
	}

	// dev.Protocol must be 'gt06_video' (security finding F5): a plain GT06
	// tracker (no camera, protocol='gt06') shares the same IMEI format, and
	// without this check could also "publish video" (never playable through
	// the API, but consuming ingest/CPU/disk indefinitely all the same).
	var dev db.Device
	err := db.WithBypass(ctx, b.pool, func(ctx context.Context, tx pgx.Tx) error {
		d, err := db.LookupDeviceByIMEI(ctx, tx, imei)
		if err != nil {
			return err
		}
		dev = d
		if d.Protocol != "gt06_video" {
			return fmt.Errorf("protocol not authorized for video: %s", d.Protocol)
		}
		tenantStatus, err := db.GetTenantStatus(ctx, tx, dev.TenantID)
		if err != nil {
			return err
		}
		if tenantStatus != "active" {
			return fmt.Errorf("tenant not active: %s", tenantStatus)
		}
		return nil
	})
	if err != nil {
		return err
	}
	if dev.Status != "active" {
		return fmt.Errorf("device not active: %s", dev.Status)
	}

	log.Printf("gt06videobridge: on_publish authorized imei=%s channel=%d app=%s stream=%s", imei, channel, app, stream)
	info := gt06StreamInfo{App: app, Stream: stream}
	if !b.wait.notify(imei, channel, info) {
		// Nobody is waiting for this confirmation (the OTHER camera of the
		// same command confirming first -- "RTMP,ON,INOUT#" starts both at
		// once, see gt06KnownChannels --, RequestVideo already gave up on a
		// timeout, or the device reconnected on its own after a network
		// drop). Security finding F3: without this, an AUTHORIZED push kept
		// running indefinitely with no time limit and no idle cut, because
		// ActiveStreams was never populated. AuthorizePublish becomes the
		// authoritative source: it resolves the tenant limits and starts the
		// time cut itself, exactly as if RequestVideo had received it.
		maxSeconds, quotaRemaining, err := b.tenantVideoLimits(context.Background(), imei)
		if err != nil {
			log.Printf("gt06videobridge: on_publish authorized but tracking for %s channel %d could not be set up (left unlimited, accepted risk until the next request): %v", imei, channel, err)
		} else if quotaRemaining <= 0 {
			// The monthly quota is already exhausted. The publish cannot be
			// rejected at this point (nil is returned below and cannot be
			// undone), but it is cut immediately instead of left running.
			log.Printf("gt06videobridge: on_publish for %s channel %d with monthly quota exhausted, cutting immediately", imei, channel)
			go b.enforceLiveViewLimit(context.Background(), imei, channel, info, 0, false)
		} else {
			if _, _, _, _, err := b.startVideoTracking(imei, channel, info, maxSeconds, quotaRemaining, true); err != nil {
				log.Printf("gt06videobridge: on_publish could not set up tracking for %s channel %d: %v", imei, channel, err)
			}
		}
	}
	return nil
}

// HandleStreamNotFound implements videobridge.Protocol -- GT06 is pure push
// and never triggers anything here (unlike a pull protocol): it only answers
// "wait" (allow=true) if there is already an active/pending entry for this
// device/channel, the real race between on_publish and on_stream_not_found.
func (b *Bridge) HandleStreamNotFound(ctx context.Context, app, stream string) (allow bool) {
	channel, imei, ok := extractGT06Stream(stream)
	if !ok {
		log.Printf("gt06videobridge: on_stream_not_found with unrecognized stream_id: %q", stream)
		return false
	}
	_, active := b.active.Entry(gt06StreamKey(imei, channel))
	if active {
		log.Printf("gt06videobridge: on_stream_not_found raced with on_publish for %s channel %d, waiting", imei, channel)
	}
	return active
}

// HandleStreamStopped implements videobridge.Protocol -- releases
// ActiveStreams when an RTMP push stops on the DEVICE side (unlike
// enforceLiveViewLimit, where the SERVER stops it on a time limit). Without
// it, a device signal loss leaves a stale "active" entry with a dead
// playURL: a later video request for the SAME device would take
// RequestVideo's idempotent path (believing the stream is alive) and return
// that dead URL instead of sending a new "request_video".
func (b *Bridge) HandleStreamStopped(app, stream string) {
	channel, imei, ok := extractGT06Stream(stream)
	if !ok {
		return
	}
	if b.active.MarkInactive(gt06StreamKey(imei, channel)) {
		log.Printf("gt06videobridge: stream %s channel %d stopped on the device side, releasing", imei, channel)
	}
}

// HandleIdleStream implements videobridge.Protocol -- called only once the
// Dispatcher knows ZLMediaKit will close this stream for lack of viewers.
// Unlike JT1078 (where closing ZLM's RTP receiver already breaks the socket
// the device holds with the bridge, an automatic cascade), a GT06 RTMP push
// goes DIRECTLY to ZLMediaKit without passing through this process, so
// closing it on the ZLM side tells the device nothing by itself -- the
// DEVICE must be asked to stop publishing.
//
// BUT "stop_video" (RTMP,OFF#) stops BOTH cameras at once. If the user is
// watching channel 0 in the detail panel AND channel 1 in the camera dock
// and closes ONLY the dock, this is called for channel 1 with no viewers,
// but sending stop_video would also cut channel 0, which IS still being
// watched. Before sending the real stop, check whether any OTHER known
// channel of this IMEI is still active; if so, stop_video is not sent and the
// Dispatcher only closes THIS channel's stream in ZLM (the device keeps
// publishing the other one, as it should).
func (b *Bridge) HandleIdleStream(deviceKey string, channel uint8) (tracked bool) {
	imei := deviceKey
	if _, active := b.active.Entry(gt06StreamKey(imei, channel)); !active {
		return false
	}

	if b.sender != nil {
		otherChannelStillActive := false
		for _, ch := range gt06KnownChannels {
			if ch == channel {
				continue
			}
			if _, active := b.active.Entry(gt06StreamKey(imei, ch)); active {
				otherChannelStillActive = true
				break
			}
		}
		if otherChannelStillActive {
			log.Printf("gt06videobridge: %s channel %d has no viewers, but another channel is still active -- not sending stop_video (it would affect both cameras)", imei, channel)
		} else {
			go func(imei string) {
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				b.wait.clearStarted(imei)
				if _, err := b.sender.SendCommand(ctx, imei, "stop_video", 5*time.Second); err != nil {
					log.Printf("gt06videobridge: %s did not confirm stop_video after running out of viewers: %v", imei, err)
				}
			}(imei)
		}
	}

	return true
}

// IsPublishing implements videobridge.Protocol -- pure query, same check as
// the first step of HandleIdleStream but with no side effects (never sends
// stop_video).
func (b *Bridge) IsPublishing(deviceKey string, channel uint8) bool {
	_, active := b.active.Entry(gt06StreamKey(deviceKey, channel))
	return active
}

// LiveViewClaimed implements videobridge.Protocol: an active, non-Auto entry
// is an explicit live video request (RequestVideo with forSnapshot=false, or
// a later ClaimAuto). Entries started by a preview photo or by the device on
// its own stay Auto until a real viewer claims them.
func (b *Bridge) LiveViewClaimed(deviceKey string, channel uint8) bool {
	e, active := b.active.Entry(gt06StreamKey(deviceKey, channel))
	return active && !e.Auto
}

// PhotoCapturer asks the device for a native photo (alarmclip.Bridge.
// CapturePhoto: "Picture,out#"/"Picture,in#" + the photo's HTTP upload). Own
// interface so alarmclip is not imported from here.
type PhotoCapturer interface {
	CapturePhoto(ctx context.Context, imei string, channel uint8) ([]byte, error)
}

// SetPhotoCapturer wires the native photo (a setter because of the real
// construction order in main.go: alarmclip needs gt06server's Dispatcher).
func (b *Bridge) SetPhotoCapturer(pc PhotoCapturer) {
	b.photos = pc
}

// SetLiveMeter wires the central live-view time meter (see
// videobridge.LiveMeter). Without a meter video still works but viewing time
// is not deducted (tests only).
func (b *Bridge) SetLiveMeter(m *videobridge.LiveMeter) {
	b.meter = m
}

// NativeSnapshot implements videobridge.NativeSnapshotter: the JC261/JC400
// takes the photo with its own command, without opening the RTMP stream.
func (b *Bridge) NativeSnapshot(ctx context.Context, deviceKey string, channel uint8) ([]byte, error) {
	if b.photos == nil {
		return nil, videobridge.ErrNativeSnapshotUnsupported
	}
	return b.photos.CapturePhoto(ctx, deviceKey, channel)
}

// NativeSnapshotChannels implements videobridge.NativeSnapshotGroup: the
// JC261/JC400 takes front and cabin with a single "Picture,inout#".
func (b *Bridge) NativeSnapshotChannels(string) []uint8 {
	return gt06KnownChannels
}

var (
	_ videobridge.NativeSnapshotter   = (*Bridge)(nil)
	_ videobridge.NativeSnapshotGroup = (*Bridge)(nil)
)
