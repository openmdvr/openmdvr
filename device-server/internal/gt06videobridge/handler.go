package gt06videobridge

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
)

// RegisterRoutes mounts the only endpoint fully owned by this protocol:
// request live video for a specific gt06_video device. The shared endpoints
// (tickets, ZLMediaKit hooks) live in videobridge.Dispatcher, mounted
// separately.
func (b *Bridge) RegisterRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/v1/gt06-video", b.handleRequestVideo)
}

type requestVideoBody struct {
	IMEI    string `json:"imei"`
	Channel uint8  `json:"channel"`
	// Purpose "snapshot": start only for the preview photo (see
	// RequestVideoFor). Any other value (or absent) = live video.
	Purpose string `json:"purpose"`
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

// handleRequestVideo is the gt06_video equivalent of POST /api/v1/9101
// (JT1078), called by the internal API after authorizing the video (POST
// /devices/{id}/video). The API's video.py translates these HTTP statuses
// (404/402) into its own business messages, so changing the contract here
// would break that translation.
func (b *Bridge) handleRequestVideo(w http.ResponseWriter, r *http.Request) {
	var req requestVideoBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.IMEI == "" {
		writeJSON(w, http.StatusBadRequest, requestVideoResponse{Code: 400, Msg: "invalid body, imei is required"})
		return
	}

	playURL, webrtcURL, maxSeconds, quotaRemaining, err := b.RequestVideoFor(r.Context(), req.IMEI, req.Channel, req.Purpose == "snapshot")
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
		log.Printf("gt06videobridge: gt06-video for %s failed: %v", req.IMEI, err)
		writeJSON(w, status, requestVideoResponse{Code: status, Msg: err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, requestVideoResponse{
		Code:                     0,
		Msg:                      "success",
		URL:                      playURL,
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
