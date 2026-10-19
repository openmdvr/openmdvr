package jt1078bridge

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"strconv"
	"strings"
)

// jt1078InternalRTPApp is the "app" ZLMediaKit assigns by default to streams
// arriving through its rtp_proxy (openRtpServer/closeRtpServer, see zlm.go in
// videobridge) -- confirmed empirically (not a documented configurable value
// in config.ini): ZLM_PLAY_URL_FORMAT already assumes it
// ("http://.../rtp/%s.live.flv") and the on_publish log for a test RTP push
// shows app="rtp". It is this protocol's App() (see protocol.go), distinct
// from any other video protocol's app.
const jt1078InternalRTPApp = "rtp"

// parseStreamID reverses streamID() ("<terminalID>_<channel>"). terminalID
// itself never contains "_" (digits only, BCD), so the last "_"-separated
// segment is always the channel.
func parseStreamID(id string) (terminalID string, channel uint8, ok bool) {
	idx := strings.LastIndexByte(id, '_')
	if idx < 0 || idx == len(id)-1 {
		return "", 0, false
	}
	terminalID = id[:idx]
	ch, err := strconv.Atoi(id[idx+1:])
	if err != nil || ch < 0 || ch > 255 {
		return "", 0, false
	}
	return terminalID, uint8(ch), true
}

// RegisterRoutes mounts the only endpoint fully owned by this protocol:
// request live video for a specific jt808 device on demand (used by the API
// after checking permissions). The shared endpoints (tickets, ZLMediaKit
// hooks) live in videobridge.Dispatcher, mounted separately from
// cmd/server/main.go.
func (b *Bridge) RegisterRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/v1/9101", b.handleRequestVideo)
}

type requestVideoBody struct {
	TerminalID string `json:"terminalId"`
	Channel    uint8  `json:"channel"`
}

type requestVideoResponse struct {
	Code int    `json:"code"`
	Msg  string `json:"msg"`
	// URL: legacy HTTP-FLV/mpegts.js playback URL, kept until the WebRTC
	// path fully replaces it.
	URL                      string `json:"url,omitempty"`
	WebrtcURL                string `json:"webrtcUrl,omitempty"`
	ExpiresInSeconds         int    `json:"expiresInSeconds,omitempty"`
	LiveViewSecondsRemaining int    `json:"liveViewSecondsRemaining,omitempty"`
}

func (b *Bridge) handleRequestVideo(w http.ResponseWriter, r *http.Request) {
	var req requestVideoBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.TerminalID == "" {
		writeJSON(w, http.StatusBadRequest, requestVideoResponse{Code: 400, Msg: "invalid body, terminalId is required"})
		return
	}

	url, webrtcURL, maxSeconds, quotaRemaining, err := b.RequestVideo(r.Context(), req.TerminalID, req.Channel)
	if err != nil {
		status := http.StatusInternalServerError
		var notConnected ErrDeviceNotConnected
		var quotaExhausted ErrLiveViewQuotaExhausted
		switch {
		case errors.As(err, &notConnected):
			status = http.StatusNotFound
		case errors.As(err, &quotaExhausted):
			status = http.StatusPaymentRequired
		}
		log.Printf("jt1078bridge: 9101 for %s channel %d failed: %v", req.TerminalID, req.Channel, err)
		writeJSON(w, status, requestVideoResponse{Code: status, Msg: err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, requestVideoResponse{
		Code:                     0,
		Msg:                      "success",
		URL:                      url,
		WebrtcURL:                webrtcURL,
		ExpiresInSeconds:         maxSeconds,
		LiveViewSecondsRemaining: quotaRemaining,
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
