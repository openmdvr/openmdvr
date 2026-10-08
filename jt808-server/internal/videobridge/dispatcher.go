package videobridge

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"net/url"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
)

// Dispatcher is the shared dispatch point for ZLMediaKit hooks (ZLM only
// accepts ONE global URL per event type, never one per app), plus the ticket
// minting, snapshot capture (and its cache-only variant, see
// snapshot_cache.go) and live-balance endpoints. The remaining endpoints
// (request video for a specific device, one per protocol: POST /api/v1/9101
// for JT1078, POST /api/v1/gt06-video for GT06) are 100% protocol-specific
// and are mounted directly by their own packages.
//
// Registering a new Protocol means passing it to NewDispatcher. The
// Dispatcher never knows any stream_id format or concrete app name, only the
// App()/Name() -> Protocol mapping.
type Dispatcher struct {
	byApp   map[string]Protocol
	byName  map[string]Protocol
	tickets *TicketStore
	pool    *pgxpool.Pool
	// zlm is the SAME *ZLMClient each Protocol already holds by reference
	// (built once in cmd/server/main.go); handleSnapshot (snapshot.go) needs
	// it to call getSnap / force-close streams.
	zlm *ZLMClient
	// snapshotCache: latest real photo per device+channel, shared across ALL
	// sessions/tenants -- see snapshot_cache.go. Avoids repeating a physical
	// capture (waking up the device) when another session already has a
	// recent enough one.
	snapshotCache *snapshotCacheStore
	// meter is the central live-view time meter (SetLiveMeter).
	meter *LiveMeter
}

func NewDispatcher(pool *pgxpool.Pool, tickets *TicketStore, zlm *ZLMClient, protocols ...Protocol) *Dispatcher {
	d := &Dispatcher{
		byApp: map[string]Protocol{}, byName: map[string]Protocol{}, tickets: tickets, pool: pool, zlm: zlm,
		snapshotCache: newSnapshotCacheStore(),
	}
	for _, p := range protocols {
		// Never register a protocol with an empty App()/Name(). Guardrail
		// from a real bug: a deployment without GT06 configured left its app
		// name as "", and comparing that against the missing "app" of a
		// JT1078 payload (also "") matched by mistake. Here the problem is
		// prevented structurally: a Protocol without a real identifier simply
		// does not take part in dispatch. Whoever builds the Dispatcher
		// (main.go) must not pass a misconfigured Protocol; this registry
		// never silently accepts one.
		if p.App() == "" || p.Name() == "" {
			log.Printf("videobridge: protocol with App()=%q Name()=%q skipped from registry (empty identifier)", p.App(), p.Name())
			continue
		}
		d.byApp[p.App()] = p
		d.byName[p.Name()] = p
	}
	return d
}

// RegisterRoutes mounts the shared endpoints on mux. Called once from
// cmd/server/main.go together with each protocol package's own routes
// (jt1078bridge.RegisterRoutes, gt06videobridge.RegisterRoutes) on the SAME
// *http.ServeMux.
func (d *Dispatcher) RegisterRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/v1/video-tickets", d.handleMintTicket)
	mux.HandleFunc("POST /api/v1/snapshot", d.handleSnapshot)
	mux.HandleFunc("POST /api/v1/snapshot-cache", d.handleSnapshotCache)
	mux.HandleFunc("POST /api/v1/snapshot-native", d.handleSnapshotNative)
	mux.HandleFunc("POST /api/v1/live-balance", d.handleLiveBalance)
	mux.HandleFunc("POST /api/v1/on_publish", d.handlePublish)
	mux.HandleFunc("POST /api/v1/on_play", d.handlePlayAuth)
	mux.HandleFunc("POST /api/v1/on_flow_report", d.handleFlowReport)
	mux.HandleFunc("POST /api/v1/on_stream_not_found", d.handleStreamNotFound)
	mux.HandleFunc("POST /api/v1/on_stream_none_reader", d.handleStreamNoneReader)
	mux.HandleFunc("POST /api/v1/on_stream_changed", d.handleStreamChanged)
}

// --- POST /api/v1/video-tickets ---

