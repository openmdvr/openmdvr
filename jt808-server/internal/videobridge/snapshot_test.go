package videobridge

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// snapshot_test.go tests the POST /api/v1/snapshot orchestration against a
// FAKE ZLMediaKit server (httptest.Server), never a real ZLM (that is
// covered by end-to-end testing against the dev Docker stack). fakeZLM
// answers getSnap/close_streams exactly like the real ZLM (confirmed
// empirically): getSnap ALWAYS returns HTTP 200 and the Content-Type
// distinguishes a real capture (image/jpeg) from the placeholder image
// (image/png) -- never an error status.

// shrinkSnapshotTimers shrinks the wait tunables to milliseconds so these
// tests do not take real seconds, restoring the originals afterwards
// (t.Cleanup) so test timers never leak into other tests.
func shrinkSnapshotTimers(t *testing.T) {
	t.Helper()
	origWait, origPoll, origGrace := snapshotWaitTimeout, snapshotPollInterval, snapshotKeyframeGrace
	snapshotWaitTimeout = 300 * time.Millisecond
	snapshotPollInterval = 20 * time.Millisecond
	snapshotKeyframeGrace = 10 * time.Millisecond
	t.Cleanup(func() {
		snapshotWaitTimeout, snapshotPollInterval, snapshotKeyframeGrace = origWait, origPoll, origGrace
	})
}

type fakeZLMResponse struct {
	contentType string
	body        []byte
}

// zlmCallLog records which ZLM API endpoints were actually called. Each test
// asserts explicitly what it expects (never an implicit/global expectation),
// because "stop the stream" only applies when a real publish was confirmed
// (see TestHandleSnapshot_NeverPublishing_TimesOut, which deliberately
// expects NO ZLM API call).
type zlmCallLog struct {
	getSnapCalled      bool
	getSnapCallCount   int
	closeStreamsCalled bool
	// readerCount is what getMediaList answers (connected viewers) -- 0 by
	// default, each test sets it if needed.
	readerCount int
}

// newFakeZLM starts a test server that answers getSnap with snapResp and
// close_streams with {"code":0} -- enough to exercise the WHOLE real
// handleSnapshot path without a real ZLMediaKit.
func newFakeZLM(t *testing.T, snapResp fakeZLMResponse) (*ZLMClient, *zlmCallLog) {
	t.Helper()
	return newFakeZLMSequenced(t, []fakeZLMResponse{snapResp})
}

