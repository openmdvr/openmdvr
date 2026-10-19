package gt06server

import (
	"context"
	"time"

	"github.com/google/uuid"
)

// ClipRequester is implemented by alarmclip.Bridge (injected via
// Server.SetClipRequester in cmd/server/main.go) to auto-request the video
// clip of a camera event. It is a local interface rather than an import of
// alarmclip.Bridge so this generic GT06 package stays decoupled from that
// feature. Optional (nil-safe): without it, handleVideoEventReport still
// stores the alarm, it just never requests a clip.
type ClipRequester interface {
	RequestClipForAlarm(ctx context.Context, tenantID, deviceID, alarmID uuid.UUID, imei string, alarmTime time.Time, frontFileName, cabinFileName string)
}
