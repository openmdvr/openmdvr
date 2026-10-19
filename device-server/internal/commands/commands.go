// Package commands is the protocol-AGNOSTIC remote command layer. It sits
// between the FastAPI API (which only knows "protocol X, send command Y to
// this device") and each protocol package (gt06server today, others later),
// which is the only one that knows how to translate a generic command_type
// into its real wire format. This follows the common "generic command model
// + per-protocol encoder" design used by open-source tracking servers.
// Adding a protocol with commands means implementing Sender in its package
// and adding an entry to the senders map in cmd/server/main.go -- without
// touching this file, the API, the database or the frontend.
package commands

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"time"
)

// Sender is all a protocol package must implement to take part in the
// remote command channel.
type Sender interface {
	// SendCommand sends commandType (protocol-agnostic vocabulary, see
	// device_commands.command_type / api/app/schemas.py) to the device
	// identified by deviceKey -- the identifier THAT protocol uses to find
	// its active connection (the IMEI for GT06) -- and returns the device's
	// real reply (text, for auditing) or an error wrapping (%w) one of the
	// sentinels below.
	SendCommand(ctx context.Context, deviceKey, commandType string, timeout time.Duration) (reply string, err error)
}

// Error sentinels every Sender must wrap (fmt.Errorf with %w) so Handler can
// map them to the right HTTP status without coupling to each protocol's
// concrete error types.
var (
	ErrDeviceNotConnected = errors.New("device not connected")
	ErrCommandBusy        = errors.New("the device already has a pending command")
	ErrCommandTimeout     = errors.New("the device did not respond in time")
)

// commandTimeout is deliberately generous: a real GT06 may take a while to
// reply when it first checks its own speed/GPS-fix guardrail (section 6.4 of
// the Concox protocol document) before responding.
const commandTimeout = 15 * time.Second

// maxReplyLen mirrors the device_reply CHECK in migration 0029. The reply
// text comes from the DEVICE (untrusted input); it is never executed or
// interpreted, but it is truncated before leaving this process anyway.
const maxReplyLen = 500

type requestBody struct {
	Protocol    string `json:"protocol"`
	DeviceKey   string `json:"deviceKey"`
	CommandType string `json:"commandType"`
}

type responseBody struct {
	Code  int    `json:"code"`
	Msg   string `json:"msg"`
	Reply string `json:"reply,omitempty"`
}

// Handler serves POST /api/v1/commands, called only by the FastAPI API
// (trusted internal docker compose network, same authentication model -- or
// lack of one -- as this process's other internal endpoints, see
// jt1078bridge/handler.go). senders picks the implementation by the
// request's "protocol" field.
//
// Security review note (informational, no code fix): this endpoint writes NO
// audit row -- that is the sole responsibility of device_commands.py
// (api/app/routers), its only real caller. If something inside the docker
// network called this endpoint directly (internal port, bound to 127.0.0.1,
// never exposed to the internet), the command would still run but WITHOUT
// being recorded in device_commands (who asked, when, what the device
// replied). This is a traceability gap, not an access vulnerability (the
// network surface is unchanged); auditing belongs to the layer that already
// knows tenant_id/user, which this package knows neither of.
func Handler(senders map[string]Sender) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req requestBody
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Protocol == "" || req.DeviceKey == "" || req.CommandType == "" {
			writeJSON(w, http.StatusBadRequest, responseBody{Code: 400, Msg: "invalid body, protocol/deviceKey/commandType are required"})
			return
		}

		sender, ok := senders[req.Protocol]
		if !ok {
			writeJSON(w, http.StatusBadRequest, responseBody{Code: 400, Msg: "protocol does not support commands"})
			return
		}

		ctx, cancel := context.WithTimeout(r.Context(), commandTimeout+2*time.Second)
		defer cancel()

		reply, err := sender.SendCommand(ctx, req.DeviceKey, req.CommandType, commandTimeout)
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
			log.Printf("commands: %s/%s command %q failed: %v", req.Protocol, req.DeviceKey, req.CommandType, err)
			writeJSON(w, status, responseBody{Code: status, Msg: err.Error()})
			return
		}
		if len(reply) > maxReplyLen {
			reply = reply[:maxReplyLen]
		}
		writeJSON(w, http.StatusOK, responseBody{Code: 0, Msg: "success", Reply: reply})
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