// newFakeZLMSequenced returns a DIFFERENT getSnap response for each
// successive call (the last one repeats if there are more calls than
// responses) -- needed to test handleSnapshot's single retry (see
// TestHandleSnapshot_RetriesOnceOnFallbackThenSucceeds).
func newFakeZLMSequenced(t *testing.T, snapResps []fakeZLMResponse) (*ZLMClient, *zlmCallLog) {
	t.Helper()
	log := &zlmCallLog{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/index/api/getSnap":
			idx := log.getSnapCallCount
			if idx >= len(snapResps) {
				idx = len(snapResps) - 1
			}
			resp := snapResps[idx]
			log.getSnapCalled = true
			log.getSnapCallCount++
			w.Header().Set("Content-Type", resp.contentType)
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(resp.body)
		case "/index/api/getMediaList":
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = fmt.Fprintf(w, `{"code":0,"data":[{"schema":"rtmp","totalReaderCount":%d}]}`, log.readerCount)
		case "/index/api/close_streams":
			log.closeStreamsCalled = true
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"code":0,"count_hit":1}`))
		default:
			t.Errorf("unexpected ZLM client call to %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	return &ZLMClient{baseURL: srv.URL, secret: "test-secret", http: srv.Client()}, log
}

func TestHandleSnapshot_WaitsForPublishThenReturnsRealImage(t *testing.T) {
	shrinkSnapshotTimers(t)
	realJPEG := []byte("\xff\xd8\xff fake jpeg bytes")
	zlm, calls := newFakeZLM(t, fakeZLMResponse{contentType: "image/jpeg", body: realJPEG})

	proto := &fakeProtocol{name: "gt06_video", app: "live", streamName: "0/imei-1", idleStreamTracked: true}
	tickets := NewTicketStore()
	d := NewDispatcher(nil, tickets, zlm, proto)
	mux := http.NewServeMux()
	d.RegisterRoutes(mux)

	// Simulate on_publish confirming the real session AFTER the handler has
	// started polling -- exercises the wait path, not just "already
	// publishing".
	go func() {
		time.Sleep(40 * time.Millisecond)
		proto.setPublishing(true)
	}()

	rec := postJSON(t, mux, "/api/v1/snapshot", `{"tenantId":"t1","protocol":"gt06_video","deviceKey":"imei-1","channel":0}`)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body: %s)", rec.Code, rec.Body.String())
	}
	if ct := rec.Header().Get("Content-Type"); ct != "image/jpeg" {
		t.Errorf("Content-Type = %q, want image/jpeg", ct)
	}
	if rec.Body.String() != string(realJPEG) {
		t.Errorf("body = %q, want the real JPEG bytes returned by getSnap", rec.Body.String())
	}
	if proto.isPublishingCalls < 2 {
		t.Errorf("IsPublishing called %d times, want at least 2 (real polling before confirmation)", proto.isPublishingCalls)
	}
	if !proto.idleStreamCalled {
		t.Error("HandleIdleStream never called -- the stream must be stopped right after the capture")
	}
	if !calls.getSnapCalled || !calls.closeStreamsCalled {
		t.Errorf("ZLM calls = %+v, want both true", calls)
	}
}

func TestHandleSnapshot_NeverPublishing_TimesOut(t *testing.T) {
	shrinkSnapshotTimers(t)
	zlm, calls := newFakeZLM(t, fakeZLMResponse{contentType: "image/jpeg", body: []byte("should not be called")})

	proto := &fakeProtocol{name: "gt06_video", app: "live", streamName: "0/imei-1", publishing: false}
	d := NewDispatcher(nil, NewTicketStore(), zlm, proto)
	mux := http.NewServeMux()
	d.RegisterRoutes(mux)

	rec := postJSON(t, mux, "/api/v1/snapshot", `{"tenantId":"t1","protocol":"gt06_video","deviceKey":"imei-1","channel":0}`)
	out := decodeCode(t, rec)
	if out["code"].(float64) != 504 {
		t.Errorf("code = %v, want 504 (publish never confirmed)", out["code"])
	}
	// If the device never confirmed publishing there is nothing real to
	// close -- handleSnapshot must give up BEFORE attempting capture or stop,
	// never calling the ZLM API on this path.
	if calls.getSnapCalled || calls.closeStreamsCalled {
		t.Errorf("ZLM calls = %+v, want both false (no real publish was confirmed)", calls)
	}
	if proto.idleStreamCalled {
		t.Error("HandleIdleStream called even though no real session was confirmed")
	}
}

func TestHandleSnapshot_FallbackImageDetected(t *testing.T) {
	shrinkSnapshotTimers(t)
	// The placeholder ZLMediaKit serves when it cannot capture a frame:
	// Content-Type image/png, NEVER jpeg (see zlm.go::GetSnap).
	zlm, calls := newFakeZLM(t, fakeZLMResponse{contentType: "image/png", body: []byte("fake png fallback")})

	proto := &fakeProtocol{name: "gt06_video", app: "live", streamName: "0/imei-1", publishing: true, idleStreamTracked: true}
	d := NewDispatcher(nil, NewTicketStore(), zlm, proto)
	mux := http.NewServeMux()
	d.RegisterRoutes(mux)

	rec := postJSON(t, mux, "/api/v1/snapshot", `{"tenantId":"t1","protocol":"gt06_video","deviceKey":"imei-1","channel":0}`)
	out := decodeCode(t, rec)
	if out["code"].(float64) != 502 {
		t.Errorf("code = %v, want 502 (placeholder detected, never served as real)", out["code"])
	}
	if !proto.idleStreamCalled {
		t.Error("HandleIdleStream never called -- the stream must be stopped even if the capture failed")
	}
	if !calls.closeStreamsCalled {
		t.Error("close_streams never called -- the stream must be stopped even if the capture failed")
	}
	// The single retry (see TestHandleSnapshot_RetriesOnceOnFallbackThenSucceeds)
	// is also exhausted if the second attempt fails again -- two real getSnap
	// calls, never more.
	if calls.getSnapCallCount != 2 {
		t.Errorf("getSnap called %d times, want 2 (one attempt + one retry, both placeholder)", calls.getSnapCallCount)
	}
}

// TestHandleSnapshot_RetriesOnceOnFallbackThenSucceeds covers a field case:
// a dual-camera gt06_video device (RTMP,ON,INOUT# starts both with one
// command) sharing the same cellular uplink can make ONE channel lose the
// race for its first keyframe while the other captures fine -- the single
// retry must recover that case instead of giving up on the first attempt.
func TestHandleSnapshot_RetriesOnceOnFallbackThenSucceeds(t *testing.T) {
	shrinkSnapshotTimers(t)
	realJPEG := []byte("\xff\xd8\xff real jpeg on the second try")
	zlm, calls := newFakeZLMSequenced(t, []fakeZLMResponse{
		{contentType: "image/png", body: []byte("fake png fallback (first attempt)")},
		{contentType: "image/jpeg", body: realJPEG},
	})

	proto := &fakeProtocol{name: "gt06_video", app: "live", streamName: "1/imei-1", publishing: true, idleStreamTracked: true}
	d := NewDispatcher(nil, NewTicketStore(), zlm, proto)
	mux := http.NewServeMux()
	d.RegisterRoutes(mux)

	rec := postJSON(t, mux, "/api/v1/snapshot", `{"tenantId":"t1","protocol":"gt06_video","deviceKey":"imei-1","channel":1}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body: %s)", rec.Code, rec.Body.String())
	}
	if ct := rec.Header().Get("Content-Type"); ct != "image/jpeg" {
		t.Errorf("Content-Type = %q, want image/jpeg", ct)
	}
	if rec.Body.String() != string(realJPEG) {
		t.Errorf("body = %q, want the real bytes from the SECOND attempt", rec.Body.String())
	}
	if calls.getSnapCallCount != 2 {
		t.Errorf("getSnap called %d times, want 2 (one failed attempt + one successful retry)", calls.getSnapCallCount)
	}
}

