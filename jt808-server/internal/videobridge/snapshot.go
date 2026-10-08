package videobridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"time"
)

// Low-cost photo capture for the camera preview: show a recent photo by
// default instead of forcing a full live view (much more expensive in
// data). It reuses the already-audited live video pipeline for a few seconds
// instead of a native photo command (JT808 0x8801 is not implemented); see
// snapshot_native.go for devices that do support a native photo.
//
// This file lives in videobridge (never in jt1078bridge/gt06videobridge)
// because it needs nothing protocol-specific beyond the Protocol interface:
// a new protocol gets snapshots for free just by implementing it.
//
// Shared cache (snapshot_cache.go): reloading the page or viewing the same
// device from another tab/device would otherwise trigger a real capture
// every time -- at thousands of devices, exactly the data cost this feature
// exists to avoid. The latest real photo per device+channel is served to ANY
// session asking within snapshotCacheTTL, whatever tenant it comes from
// (authorization of EACH request is resolved by the API before reaching
// here).
//
// Tunables are vars (not const) on purpose: tests shrink them to avoid real
// multi-second waits that must be generous in production (see
// snapshot_test.go).
var (
	// snapshotWaitTimeout: how long to wait for on_publish to confirm a real
	// session before giving up. IsPublishing is a cheap in-memory lookup, so
	// short polling does not warrant a channel-based wait; not a hot path.
	snapshotWaitTimeout  = 8 * time.Second
	snapshotPollInterval = 200 * time.Millisecond
	// snapshotKeyframeGrace: extra margin AFTER on_publish is confirmed.
	// on_publish confirms the source exists, not that a real decodable
	// H.264 keyframe has arrived (the GOP interval can be a couple of
	// seconds). Without this margin getSnap risks capturing before the first
	// keyframe and returning the placeholder image.
	snapshotKeyframeGrace = 1500 * time.Millisecond
)

const snapshotZLMTimeoutSec = 5

type snapshotRequestBody struct {
	TenantID  string `json:"tenantId"`
	Protocol  string `json:"protocol"`
	DeviceKey string `json:"deviceKey"`
	Channel   uint8  `json:"channel"`
}

// handleSnapshot orchestrates the capture: waits until the device is REALLY
// publishing (the API already triggered the real signaling by calling the
// protocol's own "request video" endpoint BEFORE calling here -- this
// handler NEVER starts signaling on its own), mints a one-time ticket to
// authorize the internal read (same mechanism as any real playback, see
// zlm.go::GetSnap), asks ZLMediaKit for a frame, and stops the stream
// IMMEDIATELY -- never waiting for the normal "no viewers" timeout (~20s) --
// reusing the same audited stop path as on_stream_none_reader.
func (d *Dispatcher) handleSnapshot(w http.ResponseWriter, r *http.Request) {
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

	// Cache first, defense in depth: the API should already have called
	// /api/v1/snapshot-cache BEFORE starting the real signaling (the common
	// hit case never reaches here), but if two nearly simultaneous requests
	// pass the peek, this check still avoids the second physical capture.
	if data, ok := d.snapshotCache.Get(req.Protocol, req.DeviceKey, req.Channel); ok {
		w.Header().Set("Content-Type", "image/jpeg")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(data)
		return
	}

	waitCtx, cancelWait := context.WithTimeout(r.Context(), snapshotWaitTimeout)
	publishing := waitForPublish(waitCtx, proto, req.DeviceKey, req.Channel)
	cancelWait()
	if !publishing {
		writeJSON(w, http.StatusOK, map[string]any{"code": 504, "msg": "device did not confirm streaming in time"})
		return
	}

	select {
	case <-time.After(snapshotKeyframeGrace):
	case <-r.Context().Done():
		writeJSON(w, http.StatusOK, map[string]any{"code": 504, "msg": "timed out"})
		return
	}

	stream := proto.StreamName(req.DeviceKey, req.Channel)
	data, snapErr := d.captureFrame(r.Context(), proto, req, stream)
	// One retry when the first attempt falls back to the placeholder. Seen
	// in the field: on a DUAL-camera device (RTMP,ON,INOUT# starts both with
	// one command) both share the same cellular uplink, and one can lose the
	// race for its first keyframe while the OTHER captures fine in the same
	// request. The stream is already active anyway, so this extra wait costs
	// the device nothing new; it just gives the keyframe another chance.
	if errors.Is(snapErr, ErrSnapFallbackImage) {
		select {
		case <-time.After(snapshotKeyframeGrace):
			data, snapErr = d.captureFrame(r.Context(), proto, req, stream)
		case <-r.Context().Done():
		}
	}

	// ALWAYS stop the stream immediately, whether the capture succeeded or
	// not -- never leave the device streaming because of an error on our
	// side. HandleIdleStream already sends the real stop command (stop_video
	// on GT06, with its own "do not stop if the other channel is active"
	// guard) and CloseMediaStream is the forced close on the ZLM side -- the
	// SAME two actions the normal on_stream_none_reader path triggers, just
	// immediately instead of after the usual ~20s.
	//
	// EXCEPT when someone is watching (or just asked to watch) this same
	// stream LIVE: otherwise, opening both cameras of a JC261 made the
	// preview photo finish and cut the stream the live window was using.
	// Two signals, neither sufficient alone: LiveViewClaimed covers the
	// window where the viewer already requested video but is still
	// negotiating WebRTC (not yet a reader in ZLM), and ReaderCount covers
	// any already-connected viewer. If nobody claims it and there are no
	// readers, stop as usual; if ZLM cannot be queried, assume someone might
	// be watching (the normal no-viewer stop still arrives within ~20s, so it
	// is never unbounded).
	closeCtx, cancelClose := context.WithTimeout(context.Background(), 5*time.Second)
	if keep, why := d.snapshotShouldKeepStream(closeCtx, proto, req.DeviceKey, req.Channel, stream); keep {
		log.Printf("videobridge: snapshot of %s/%d does not stop the stream: %s", req.DeviceKey, req.Channel, why)
	} else {
		proto.HandleIdleStream(req.DeviceKey, req.Channel)
		if closeErr := d.zlm.CloseMediaStream(closeCtx, proto.App(), stream); closeErr != nil {
			log.Printf("videobridge: snapshot could not force-close %s/%s: %v", proto.App(), stream, closeErr)
		}
	}
	cancelClose()

	if snapErr != nil {
		if errors.Is(snapErr, ErrSnapFallbackImage) {
			log.Printf("videobridge: snapshot of %s/%d returned the placeholder image (no keyframe in time)", req.DeviceKey, req.Channel)
			writeJSON(w, http.StatusOK, map[string]any{"code": 502, "msg": "could not capture a real image"})
			return
		}
		log.Printf("videobridge: snapshot of %s/%d failed: %v", req.DeviceKey, req.Channel, snapErr)
		writeJSON(w, http.StatusOK, map[string]any{"code": 500, "msg": "error capturing image"})
		return
	}

	// Store for the NEXT session that asks for this device+channel, whatever
	// tenant/user it comes from -- see snapshot_cache.go. Only after a REAL
	// capture (never reached if snapErr != nil), never ZLM's placeholder.
	d.snapshotCache.Set(req.Protocol, req.DeviceKey, req.Channel, data)

	w.Header().Set("Content-Type", "image/jpeg")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(data)
}

