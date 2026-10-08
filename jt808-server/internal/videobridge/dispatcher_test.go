package videobridge

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/jackc/pgx/v5"

	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
)

// dispatcher_test.go tests the Dispatcher's ROUTING. Unlike the
// jt1078bridge/gt06videobridge tests (which call their own handlers directly
// as http.HandlerFunc), these go through RegisterRoutes + the real
// *http.ServeMux via httptest, so a wiring error (wrong path, wrong HTTP
// method) is caught.
//
// fakeProtocol is a complete test Protocol, with no real Postgres/network:
// each field configures what each method returns, and the *Called booleans
// confirm the Dispatcher really delegated to the right implementation (not
// just that the HTTP response happened to look right).
type fakeProtocol struct {
	name, app string

	authorizePublishErr error
	streamNotFoundAllow bool
	idleStreamTracked   bool
	parseOK             bool
	parseDeviceKey      string
	parseChannel        uint8
	streamName          string
	// publishing is read by IsPublishing on every call (not fixed) so the
	// snapshot tests can simulate "starts publishing after a few polls" by
	// mutating it from a goroutine.
	publishing bool
	// pubMu protects publishing when a test changes it from another
	// goroutine (setPublishing) while the handler reads it.
	pubMu sync.Mutex
	// liveClaimed simulates a live viewer that already claimed the stream
	// (LiveViewClaimed) -- the snapshot must never cut it.
	liveClaimed bool

	publishCalled        bool
	streamNotFoundCalled bool
	streamStoppedCalled  bool
	idleStreamCalled     bool
	lookupDeviceCalled   bool
	isPublishingCalls    int
}

func (f *fakeProtocol) Name() string { return f.name }
func (f *fakeProtocol) App() string  { return f.app }

func (f *fakeProtocol) ParseStream(app, stream string) (string, uint8, bool) {
	return f.parseDeviceKey, f.parseChannel, f.parseOK
}

func (f *fakeProtocol) LookupDevice(ctx context.Context, tx pgx.Tx, deviceKey string) (db.Device, error) {
	f.lookupDeviceCalled = true
	return db.Device{}, errors.New("fakeProtocol: LookupDevice not implemented for this test")
}

func (f *fakeProtocol) AuthorizePublish(ctx context.Context, app, stream string) error {
	f.publishCalled = true
	return f.authorizePublishErr
}

func (f *fakeProtocol) HandleStreamNotFound(ctx context.Context, app, stream string) bool {
	f.streamNotFoundCalled = true
	return f.streamNotFoundAllow
}

func (f *fakeProtocol) HandleStreamStopped(app, stream string) {
	f.streamStoppedCalled = true
}

func (f *fakeProtocol) HandleIdleStream(deviceKey string, channel uint8) bool {
	f.idleStreamCalled = true
	return f.idleStreamTracked
}

func (f *fakeProtocol) StreamName(deviceKey string, channel uint8) string {
	return f.streamName
}

func (f *fakeProtocol) LiveViewClaimed(deviceKey string, channel uint8) bool {
	return f.liveClaimed
}

func (f *fakeProtocol) IsPublishing(deviceKey string, channel uint8) bool {
	f.pubMu.Lock()
	defer f.pubMu.Unlock()
	f.isPublishingCalls++
	return f.publishing
}

func (f *fakeProtocol) setPublishing(v bool) {
	f.pubMu.Lock()
	f.publishing = v
	f.pubMu.Unlock()
}

func newTestMux(protocols ...Protocol) *http.ServeMux {
	d := NewDispatcher(nil, NewTicketStore(), nil, protocols...)
	mux := http.NewServeMux()
	d.RegisterRoutes(mux)
	return mux
}

func postJSON(t *testing.T, mux *http.ServeMux, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, path, bytes.NewBufferString(body))
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	return rec
}

func decodeCode(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.NewDecoder(rec.Body).Decode(&out); err != nil {
		t.Fatalf("non-JSON response: %v (body: %q)", err, rec.Body.String())
	}
	return out
}

// --- POST /video-tickets: picks protocol by Name(), never by App() ---

func TestDispatcher_MintTicket_RoutesByNameAndUsesRealApp(t *testing.T) {
	jt := &fakeProtocol{name: "jt808", app: "rtp"}
	gt := &fakeProtocol{name: "gt06_video", app: "live"}
	tickets := NewTicketStore()
	d := NewDispatcher(nil, tickets, nil, jt, gt)
	mux := http.NewServeMux()
	d.RegisterRoutes(mux)

	rec := postJSON(t, mux, "/api/v1/video-tickets",
		`{"tenantId":"t1","terminalId":"imei-1","channel":1,"protocol":"gt06_video"}`)
	out := decodeCode(t, rec)
	if out["code"].(float64) != 0 {
		t.Fatalf("code = %v, want 0", out["code"])
	}
	token, _ := out["token"].(string)
	if token == "" {
		t.Fatal("no token in response")
	}

	// The ticket must carry the gt06_video protocol's App() ("live"), never
	// jt808's ("rtp") -- confirms the Dispatcher translated protocol->app
	// using the right Protocol, not a hardcoded value.
	ticket := tickets.Consume(token)
	if ticket == nil {
		t.Fatal("the issued token could not be consumed")
	}
	if ticket.App != "live" {
		t.Errorf("ticket.App = %q, want %q", ticket.App, "live")
	}
}