// shrinkSnapshotCacheTTL shrinks snapshotCacheTTL for real expiry tests
// (without mocking time.Now) -- same pattern as shrinkSnapshotTimers.
func shrinkSnapshotCacheTTL(t *testing.T, ttl time.Duration) {
	t.Helper()
	orig := snapshotCacheTTL
	snapshotCacheTTL = ttl
	t.Cleanup(func() { snapshotCacheTTL = orig })
}

// TestHandleSnapshot_CacheHit_SkipsRealCaptureOnSecondRequest: reloading the
// page or viewing the same device from another session must not wake the
// camera again if a recent photo exists -- the second request must never
// touch IsPublishing, HandleIdleStream or the ZLM API.
func TestHandleSnapshot_CacheHit_SkipsRealCaptureOnSecondRequest(t *testing.T) {
	shrinkSnapshotTimers(t)
	realJPEG := []byte("\xff\xd8\xff fake jpeg bytes")
	zlm, calls := newFakeZLM(t, fakeZLMResponse{contentType: "image/jpeg", body: realJPEG})

	proto := &fakeProtocol{name: "gt06_video", app: "live", streamName: "0/imei-1", publishing: true, idleStreamTracked: true}
	d := NewDispatcher(nil, NewTicketStore(), zlm, proto)
	mux := http.NewServeMux()
	d.RegisterRoutes(mux)

	body := `{"tenantId":"t1","protocol":"gt06_video","deviceKey":"imei-1","channel":0}`

	rec1 := postJSON(t, mux, "/api/v1/snapshot", body)
	if rec1.Code != http.StatusOK || rec1.Body.String() != string(realJPEG) {
		t.Fatalf("first request: status=%d body=%q, want 200 with the real bytes", rec1.Code, rec1.Body.String())
	}
	if !calls.getSnapCalled {
		t.Fatal("first request: getSnap never called -- it should be a real capture (empty cache)")
	}

	// "Reload" / "a different session" -- tenantId does not matter, the
	// cache is per device+channel, never per tenant/user (authorization of
	// EACH request is resolved by the API before reaching here). Reset the
	// counters to assert the SECOND request touches none of that again.
	calls.getSnapCalled, calls.closeStreamsCalled = false, false
	proto.idleStreamCalled = false
	proto.isPublishingCalls = 0

	rec2 := postJSON(t, mux, "/api/v1/snapshot", body)
	if rec2.Code != http.StatusOK {
		t.Fatalf("second request: status = %d, want 200", rec2.Code)
	}
	if ct := rec2.Header().Get("Content-Type"); ct != "image/jpeg" {
		t.Errorf("second request: Content-Type = %q, want image/jpeg", ct)
	}
	if rec2.Body.String() != string(realJPEG) {
		t.Errorf("second request: body = %q, want the SAME cached bytes", rec2.Body.String())
	}
	if calls.getSnapCalled || calls.closeStreamsCalled {
		t.Errorf("second request: ZLM calls = %+v, want both false (should be served from cache)", calls)
	}
	if proto.idleStreamCalled {
		t.Error("second request: HandleIdleStream called -- a cache hit must never touch the real stream")
	}
	if proto.isPublishingCalls != 0 {
		t.Errorf("second request: IsPublishing called %d times, want 0 (never poll on a hit)", proto.isPublishingCalls)
	}
}