// mintTicketBody is what the API sends to this bridge after authorizing a
// video request (POST /devices/{id}/video) to register a one-time playback
// ticket.
type mintTicketBody struct {
	TenantID   string `json:"tenantId"`
	TerminalID string `json:"terminalId"`
	Channel    uint8  `json:"channel"`
	// Protocol ("jt808"/"gt06_video"/...) selects the Protocol registered by
	// Name(). It is translated to its real App() INSIDE this package; the API
	// does not need to know each protocol's RTMP/RTP namespace.
	Protocol string `json:"protocol"`
}

func (d *Dispatcher) handleMintTicket(w http.ResponseWriter, r *http.Request) {
	var req mintTicketBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.TenantID == "" || req.TerminalID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": 400, "msg": "invalid body"})
		return
	}
	proto, ok := d.byName[req.Protocol]
	if !ok {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": 400, "msg": "unknown protocol"})
		return
	}
	token, err := d.tickets.Mint(req.TenantID, req.TerminalID, proto.App(), req.Channel)
	if err != nil {
		log.Printf("videobridge: could not mint ticket for %s: %v", req.TerminalID, err)
		writeJSON(w, http.StatusInternalServerError, map[string]any{"code": 500, "msg": "could not issue ticket"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"code": 0, "token": token})
}

// --- POST /api/v1/on_publish ---

type zlmPublishBody struct {
	App    string `json:"app"`
	Stream string `json:"stream"`
}

// handlePublish is the authorization gate for EVERY publish attempt
// (ZLMediaKit on_publish hook) -- the ONLY real defense of any public ingest
// surface a push protocol (like GT06/RTMP) exposes. An app with no registered
// Protocol is rejected outright. See Protocol.AuthorizePublish for why a
// pull protocol (JT1078) always authorizes without touching the database.
func (d *Dispatcher) handlePublish(w http.ResponseWriter, r *http.Request) {
	var req zlmPublishBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusOK, map[string]any{"code": -1, "msg": "invalid payload"})
		return
	}
	proto, ok := d.byApp[req.App]
	if !ok {
		log.Printf("videobridge: on_publish rejected (unknown app): app=%q stream=%q", req.App, req.Stream)
		writeJSON(w, http.StatusOK, map[string]any{"code": -1, "msg": "unauthorized"})
		return
	}
	if err := proto.AuthorizePublish(r.Context(), req.App, req.Stream); err != nil {
		log.Printf("videobridge: on_publish rejected (%s): app=%q stream=%q: %v", proto.Name(), req.App, req.Stream, err)
		writeJSON(w, http.StatusOK, map[string]any{"code": -1, "msg": "unauthorized"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"code": 0})
}

// --- POST /api/v1/on_stream_changed ---

// zlmStreamChangedBody holds the on_stream_changed hook fields. Regist=true
// is a stream that JUST started (no action here: each Protocol already learns
// about the real start via on_publish or its own pull); Regist=false is the
// one that matters: the device stopped publishing. This hook never gates
// anything on the ZLMediaKit side (unlike on_publish/on_play), so it always
// answers code:0.
type zlmStreamChangedBody struct {
	Regist bool   `json:"regist"`
	App    string `json:"app"`
	Stream string `json:"stream"`
}

func (d *Dispatcher) handleStreamChanged(w http.ResponseWriter, r *http.Request) {
	var req zlmStreamChangedBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusOK, map[string]any{"code": 0})
		return
	}
	if req.Regist {
		writeJSON(w, http.StatusOK, map[string]any{"code": 0})
		return
	}
	if proto, ok := d.byApp[req.App]; ok {
		proto.HandleStreamStopped(req.App, req.Stream)
	}
	writeJSON(w, http.StatusOK, map[string]any{"code": 0})
}

// --- POST /api/v1/on_stream_not_found ---

type zlmStreamNotFoundBody struct {
	App    string `json:"app"`
	Stream string `json:"stream"`
}