func TestDispatcher_NewDispatcher_SkipsProtocolWithEmptyIdentifiers(t *testing.T) {
	// Guardrail from a real bug (see the comment in NewDispatcher): a
	// protocol with an empty App()/Name() (deployment without that
	// integration configured) must never be registered -- otherwise it would
	// wrongly match any payload that also lacks the "app" field.
	empty := &fakeProtocol{name: "", app: ""}
	real := &fakeProtocol{name: "jt808", app: "rtp"}
	mux := newTestMux(empty, real)

	// A payload without "app" (like any legacy JT1078 payload that never
	// sets it) must not authorize via the empty protocol.
	rec := postJSON(t, mux, "/api/v1/on_publish", `{"app":"","stream":"x"}`)
	out := decodeCode(t, rec)
	if out["code"].(float64) == 0 {
		t.Error("a protocol with App()=\"\" was registered and authorized a payload without app")
	}
	if empty.publishCalled {
		t.Error("the empty protocol's AuthorizePublish was called -- it should not be registered")
	}
}

func TestDispatcher_MintTicket_UnknownProtocolRejected(t *testing.T) {
	mux := newTestMux(&fakeProtocol{name: "jt808", app: "rtp"})
	rec := postJSON(t, mux, "/api/v1/video-tickets",
		`{"tenantId":"t1","terminalId":"x","channel":0,"protocol":"does-not-exist"}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

// --- POST /on_publish: routes by App(), delegates to AuthorizePublish ---

func TestDispatcher_Publish_RoutesToCorrectProtocolByApp(t *testing.T) {
	jt := &fakeProtocol{name: "jt808", app: "rtp"}
	gt := &fakeProtocol{name: "gt06_video", app: "live", authorizePublishErr: nil}
	mux := newTestMux(jt, gt)

	rec := postJSON(t, mux, "/api/v1/on_publish", `{"app":"live","stream":"0/imei-1"}`)
	out := decodeCode(t, rec)
	if out["code"].(float64) != 0 {
		t.Fatalf("code = %v, want 0 (authorized)", out["code"])
	}
	if !gt.publishCalled {
		t.Error("gt06_video protocol's (app=live) AuthorizePublish was never called")
	}
	if jt.publishCalled {
		t.Error("jt808 protocol's (app=rtp) AuthorizePublish was called for an app=live push")
	}
}

func TestDispatcher_Publish_UnregisteredAppRejectedWithoutCallingAnyProtocol(t *testing.T) {
	jt := &fakeProtocol{name: "jt808", app: "rtp"}
	mux := newTestMux(jt)

	rec := postJSON(t, mux, "/api/v1/on_publish", `{"app":"unknown","stream":"x"}`)
	out := decodeCode(t, rec)
	if out["code"].(float64) == 0 {
		t.Error("unregistered app authorized, want rejection")
	}
	if jt.publishCalled {
		t.Error("AuthorizePublish called on a protocol whose App() does not match")
	}
}

func TestDispatcher_Publish_ProtocolErrorRejects(t *testing.T) {
	gt := &fakeProtocol{name: "gt06_video", app: "live", authorizePublishErr: errors.New("device not authorized")}
	mux := newTestMux(gt)

	rec := postJSON(t, mux, "/api/v1/on_publish", `{"app":"live","stream":"0/imei-1"}`)
	out := decodeCode(t, rec)
	if out["code"].(float64) == 0 {
		t.Error("AuthorizePublish returned an error but the Dispatcher authorized anyway")
	}
}

// --- POST /on_stream_not_found: routes by App(), delegates to HandleStreamNotFound ---

func TestDispatcher_StreamNotFound_RoutesAndRespectsAllow(t *testing.T) {
	allow := &fakeProtocol{name: "a", app: "app-a", streamNotFoundAllow: true}
	deny := &fakeProtocol{name: "b", app: "app-b", streamNotFoundAllow: false}
	mux := newTestMux(allow, deny)

	recAllow := postJSON(t, mux, "/api/v1/on_stream_not_found", `{"app":"app-a","stream":"x"}`)
	if decodeCode(t, recAllow)["code"].(float64) != 0 {
		t.Error("allow=true protocol rejected")
	}
	if !allow.streamNotFoundCalled {
		t.Error("the right protocol's HandleStreamNotFound was never called")
	}

	recDeny := postJSON(t, mux, "/api/v1/on_stream_not_found", `{"app":"app-b","stream":"x"}`)
	if decodeCode(t, recDeny)["code"].(float64) == 0 {
		t.Error("allow=false protocol authorized")
	}
}

func TestDispatcher_StreamNotFound_UnregisteredAppRejected(t *testing.T) {
	mux := newTestMux(&fakeProtocol{name: "a", app: "app-a"})
	rec := postJSON(t, mux, "/api/v1/on_stream_not_found", `{"app":"other","stream":"x"}`)
	if decodeCode(t, rec)["code"].(float64) == 0 {
		t.Error("unregistered app authorized")
	}
}

// --- POST /on_stream_changed: never rejects, only notifies the owning protocol ---

func TestDispatcher_StreamChanged_NotifiesOwnerOnlyWhenUnregistering(t *testing.T) {
	gt := &fakeProtocol{name: "gt06_video", app: "live"}
	mux := newTestMux(gt)

	// regist=true: must notify nobody (start, not stop).
	postJSON(t, mux, "/api/v1/on_stream_changed", `{"regist":true,"app":"live","stream":"0/imei-1"}`)
	if gt.streamStoppedCalled {
		t.Error("HandleStreamStopped called with regist=true")
	}

	// regist=false: must notify.
	rec := postJSON(t, mux, "/api/v1/on_stream_changed", `{"regist":false,"app":"live","stream":"0/imei-1"}`)
	if decodeCode(t, rec)["code"].(float64) != 0 {
		t.Error("on_stream_changed answered something other than code:0 -- this hook must never gate anything")
	}
	if !gt.streamStoppedCalled {
		t.Error("HandleStreamStopped never called with regist=false")
	}
}

// --- POST /on_play: consumes the ticket, compares app/deviceKey/channel ---

// newTestMuxWithTickets is like newTestMux but exposes the TicketStore so
// on_play tests can mint directly, controlling exactly which
// App/TerminalID/Channel the ticket carries without depending on each
// fakeProtocol's registered Name().
func newTestMuxWithTickets(protocols ...Protocol) (*http.ServeMux, *TicketStore) {
	tickets := NewTicketStore()
	d := NewDispatcher(nil, tickets, nil, protocols...)
	mux := http.NewServeMux()
	d.RegisterRoutes(mux)
	return mux, tickets
}

func zlmPlayPayload(app, stream, token string) string {
	b, _ := json.Marshal(map[string]string{
		"app":    app,
		"stream": stream,
		"schema": "rtmp",
		"params": "token=" + token,
	})
	return string(b)
}

func TestDispatcher_PlayAuth_AcceptsMatchingTicket(t *testing.T) {
	gt := &fakeProtocol{name: "gt06_video", app: "live", parseOK: true, parseDeviceKey: "imei-1", parseChannel: 1}
	mux, tickets := newTestMuxWithTickets(gt)
	token, err := tickets.Mint("t1", "imei-1", "live", 1)
	if err != nil {
		t.Fatalf("mint: %v", err)
	}

	rec := postJSON(t, mux, "/api/v1/on_play", zlmPlayPayload("live", "1/imei-1", token))
	if decodeCode(t, rec)["code"].(float64) != 0 {
		t.Error("valid ticket with matching app/deviceKey/channel rejected")
	}
}

func TestDispatcher_PlayAuth_RejectsTicketMintedForDifferentApp(t *testing.T) {
	// Security finding (F6): two protocols can have identifier columns with
	// INDEPENDENT unique constraints (nothing in the schema prevents one
	// protocol's terminalID from numerically matching ANOTHER tenant's
	// deviceKey in another protocol). Without comparing the App() it was
	// minted for, a ticket for one protocol authorized another's stream
	// when the identifiers collided as digits.
	jt := &fakeProtocol{name: "jt808", app: "rtp", parseOK: true, parseDeviceKey: "490154203237518", parseChannel: 1}
	gt := &fakeProtocol{name: "gt06_video", app: "live", parseOK: true, parseDeviceKey: "490154203237518", parseChannel: 1}
	mux, tickets := newTestMuxWithTickets(jt, gt)
	// Minted for "rtp" (jt808), with a terminalId that numerically matches
	// the deviceKey gt06_video would resolve.
	token, err := tickets.Mint("tenant-jt808", "490154203237518", "rtp", 1)
	if err != nil {
		t.Fatalf("mint: %v", err)
	}

	rec := postJSON(t, mux, "/api/v1/on_play", zlmPlayPayload("live", "1/490154203237518", token))
	if decodeCode(t, rec)["code"].(float64) == 0 {
		t.Error("ticket minted for app=rtp authorized the app=live stream -- namespace collision (F6)")
	}
}

func TestDispatcher_PlayAuth_RejectsDifferentDeviceKey(t *testing.T) {
	gt := &fakeProtocol{name: "gt06_video", app: "live", parseOK: true, parseDeviceKey: "imei-OTHER", parseChannel: 1}
	mux, tickets := newTestMuxWithTickets(gt)
	token, err := tickets.Mint("t1", "imei-1", "live", 1)
	if err != nil {
		t.Fatalf("mint: %v", err)
	}

	rec := postJSON(t, mux, "/api/v1/on_play", zlmPlayPayload("live", "1/imei-OTHER", token))
	if decodeCode(t, rec)["code"].(float64) == 0 {
		t.Error("one device's ticket authorized another's stream -- IDOR")
	}
}

func TestDispatcher_PlayAuth_RejectsDifferentChannel(t *testing.T) {
	gt := &fakeProtocol{name: "gt06_video", app: "live", parseOK: true, parseDeviceKey: "imei-1", parseChannel: 2}
	mux, tickets := newTestMuxWithTickets(gt)
	token, err := tickets.Mint("t1", "imei-1", "live", 1)
	if err != nil {
		t.Fatalf("mint: %v", err)
	}

	rec := postJSON(t, mux, "/api/v1/on_play", zlmPlayPayload("live", "2/imei-1", token))
	if decodeCode(t, rec)["code"].(float64) == 0 {
		t.Error("channel 1 ticket authorized channel 2")
	}
}

func TestDispatcher_PlayAuth_RejectsMissingOrUnknownToken(t *testing.T) {
	mux, _ := newTestMuxWithTickets(&fakeProtocol{name: "gt06_video", app: "live"})
	for _, params := range []string{"", "foo=bar", "token="} {
		body, _ := json.Marshal(map[string]string{"app": "live", "stream": "1/imei-1", "params": params})
		rec := postJSON(t, mux, "/api/v1/on_play", string(body))
		if decodeCode(t, rec)["code"].(float64) == 0 {
			t.Errorf("params=%q authorized without a valid token", params)
		}
	}
}

func TestDispatcher_PlayAuth_TicketIsSingleUse(t *testing.T) {
	gt := &fakeProtocol{name: "gt06_video", app: "live", parseOK: true, parseDeviceKey: "imei-1", parseChannel: 1}
	mux, tickets := newTestMuxWithTickets(gt)
	token, err := tickets.Mint("t1", "imei-1", "live", 1)
	if err != nil {
		t.Fatalf("mint: %v", err)
	}
	payload := zlmPlayPayload("live", "1/imei-1", token)

	first := decodeCode(t, postJSON(t, mux, "/api/v1/on_play", payload))
	if first["code"].(float64) != 0 {
		t.Fatalf("first use: code = %v, want 0", first["code"])
	}
	second := decodeCode(t, postJSON(t, mux, "/api/v1/on_play", payload))
	if second["code"].(float64) == 0 {
		t.Error("second use of the same ticket authorized -- the URL is still shareable")
	}
}

// --- POST /on_stream_none_reader: routes, respects HandleIdleStream ---

func TestDispatcher_StreamNoneReader_ClosesOnlyWhenProtocolTracksIt(t *testing.T) {
	tracked := &fakeProtocol{name: "a", app: "app-a", parseOK: true, idleStreamTracked: true}
	mux := newTestMux(tracked)

	rec := postJSON(t, mux, "/api/v1/on_stream_none_reader", `{"app":"app-a","stream":"x"}`)
	out := decodeCode(t, rec)
	if out["close"].(bool) != true {
		t.Error("close = false, want true when the protocol recognizes the stream as active")
	}
	if !tracked.idleStreamCalled {
		t.Error("HandleIdleStream never called")
	}
}

func TestDispatcher_StreamNoneReader_DoesNotCloseWhenNotTracked(t *testing.T) {
	untracked := &fakeProtocol{name: "a", app: "app-a", parseOK: true, idleStreamTracked: false}
	mux := newTestMux(untracked)

	rec := postJSON(t, mux, "/api/v1/on_stream_none_reader", `{"app":"app-a","stream":"x"}`)
	out := decodeCode(t, rec)
	if out["close"].(bool) != false {
		t.Error("close = true for a stream the protocol does not recognize as active")
	}
}

func TestDispatcher_StreamNoneReader_UnparseableStreamNeverCloses(t *testing.T) {
	proto := &fakeProtocol{name: "a", app: "app-a", parseOK: false}
	mux := newTestMux(proto)

	rec := postJSON(t, mux, "/api/v1/on_stream_none_reader", `{"app":"app-a","stream":"x"}`)
	out := decodeCode(t, rec)
	if out["close"].(bool) != false {
		t.Error("close = true with a stream ParseStream could not parse")
	}
	if proto.idleStreamCalled {
		t.Error("HandleIdleStream called even though ParseStream failed")
	}
}