// TestSnapshotCache_ExpiresAfterTTL confirms an expired entry is treated as
// absent -- a stale photo is never served silently, and the next request
// DOES trigger a new real capture.
func TestSnapshotCache_ExpiresAfterTTL(t *testing.T) {
	shrinkSnapshotTimers(t)
	shrinkSnapshotCacheTTL(t, 30*time.Millisecond)
	realJPEG := []byte("\xff\xd8\xff fake jpeg bytes")
	zlm, calls := newFakeZLM(t, fakeZLMResponse{contentType: "image/jpeg", body: realJPEG})

	proto := &fakeProtocol{name: "gt06_video", app: "live", streamName: "0/imei-1", publishing: true, idleStreamTracked: true}
	d := NewDispatcher(nil, NewTicketStore(), zlm, proto)
	mux := http.NewServeMux()
	d.RegisterRoutes(mux)
	body := `{"tenantId":"t1","protocol":"gt06_video","deviceKey":"imei-1","channel":0}`

	postJSON(t, mux, "/api/v1/snapshot", body)
	calls.getSnapCalled = false
	time.Sleep(60 * time.Millisecond) // > snapshotCacheTTL

	rec := postJSON(t, mux, "/api/v1/snapshot", body)
	if rec.Code != http.StatusOK || rec.Body.String() != string(realJPEG) {
		t.Fatalf("after cache expiry: status=%d body=%q, want 200 with a new real capture", rec.Code, rec.Body.String())
	}
	if !calls.getSnapCalled {
		t.Error("after cache expiry: getSnap never called -- an expired entry must not be served")
	}
}

func TestHandleSnapshotCache_MissWhenNothingCaptured(t *testing.T) {
	mux := newTestMux(&fakeProtocol{name: "gt06_video", app: "live"})
	rec := postJSON(t, mux, "/api/v1/snapshot-cache", `{"protocol":"gt06_video","deviceKey":"imei-never-seen","channel":0}`)
	out := decodeCode(t, rec)
	if out["code"].(float64) != 404 {
		t.Errorf("code = %v, want 404 (nothing was ever captured for this device+channel)", out["code"])
	}
}