// handleStreamNotFound fires when an AUTHORIZED client asks to play a stream
// that does not exist yet (never before on_play authorized it). A pull
// protocol triggers its own signaling here (0x9101...); a push protocol only
// confirms whether something is already in progress -- see
// Protocol.HandleStreamNotFound.
func (d *Dispatcher) handleStreamNotFound(w http.ResponseWriter, r *http.Request) {
	var req zlmStreamNotFoundBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": 400})
		return
	}
	proto, ok := d.byApp[req.App]
	if !ok {
		log.Printf("videobridge: on_stream_not_found with unknown app: %q", req.App)
		writeJSON(w, http.StatusOK, map[string]any{"code": -1})
		return
	}
	if !proto.HandleStreamNotFound(r.Context(), req.App, req.Stream) {
		writeJSON(w, http.StatusOK, map[string]any{"code": -1})
		return
	}
	// code 0 tells ZLMediaKit it can wait for the stream to appear.
	writeJSON(w, http.StatusOK, map[string]any{"code": 0})
}

// --- POST /api/v1/on_flow_report ---

// zlmFlowReportBody holds the relevant on_flow_report payload fields
// (verified against the ZLMediaKit source -- a wrong field name here fails
// silently: json.Decode leaves the field at its zero value without error).
type zlmFlowReportBody struct {
	App        string `json:"app"`
	Stream     string `json:"stream"`
	Schema     string `json:"schema"`
	Player     bool   `json:"player"`
	TotalBytes int64  `json:"totalBytes"`
	Duration   int64  `json:"duration"`
	IP         string `json:"ip"`
}

// handleFlowReport records a usage_event for every playback session
// ZLMediaKit reports as closed. It ONLY counts Player=true: that is traffic
// served to a client, which must be recorded at EVERY byte delivery point
// (see docs/architecture.md). Player=false is ingest (a push protocol
// receiving the device's stream, or JT1078's internal pull) and is not
// billed. Never fails loudly toward ZLMediaKit: this is telemetry that gates
// nothing on the ZLM side; an error on our side is logged and code 0 is
// returned anyway.
func (d *Dispatcher) handleFlowReport(w http.ResponseWriter, r *http.Request) {
	var req zlmFlowReportBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": 400})
		return
	}
	if !req.Player {
		writeJSON(w, http.StatusOK, map[string]any{"code": 0})
		return
	}
	proto, ok := d.byApp[req.App]
	if !ok {
		log.Printf("videobridge: on_flow_report with unknown app: %q", req.App)
		writeJSON(w, http.StatusOK, map[string]any{"code": 0})
		return
	}
	deviceKey, channel, ok := proto.ParseStream(req.App, req.Stream)
	if !ok {
		log.Printf("videobridge: on_flow_report (%s) with unparseable stream_id: %q", proto.Name(), req.Stream)
		writeJSON(w, http.StatusOK, map[string]any{"code": 0})
		return
	}
	metadata := map[string]any{
		"schema":     req.Schema,
		"duration_s": req.Duration,
		"ip":         req.IP,
		"channel":    channel,
	}
	err := db.WithBypass(r.Context(), d.pool, func(ctx context.Context, tx pgx.Tx) error {
		dev, err := proto.LookupDevice(ctx, tx, deviceKey)
		if err != nil {
			return err
		}
		return db.InsertUsageEvent(ctx, tx, dev.TenantID, dev.ID, "live_view", req.TotalBytes, metadata)
	})
	if err != nil {
		log.Printf("videobridge: on_flow_report could not record usage_event for stream %q: %v", req.Stream, err)
	}
	writeJSON(w, http.StatusOK, map[string]any{"code": 0})
}

// --- POST /api/v1/on_play ---

// zlmPlayBody holds the on_play payload fields this handler actually uses.
// The full payload carries more (mediaServerId, vhost, ip, port,
// hook_index...) -- deliberately ignored, ZLMediaKit adds fields between
// versions and a new field must not break authorization.
type zlmPlayBody struct {
	App    string `json:"app"`
	Stream string `json:"stream"`
	Schema string `json:"schema"`
	// Params is the RAW (undecoded) query string of the URL the client
	// requested; the ticket is extracted from here.
	Params string `json:"params"`
}

