package videobridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
)

// NativeSnapshotter is OPTIONAL: a Protocol that can ask the device for a
// photo with its own command (without opening the video stream) implements
// it. Today: gt06_video (JC261/JC400, "Picture,out#"/"Picture,in#"). Much
// cheaper than the generic path in snapshot.go (start the stream + getSnap
// one frame): a photo is a few KB and does not load the media server. The
// Dispatcher uses it without knowing any protocol; adding a native photo to
// another model means implementing this interface.
type NativeSnapshotter interface {
	NativeSnapshot(ctx context.Context, deviceKey string, channel uint8) ([]byte, error)
}

// ErrNativeSnapshotUnsupported: the Protocol implements it but THIS device
// cannot (e.g. not configured) -- the caller falls back to the generic path.
var ErrNativeSnapshotUnsupported = errors.New("videobridge: this device does not support native photos")

// nativeSnapshotTimeout bounds the whole wait (command + photo upload over
// cellular data), slightly above the protocol's own limit.
const nativeSnapshotTimeout = 40 * time.Second

// --- POST /api/v1/snapshot-native ---
//
// Same body as /api/v1/snapshot. Responses:
//   - 200 image/jpeg: the photo.
//   - JSON code 501: the protocol has no native photo -> the API uses the
//     generic path (start video + getSnap).
//   - JSON code 504/502: the device did not deliver it -> the API may also
//     fall back to the generic path.
//
// Every delivered photo is recorded in usage_events ('download', with its
// real bytes -- every point that delivers bytes to a client is recorded) and
// stored in the shared cache, same as a capture from the generic path.
func (d *Dispatcher) handleSnapshotNative(w http.ResponseWriter, r *http.Request) {
	var req snapshotRequestBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.TenantID == "" || req.DeviceKey == "" {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": 400, "msg": "invalid body"})
		return
	}
	proto, ok := d.byName[req.Protocol]
	if !ok {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": 400, "msg": "unknown protocol"})
		return
	}
	native, ok := proto.(NativeSnapshotter)
	if !ok {
		writeJSON(w, http.StatusOK, map[string]any{"code": 501, "msg": "no native photo for this protocol"})
		return
	}
	if data, ok := d.snapshotCache.Get(req.Protocol, req.DeviceKey, req.Channel); ok {
		writeJPEG(w, data)
		return
	}

	// Sibling cameras: a dual-camera device (JC261/JC400) takes both photos
	// with ONE command, but if the second is requested a couple of seconds
	// later the camera answers "busy" for over 10 s (observed in the field)
	// and the preview fell back to the much more expensive video path. They
	// are requested together (arriving within the same grouping window, a
	// single "Picture,inout#") and the sibling photo lands in the shared
	// cache for when its tile asks for it.
	if group, ok := proto.(NativeSnapshotGroup); ok {
		for _, ch := range group.NativeSnapshotChannels(req.DeviceKey) {
			if ch == req.Channel {
				continue
			}
			if _, cached := d.snapshotCache.Get(req.Protocol, req.DeviceKey, ch); cached {
				continue
			}
			go d.captureSiblingSnapshot(proto, native, req.Protocol, req.DeviceKey, ch)
		}
	}

	ctx, cancel := context.WithTimeout(r.Context(), nativeSnapshotTimeout)
	defer cancel()
	data, err := native.NativeSnapshot(ctx, req.DeviceKey, req.Channel)
	if err != nil {
		code := 504
		if errors.Is(err, ErrNativeSnapshotUnsupported) {
			code = 501
		}
		log.Printf("videobridge: native photo of %s/%d failed: %v", req.DeviceKey, req.Channel, err)
		if code != 501 {
			d.recordNativeSnapshotFailure(proto, req.DeviceKey, req.Channel, err)
		}
		writeJSON(w, http.StatusOK, map[string]any{"code": code, "msg": "camera did not deliver the photo"})
		return
	}

	d.snapshotCache.Set(req.Protocol, req.DeviceKey, req.Channel, data)
	d.recordNativeSnapshotUsage(proto, req.DeviceKey, req.Channel, len(data))
	writeJPEG(w, data)
}

// NativeSnapshotGroup is optional for a NativeSnapshotter: the channels of a
// device that are captured with a single command (see handleSnapshotNative).
type NativeSnapshotGroup interface {
	NativeSnapshotChannels(deviceKey string) []uint8
}

func (d *Dispatcher) captureSiblingSnapshot(proto Protocol, native NativeSnapshotter, protocol, deviceKey string, channel uint8) {
	ctx, cancel := context.WithTimeout(context.Background(), nativeSnapshotTimeout)
	defer cancel()
	data, err := native.NativeSnapshot(ctx, deviceKey, channel)
	if err != nil {
		// Not a visible failure: nobody asked for it yet. If its tile asks
		// later, that request makes its own attempt.
		log.Printf("videobridge: sibling photo of %s/%d did not arrive (no effect): %v", deviceKey, channel, err)
		return
	}
	d.snapshotCache.Set(protocol, deviceKey, channel, data)
	d.recordNativeSnapshotUsage(proto, deviceKey, channel, len(data))
}

func writeJPEG(w http.ResponseWriter, data []byte) {
	w.Header().Set("Content-Type", "image/jpeg")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(data)
}

func (d *Dispatcher) recordNativeSnapshotUsage(proto Protocol, deviceKey string, channel uint8, n int) {
	if d.pool == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	err := db.WithBypass(ctx, d.pool, func(ctx context.Context, tx pgx.Tx) error {
		dev, err := proto.LookupDevice(ctx, tx, deviceKey)
		if err != nil {
			return err
		}
		return db.InsertUsageEvent(ctx, tx, dev.TenantID, dev.ID, "download", int64(n), map[string]any{
			"kind":    "native_snapshot",
			"channel": channel,
		})
	})
	if err != nil {
		log.Printf("videobridge: could not record usage_event for native photo of %s/%d: %v", deviceKey, channel, err)
	}
}

// recordNativeSnapshotFailure records a device health event for the
// platform: the native photo failed and the preview had to fall back to
// video (more device data). Deduplicated per channel: a persistent problem
// is ONE row with a counter.
func (d *Dispatcher) recordNativeSnapshotFailure(proto Protocol, deviceKey string, channel uint8, cause error) {
	if d.pool == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	err := db.WithBypass(ctx, d.pool, func(ctx context.Context, tx pgx.Tx) error {
		dev, err := proto.LookupDevice(ctx, tx, deviceKey)
		if err != nil {
			return err
		}
		return db.RecordDeviceHealthEvent(ctx, tx, dev.ID, "native_photo_failed", fmt.Sprintf("channel-%d", channel), "warning",
			"Camera photo failed; video was used instead (more data)",
			map[string]any{"channel": channel, "cause": cause.Error()}, 0)
	})
	if err != nil {
		log.Printf("videobridge: could not record health event for native photo of %s/%d: %v", deviceKey, channel, err)
	}
}

// CacheNativeSnapshot stores in the preview cache a native photo the device
// uploaded late (see alarmclip.SetPhotoSink) and records it as a download,
// same as a photo delivered on time.
func (d *Dispatcher) CacheNativeSnapshot(protocol, deviceKey string, channel uint8, data []byte) {
	proto, ok := d.byName[protocol]
	if !ok {
		return
	}
	d.snapshotCache.Set(protocol, deviceKey, channel, data)
	go d.recordNativeSnapshotUsage(proto, deviceKey, channel, len(data))
}
