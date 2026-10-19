package videobridge

import (
	"context"
	"encoding/json"
	"net/http"
	"sync"
	"testing"
	"time"
)

type fakeNativeProtocol struct {
	*fakeProtocol
	photo []byte
	calls int
}

func (f *fakeNativeProtocol) NativeSnapshot(ctx context.Context, deviceKey string, channel uint8) ([]byte, error) {
	f.calls++
	return f.photo, nil
}

func TestSnapshotNative_UnsupportedProtocolReturns501(t *testing.T) {
	proto := &fakeProtocol{name: "jt808", app: "rtp"}
	d := NewDispatcher(nil, NewTicketStore(), nil, proto)
	mux := http.NewServeMux()
	d.RegisterRoutes(mux)
	rec := postJSON(t, mux, "/api/v1/snapshot-native", `{"tenantId":"t1","protocol":"jt808","deviceKey":"013800000000","channel":1}`)
	var body map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	if body["code"] != float64(501) {
		t.Fatalf("protocol without native photo should answer code 501, got %s", rec.Body.String())
	}
}

func TestSnapshotNative_ReturnsPhotoAndCachesIt(t *testing.T) {
	photo := []byte("\xff\xd8\xff native photo")
	proto := &fakeNativeProtocol{fakeProtocol: &fakeProtocol{name: "gt06_video", app: "live"}, photo: photo}
	d := NewDispatcher(nil, NewTicketStore(), nil, proto)
	mux := http.NewServeMux()
	d.RegisterRoutes(mux)
	body := `{"tenantId":"t1","protocol":"gt06_video","deviceKey":"490154203237518","channel":0}`
	for i := 0; i < 2; i++ {
		rec := postJSON(t, mux, "/api/v1/snapshot-native", body)
		if rec.Header().Get("Content-Type") != "image/jpeg" || rec.Body.String() != string(photo) {
			t.Fatalf("request %d: expected the native photo, got %q", i, rec.Body.String())
		}
	}
	if proto.calls != 1 {
		t.Fatalf("the second request should come from the shared cache; the camera was queried %d times", proto.calls)
	}
	// The general preview photo path must also see the cache.
	rec := postJSON(t, mux, "/api/v1/snapshot-cache", `{"protocol":"gt06_video","deviceKey":"490154203237518","channel":0}`)
	if rec.Header().Get("Content-Type") != "image/jpeg" {
		t.Fatalf("the native photo was not stored in the shared cache")
	}
}

// fakeGroupProtocol: a dual-camera device that takes both photos together.
type fakeGroupProtocol struct {
	*fakeProtocol
	mu    sync.Mutex
	calls map[uint8]int
}

func (f *fakeGroupProtocol) NativeSnapshot(ctx context.Context, deviceKey string, channel uint8) ([]byte, error) {
	f.mu.Lock()
	f.calls[channel]++
	f.mu.Unlock()
	return []byte{0xff, 0xd8, 0xff, channel}, nil
}

func (f *fakeGroupProtocol) NativeSnapshotChannels(string) []uint8 { return []uint8{0, 1} }

// Requesting the front camera also requests the cabin one at the same time
// (a single command on the real device) and stores it in the shared cache:
// when its tile asks, it comes from the cache without bothering the camera
// again.
func TestSnapshotNative_CapturesSiblingIntoCache(t *testing.T) {
	proto := &fakeGroupProtocol{fakeProtocol: &fakeProtocol{name: "gt06_video", app: "live"}, calls: map[uint8]int{}}
	d := NewDispatcher(nil, NewTicketStore(), nil, proto)
	mux := http.NewServeMux()
	d.RegisterRoutes(mux)
	postJSON(t, mux, "/api/v1/snapshot-native", `{"tenantId":"t1","protocol":"gt06_video","deviceKey":"490154203237518","channel":0}`)

	deadline := time.Now().Add(2 * time.Second)
	for {
		if _, ok := d.snapshotCache.Get("gt06_video", "490154203237518", 1); ok {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the cabin photo was not stored in the cache")
		}
		time.Sleep(10 * time.Millisecond)
	}
	rec := postJSON(t, mux, "/api/v1/snapshot-native", `{"tenantId":"t1","protocol":"gt06_video","deviceKey":"490154203237518","channel":1}`)
	if rec.Header().Get("Content-Type") != "image/jpeg" {
		t.Fatalf("the cabin photo should come from the cache: %q", rec.Body.String())
	}
	proto.mu.Lock()
	defer proto.mu.Unlock()
	if proto.calls[0] != 1 || proto.calls[1] != 1 {
		t.Fatalf("camera calls = %v, want one per channel", proto.calls)
	}
}