// snapshotShouldKeepStream decides whether the immediate stop after a photo
// must be skipped because there is (or is about to be) a live viewer -- see
// the comment in handleSnapshot.
func (d *Dispatcher) snapshotShouldKeepStream(ctx context.Context, proto Protocol, deviceKey string, channel uint8, stream string) (keep bool, why string) {
	if proto.LiveViewClaimed(deviceKey, channel) {
		return true, "there is a live video request for this channel"
	}
	if d.zlm == nil {
		return false, ""
	}
	n, err := d.zlm.ReaderCount(ctx, proto.App(), stream)
	if err != nil {
		return true, fmt.Sprintf("could not query viewers (%v), leaving it to the normal idle stop", err)
	}
	if n > 0 {
		return true, fmt.Sprintf("%d viewer(s) connected", n)
	}
	return false, ""
}

// captureFrame mints its OWN one-time ticket and asks ZLMediaKit for a
// frame. Extracted so it can be called twice (see the retry in
// handleSnapshot) WITHOUT reusing the same token: on_play consumes the
// ticket as soon as getSnap opens its internal subscription, keyframe or
// not, so retrying with an already-consumed token would always fail as
// "unauthorized" instead of getting a real second chance.
func (d *Dispatcher) captureFrame(ctx context.Context, proto Protocol, req snapshotRequestBody, stream string) ([]byte, error) {
	token, err := d.tickets.Mint(req.TenantID, req.DeviceKey, proto.App(), req.Channel)
	if err != nil {
		log.Printf("videobridge: snapshot could not mint ticket for %s: %v", req.DeviceKey, err)
		return nil, err
	}
	snapCtx, cancel := context.WithTimeout(context.Background(), time.Duration(snapshotZLMTimeoutSec+5)*time.Second)
	defer cancel()
	return d.zlm.GetSnap(snapCtx, proto.App(), stream, token, snapshotZLMTimeoutSec)
}

// handleSnapshotCache is the "cheap" half of the capture: it checks the
// shared cache WITHOUT touching the device or the video signaling. The API
// calls it first (see api/app/routers/video.py) to decide whether the real
// cost of waking the camera is needed. A miss is not an error, it is the
// expected answer the first time a device's photo is requested -- same
// generic error shape as the rest of this file, never a real HTTP 404 (the
// API distinguishes by Content-Type, not status code).
func (d *Dispatcher) handleSnapshotCache(w http.ResponseWriter, r *http.Request) {
	var req snapshotRequestBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Protocol == "" || req.DeviceKey == "" {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": 400, "msg": "invalid body"})
		return
	}
	if data, ok := d.snapshotCache.Get(req.Protocol, req.DeviceKey, req.Channel); ok {
		w.Header().Set("Content-Type", "image/jpeg")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(data)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"code": 404, "msg": "no recent photo in cache"})
}

// waitForPublish polls Protocol.IsPublishing (pure query, see protocol.go --
// NEVER HandleIdleStream, which has real side effects) until it confirms a
// real session or ctx expires.
func waitForPublish(ctx context.Context, proto Protocol, deviceKey string, channel uint8) bool {
	if proto.IsPublishing(deviceKey, channel) {
		return true
	}
	ticker := time.NewTicker(snapshotPollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return false
		case <-ticker.C:
			if proto.IsPublishing(deviceKey, channel) {
				return true
			}
		}
	}
}