// handlePlayAuth is the authorization gate for EVERY playback start (on_play
// hook). The API issued a one-time ticket and registered it with this bridge
// (handleMintTicket). The browser requested the playback URL with the ticket
// in the query string; ZLMediaKit POSTs the stream and raw query string here
// BEFORE serving a single byte. The ticket is consumed (atomic pop, dies on
// first use) and the requested stream must match exactly the ticket's
// device/channel. This runs in every playback's hot path, so it does NOT
// touch the database: the stream<->tenant mapping was resolved by the API
// when the ticket was issued.
func (d *Dispatcher) handlePlayAuth(w http.ResponseWriter, r *http.Request) {
	var req zlmPlayBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusOK, map[string]any{"code": -1, "msg": "invalid payload"})
		return
	}

	token := ""
	if req.Params != "" {
		if q, err := url.ParseQuery(req.Params); err == nil {
			token = q.Get("token")
		}
	}

	ticket := d.tickets.Consume(token)
	if ticket == nil {
		log.Printf("videobridge: on_play rejected (missing/invalid/expired token) for stream=%q", req.Stream)
		writeJSON(w, http.StatusOK, map[string]any{"code": -1, "msg": "unauthorized"})
		return
	}

	// The ticket must match the SAME app it was minted for, not just the
	// same deviceKey. Security finding (F6): two protocols can have
	// identifier columns with INDEPENDENT unique constraints (nothing in the
	// schema prevents a JT808 terminalID from numerically matching another
	// tenant's GT06 IMEI), so without this check a ticket minted for one
	// protocol authorized playing another's stream when the identifiers
	// collided as digits.
	if req.App != ticket.App {
		log.Printf("videobridge: on_play rejected (ticket app mismatch) ticket_app=%q stream_app=%q", ticket.App, req.App)
		writeJSON(w, http.StatusOK, map[string]any{"code": -1, "msg": "unauthorized"})
		return
	}

	proto, ok := d.byApp[req.App]
	if !ok {
		log.Printf("videobridge: on_play with unknown app despite valid ticket: %q", req.App)
		writeJSON(w, http.StatusOK, map[string]any{"code": -1, "msg": "unauthorized"})
		return
	}
	deviceKey, channel, ok := proto.ParseStream(req.App, req.Stream)
	matches := ok && deviceKey == ticket.TerminalID && channel == ticket.Channel
	if !matches {
		log.Printf("videobridge: on_play rejected (ticket does not match stream) ticket=%s/%d app=%q stream=%q", ticket.TerminalID, ticket.Channel, req.App, req.Stream)
		writeJSON(w, http.StatusOK, map[string]any{"code": -1, "msg": "unauthorized"})
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{"code": 0})
}

// --- POST /api/v1/on_stream_none_reader ---

type zlmStreamNoneReaderBody struct {
	Stream string `json:"stream"`
	Schema string `json:"schema"`
	App    string `json:"app"`
}

// handleStreamNoneReader shuts down a stream nobody is watching anymore
// (on_stream_none_reader hook, fires after general.streamNoneReaderDelayMS
// with zero viewers). Answering {"code":0,"close":true} makes ZLMediaKit
// really close the stream -- real data/bandwidth savings in a per-camera
// billed product. Only a stream the owning Protocol recognizes as active is
// closed (HandleIdleStream); an unknown stream_id is left alone.
func (d *Dispatcher) handleStreamNoneReader(w http.ResponseWriter, r *http.Request) {
	var req zlmStreamNoneReaderBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusOK, map[string]any{"code": 0, "close": false})
		return
	}
	proto, ok := d.byApp[req.App]
	if !ok {
		writeJSON(w, http.StatusOK, map[string]any{"code": 0, "close": false})
		return
	}
	deviceKey, channel, ok := proto.ParseStream(req.App, req.Stream)
	if !ok {
		writeJSON(w, http.StatusOK, map[string]any{"code": 0, "close": false})
		return
	}
	if !proto.HandleIdleStream(deviceKey, channel) {
		writeJSON(w, http.StatusOK, map[string]any{"code": 0, "close": false})
		return
	}
	log.Printf("videobridge: stream %s/%s has no viewers, closing (saves device data)", req.App, req.Stream)
	writeJSON(w, http.StatusOK, map[string]any{"code": 0, "close": true})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