func TestHandleSnapshotCache_HitAfterRealCapture(t *testing.T) {
	shrinkSnapshotTimers(t)
	realJPEG := []byte("\xff\xd8\xff fake jpeg bytes")
	zlm, _ := newFakeZLM(t, fakeZLMResponse{contentType: "image/jpeg", body: realJPEG})
	proto := &fakeProtocol{name: "gt06_video", app: "live", streamName: "0/imei-1", publishing: true, idleStreamTracked: true}
	d := NewDispatcher(nil, NewTicketStore(), zlm, proto)
	mux := http.NewServeMux()
	d.RegisterRoutes(mux)

	postJSON(t, mux, "/api/v1/snapshot", `{"tenantId":"t1","protocol":"gt06_video","deviceKey":"imei-1","channel":0}`)

	rec := postJSON(t, mux, "/api/v1/snapshot-cache", `{"protocol":"gt06_video","deviceKey":"imei-1","channel":0}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "image/jpeg" {
		t.Errorf("Content-Type = %q, want image/jpeg", ct)
	}
	if rec.Body.String() != string(realJPEG) {
		t.Errorf("body = %q, want the real bytes already captured", rec.Body.String())
	}
}

func TestHandleSnapshotCache_MissingFieldsRejected(t *testing.T) {
	mux := newTestMux(&fakeProtocol{name: "gt06_video", app: "live"})
	rec := postJSON(t, mux, "/api/v1/snapshot-cache", `{"protocol":"gt06_video","channel":0}`) // no deviceKey
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

func TestHandleSnapshot_UnknownProtocolRejected(t *testing.T) {
	mux := newTestMux(&fakeProtocol{name: "gt06_video", app: "live"})
	rec := postJSON(t, mux, "/api/v1/snapshot", `{"tenantId":"t1","protocol":"does-not-exist","deviceKey":"x","channel":0}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

func TestHandleSnapshot_MissingFieldsRejected(t *testing.T) {
	mux := newTestMux(&fakeProtocol{name: "gt06_video", app: "live"})
	for _, body := range []string{
		`{"protocol":"gt06_video","deviceKey":"x","channel":0}`, // no tenantId
		`{"tenantId":"t1","protocol":"gt06_video","channel":0}`, // no deviceKey
	} {
		rec := postJSON(t, mux, "/api/v1/snapshot", body)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("body=%s: status = %d, want 400", body, rec.Code)
		}
	}
}

// Regression: opening both cameras of a JC261 made the preview photo finish
// and cut the stream the live window had already requested. With a live
// video request claiming the stream, the snapshot must NEVER cut it.
func TestHandleSnapshot_DoesNotCutStreamClaimedForLiveView(t *testing.T) {
	shrinkSnapshotTimers(t)
	zlm, calls := newFakeZLM(t, fakeZLMResponse{contentType: "image/jpeg", body: []byte("\xff\xd8 jpeg")})
	proto := &fakeProtocol{name: "gt06_video", app: "live", streamName: "0/imei-1", idleStreamTracked: true, publishing: true, liveClaimed: true}
	d := NewDispatcher(nil, NewTicketStore(), zlm, proto)
	mux := http.NewServeMux()
	d.RegisterRoutes(mux)

	rec := postJSON(t, mux, "/api/v1/snapshot", `{"tenantId":"t1","protocol":"gt06_video","deviceKey":"imei-1","channel":0}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body: %s)", rec.Code, rec.Body.String())
	}
	if proto.idleStreamCalled || calls.closeStreamsCalled {
		t.Errorf("the snapshot cut a stream claimed for live view (idle=%v close=%v)", proto.idleStreamCalled, calls.closeStreamsCalled)
	}
}

// Same bug, other signal: someone is already connected watching the stream
// (ZLM reports readers) even if the protocol does not mark it as claimed.
func TestHandleSnapshot_DoesNotCutStreamWithViewers(t *testing.T) {
	shrinkSnapshotTimers(t)
	zlm, calls := newFakeZLM(t, fakeZLMResponse{contentType: "image/jpeg", body: []byte("\xff\xd8 jpeg")})
	calls.readerCount = 1
	proto := &fakeProtocol{name: "gt06_video", app: "live", streamName: "0/imei-1", idleStreamTracked: true, publishing: true}
	d := NewDispatcher(nil, NewTicketStore(), zlm, proto)
	mux := http.NewServeMux()
	d.RegisterRoutes(mux)

	rec := postJSON(t, mux, "/api/v1/snapshot", `{"tenantId":"t1","protocol":"gt06_video","deviceKey":"imei-1","channel":0}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body: %s)", rec.Code, rec.Body.String())
	}
	if proto.idleStreamCalled || calls.closeStreamsCalled {
		t.Errorf("the snapshot cut a stream with connected viewers (idle=%v close=%v)", proto.idleStreamCalled, calls.closeStreamsCalled)
	}
}
