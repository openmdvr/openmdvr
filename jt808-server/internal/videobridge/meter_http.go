package videobridge

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"time"

	"github.com/google/uuid"
)

// SetLiveMeter wires the central live-view time meter.
func (d *Dispatcher) SetLiveMeter(m *LiveMeter) {
	d.meter = m
}

type liveBalanceBody struct {
	TenantID string `json:"tenantId"`
}

// handleLiveBalance returns the tenant's REAL live-view balance (quota -
// consumed in the DB - not-yet-written time of open sessions) and how many
// cameras are open now, so the dashboard counts down at the right rate (two
// cameras = twice as fast) even when another user of the same tenant is the
// one watching. The API already verified the caller can see that tenant.
func (d *Dispatcher) handleLiveBalance(w http.ResponseWriter, r *http.Request) {
	var req liveBalanceBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": 400, "msg": "invalid body"})
		return
	}
	tenantID, err := uuid.Parse(req.TenantID)
	if err != nil || d.meter == nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": 400, "msg": "invalid body"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	remaining, active, err := d.meter.Balance(ctx, tenantID)
	if err != nil {
		log.Printf("videobridge: live-view balance for tenant %s: %v", tenantID, err)
		writeJSON(w, http.StatusOK, map[string]any{"code": 500, "msg": "could not read balance"})
		return
	}
	if remaining < 0 {
		remaining = 0
	}
	writeJSON(w, http.StatusOK, map[string]any{"code": 0, "remainingSeconds": remaining, "activeSessions": active})
}
