// Channel for CONFIGURATION commands (SERVER/APN/TIMEZONE/UPLOAD/etc.).
// Deliberately separate from Handler/Sender in commands.go: those are
// protocol-agnostic with a FIXED vocabulary (command_type), while these are
// raw, GT06-specific text built by the API (api/app/gt06_config_commands.py)
// from validated parameters. Arbitrary text is never accepted without going
// through that API-side builder -- this handler only transports what was
// already built; it does not decide what is sent.
package commands

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"time"
)

// RawSender is what a protocol package with raw-text command support must
// implement -- currently only gt06server.Dispatcher (SendRawCommand).
type RawSender interface {
	SendRawCommand(ctx context.Context, deviceKey, text string, timeout time.Duration) (reply string, err error)
}

type rawRequestBody struct {
	Protocol  string `json:"protocol"`
	DeviceKey string `json:"deviceKey"`
	Text      string `json:"text"`
}

// maxRawTextLen matches the device_config_commands.raw_text column
// (migration 0041) -- defense in depth; real validation already happened in
// the API before calling here.
const maxRawTextLen = 300

// RawHandler serves POST /api/v1/gt06-raw-command. Same network trust model
// as Handler (internal, port 8082, never exposed to the internet). senders
// is resolved by "protocol", like Handler.
func RawHandler(senders map[string]RawSender) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req rawRequestBody
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil ||
			req.Protocol == "" || req.DeviceKey == "" || req.Text == "" || len(req.Text) > maxRawTextLen {
			writeJSON(w, http.StatusBadRequest, responseBody{Code: 400, Msg: "invalid body, protocol/deviceKey/text are required"})
			return
		}

		sender, ok := senders[req.Protocol]
		if !ok {
			writeJSON(w, http.StatusBadRequest, responseBody{Code: 400, Msg: "protocol does not support configuration commands"})
			return
		}

		ctx, cancel := context.WithTimeout(r.Context(), commandTimeout+2*time.Second)
		defer cancel()

		reply, err := sender.SendRawCommand(ctx, req.DeviceKey, req.Text, commandTimeout)
		if err != nil {
			status := http.StatusInternalServerError
			switch {
			case errors.Is(err, ErrDeviceNotConnected):
				status = http.StatusNotFound
			case errors.Is(err, ErrCommandTimeout):
				status = http.StatusGatewayTimeout
			case errors.Is(err, ErrCommandBusy):
				status = http.StatusConflict
			}
			log.Printf("commands: %s/%s configuration command %q failed: %v", req.Protocol, req.DeviceKey, req.Text, err)
			writeJSON(w, status, responseBody{Code: status, Msg: err.Error()})
			return
		}
		if len(reply) > maxReplyLen {
			reply = reply[:maxReplyLen]
		}
		writeJSON(w, http.StatusOK, responseBody{Code: 0, Msg: "success", Reply: reply})
	}
}
