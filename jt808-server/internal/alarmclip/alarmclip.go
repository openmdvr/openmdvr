// Package alarmclip implements retrieval of alarm-linked video clips for
// GT06/RTMP devices (Jimi IoT JC261/JC400). The file TRANSPORT mechanism (the
// device uploads over HTTP to one of our URLs, configured with the
// UPLOAD/FILELIST commands) is confirmed against real hardware. Two paths
// trigger a request: manual (handleRequestClip, "Request clip"/"Retry" in the
// UI) and automatic (RequestClipForAlarm, fired by gt06server as soon as it
// sees a 0x95 camera event). The package also receives native photos
// (photo.go) on the same upload endpoint. It keeps deliberate diagnostic
// logging because parts of the vendor command set are still being confirmed
// against real hardware.
package alarmclip

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"regexp"
	"strconv"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
	"github.com/openmdvr/openmdvr/jt808-server/internal/storage"
)

// gt06Sender is the subset of gt06server.Dispatcher this package needs -- a
// local interface (not commands.Sender) because these methods are not part
// of that generic contract (see gt06server/commands.go).
//
// Clip requests use SendRawCommandFireAndForget, NOT SendRawCommand:
// confirmed on real hardware (with 15s and then 30s waits), the device NEVER
// answers clip retrieval commands on the synchronous command reply channel;
// per vendor protocol documentation, the real confirmation comes through a
// separate alarm sub-protocol (0x69, not implemented), never the command
// reply RTMP/SERVER/DYD/TIMER use. Blocking the HTTP API waiting for
// something that never arrives caused spurious "did not respond in time"
// errors. See SendRawCommandFireAndForget in gt06server/commands.go: it still
// registers sess.pending to keep the F1 protection; any reply is awaited in
// the background and logged as "replied (late, out of band)".
type gt06Sender interface {
	SendCommand(ctx context.Context, imei, commandType string, timeout time.Duration) (string, error)
	SendRawCommandFireAndForget(imei, text string) error
	// SendRawCommand -- see gt06server.Dispatcher.SendRawCommand (waits for
	// the device's text reply; used by native photos, see photo.go).
	SendRawCommand(ctx context.Context, imei, text string, timeout time.Duration) (string, error)
	// HasActiveSession -- see gt06server.Dispatcher.HasActiveSession. The
	// real barrier against a security review finding: handleUpload requires
	// it BEFORE touching storage.
	HasActiveSession(imei string) bool
}

// Bridge orchestrates clip retrieval: it sends the GT06 request command and,
// separately, receives the file the device uploads afterwards (there is no
// synchronous reply carrying the bytes -- handleRequestClip only STARTS the
// request; the file arrives seconds/minutes later via handleUpload,
// correlated by IMEI and file name).
type Bridge struct {
	pool    *pgxpool.Pool
	gt06    gt06Sender
	storage *storage.Client
	photos  *photoRequests
	// photoSink receives photos that arrive late (see photoLateWindow); in
	// main.go it is the video dispatcher's preview cache.
	photoSink func(imei string, channel uint8, data []byte)
}

// SetPhotoSink wires where late native photos go.
func (b *Bridge) SetPhotoSink(sink func(imei string, channel uint8, data []byte)) {
	b.photoSink = sink
}

func New(pool *pgxpool.Pool, gt06 gt06Sender, storageClient *storage.Client) *Bridge {
	return &Bridge{pool: pool, gt06: gt06, storage: storageClient, photos: newPhotoRequests()}
}

// RegisterRoutes mounts the PUBLIC routes the device reaches directly from
// its cellular connection (configured on the device with the UPLOAD/FILELIST
// commands). They are mounted on a dedicated *http.ServeMux, on a public port
// distinct from all others (cfg.AlarmClipListenAddr), never on the internal
// port 8082.
func (b *Bridge) RegisterRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /upload/{imei}", b.handleUpload)
	mux.HandleFunc("POST /filelist/{imei}", b.handleFilelist)
}

// RegisterInternalRoutes mounts the internal route the FastAPI API calls to
// trigger a clip request -- the same internal port/mux 8082 used by
// /api/v1/commands, /api/v1/9101, etc.
func (b *Bridge) RegisterInternalRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/v1/gt06-alarm-clip", b.handleRequestClip)
}

type requestClipBody struct {
	ClipID    string `json:"clipId"`
	IMEI      string `json:"imei"`
	AlarmTime string `json:"alarmTime"` // RFC3339, already resolved by the API
}

// EVIDEO is the command that extracts an event clip from the SD card (HVIDEO
// reads a lower-quality sub-stream buffer that may no longer hold the clip).
// A first attempt with HVIDEO-style parameters (underscore timestamp +
// channel 0) got a real, specific device error -- `"EVIDEO,parameter A
// error. "` -- confirming the command name is right but the parameter format
// was not.
//
// Per the vendor command reference:
//
//	EVIDEO,{timestamp},{cameraType},{lengthSecond}
//	timestamp: "Year-Month-Day Hour:Minute:Second" (dashes + space, NOT
//	    underscores -- "parameter A" = the first parameter = the timestamp).
//	cameraType: 1=Front, 2=Cabin (inward) -- 1-indexed, NEVER 0.
//	lengthSecond: 10-60; 30 is used (the reference example value,
//	    "EVIDEO,2020-06-15 12:12:12,1,30").
//
// HVIDEO uses the underscore timestamp format but is ALSO 1-indexed, so its
// silent failure may also be explained by the invalid channel 0.
const clipVideoDateFormat = "2006-01-02 15:04:05"

// clipVideoCameraFront/Inward: 1/2, see above -- NEVER 0.
const (
	clipVideoCameraFront  = 1
	clipVideoCameraInward = 2
)

// clipVideoLengthSeconds: see above -- the reference example's exact value
// rather than an invented number in the documented range (10-60).
const clipVideoLengthSeconds = 30

const clipVideoCommand = "EVIDEO"

// clipUploadFileCommand -- UPLOADFILE,<exact name>#. Per the vendor command
// reference this is an alternative to EVIDEO: instead of rebuilding a
// timestamp, it requests the file by the EXACT name the device already
// reported. The automatic path (RequestClipForAlarm) already has that real
// name (from the 0x95 report -- zero timestamp/channel ambiguity), so it uses
// UPLOADFILE whenever it can, with EVIDEO as the fallback. The manual path
// (handleRequestClip, called from the Python API, which only knows
// alarm_time, not the file name) uses EVIDEO.
const clipUploadFileCommand = "UPLOADFILE"

// maxClipUploadBytes -- see handleUpload. 100MB is generous for the fixed
// 1-minute low-resolution dashcam segment this hardware uploads, without
// exposing process memory to an unbounded body -- storage.Upload buffers the
// whole body in memory before uploading (see its doc comment), so this limit
// is real, not cosmetic.
const maxClipUploadBytes = 100 * 1024 * 1024

// handleRequestClip STARTS a manual request ("Request clip"/"Retry" in the
// UI). The audit row already exists (created by api/, see
// alarms.py::request_alarm_clip); this only sends the command. It coexists
// with the automatic path (RequestClipForAlarm); both share sendClipCommand.
func (b *Bridge) handleRequestClip(w http.ResponseWriter, r *http.Request) {
	var req requestClipBody
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ClipID == "" || req.IMEI == "" || req.AlarmTime == "" {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": 400, "msg": "invalid body"})
		return
	}
	alarmTime, err := time.Parse(time.RFC3339, req.AlarmTime)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"code": 400, "msg": "invalid alarmTime"})
		return
	}

	// Empty fileName -- the Python API (the only caller of this endpoint)
	// only knows alarm_time, never the device's real file name.
	if err := b.sendClipCommand(req.ClipID, req.IMEI, alarmTime, ""); err != nil {
		writeJSON(w, http.StatusOK, map[string]any{"code": 500, "msg": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"code": 0, "msg": "request sent, waiting for the device to upload the file"})
}

// sendClipCommand sends UPLOADFILE (when fileName is set -- the automatic
// path, see RequestClipForAlarm) or EVIDEO as the fallback, and updates the
// audit status. Shared by the manual (handleRequestClip) and automatic paths.
func (b *Bridge) sendClipCommand(clipID, imei string, alarmTime time.Time, fileName string) error {
	var text string
	if fileName != "" {
		text = fmt.Sprintf("%s,%s#", clipUploadFileCommand, fileName)
	} else {
		text = fmt.Sprintf("%s,%s,%d,%d#", clipVideoCommand, alarmTime.UTC().Format(clipVideoDateFormat), clipVideoCameraFront, clipVideoLengthSeconds)
	}

	// SendRawCommandFireAndForget does not wait for a reply -- see gt06Sender
	// above. The only possible error here is synchronous (device not
	// connected, or a previous command still occupying the connection) --
	// never "did not respond in time", since no reply is awaited.
	if err := b.gt06.SendRawCommandFireAndForget(imei, text); err != nil {
		log.Printf("alarmclip: %s to %s failed: %v", text, imei, err)
		// context.Background(), NEVER the caller's context: if sending
		// failed precisely BECAUSE the caller's context was cancelled (e.g.
		// the Python API disconnecting on its own timeout), reusing that
		// cancelled context made the "failed" write itself fail with
		// "context canceled", leaving the row orphaned in 'requested'
		// forever. Audit/status writes must never depend on the original
		// caller's context still being alive. The same applies to the
		// automatic path (which has no HTTP request context anyway).
		b.markFailed(context.Background(), clipID, "failed", err.Error())
		return err
	}

	log.Printf("alarmclip: %s sent to %s, waiting for the upload (fire-and-forget, may not confirm immediately)", text, imei)
	b.markUploading(context.Background(), clipID)
	return nil
}

// RequestClipForAlarm is the AUTOMATIC clip request, called directly from
// gt06server (see gt06server.ClipRequester) as soon as a camera event (0x95)
// is detected, without going through the internal HTTP API or Python -- this
// minimizes latency between the real event and the clip request, the same
// approach production integrators of this hardware use. requested_by stays
// NULL (migration 0043): an automatic request has no real user behind it,
// unlike the manual path ("Request clip"/"Retry" in the UI), which remains
// as the fallback if this fails.
//
// It never blocks the caller (gt06server, in the middle of processing a
// network frame): it inserts the audit row and sends the command in its own
// goroutine, like SendRawCommandFireAndForget.
//
// frontFileName/cabinFileName are the EXACT names the device itself reported
// in the 0x95 event for each camera (e.g. "EVENT_490154203237518_..._F_23.ts"
// /"..._I_24.ts") -- gt06server already has them, so UPLOADFILE is used
// instead of EVIDEO (see clipUploadFileCommand) whenever they are present.
// Empty frontFileName = fall back to EVIDEO (same as the manual path). Empty
// cabinFileName = this event only carried one camera -- a second one is
// never requested needlessly (migration 0044). They are STORED in the row
// (front_file_name/cabin_file_name) so handleUpload can correlate the real
// upload by exact name instead of "the most recent pending request". The
// cabin camera is requested LATER, chained from handleUpload once the front
// file has arrived (never in parallel -- see there for why).
func (b *Bridge) RequestClipForAlarm(ctx context.Context, tenantID, deviceID, alarmID uuid.UUID, imei string, alarmTime time.Time, frontFileName, cabinFileName string) {
	go func() {
		var clipID string
		err := db.WithBypass(context.Background(), b.pool, func(ctx context.Context, tx pgx.Tx) error {
			return tx.QueryRow(ctx,
				`INSERT INTO alarm_video_clips (tenant_id, alarm_id, alarm_time, device_id, protocol, requested_by, front_file_name, cabin_file_name)
				 VALUES ($1, $2, $3, $4, 'gt06_video', NULL, $5, $6) RETURNING id`,
				tenantID, alarmID, alarmTime, deviceID, nullIfEmpty(frontFileName), nullIfEmpty(cabinFileName),
			).Scan(&clipID)
		})
		if err != nil {
			log.Printf("alarmclip: automatic clip request for alarm %s failed creating the audit row: %v", alarmID, err)
			return
		}
		_ = b.sendClipCommand(clipID, imei, alarmTime, frontFileName)
	}()
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// eventFilenameTimePattern extracts the date/time embedded in the real name
// the device reports for EACH file it records (format
// "EVENT_<imei>_<code>_YYYY_MM_DD_HH_MM_SS_<channel F|I>_<seq>.ts"). It is the
// timestamp of the REAL event recorded on the device, independent of when it
// is uploaded -- the key to detecting misattributed uploads (see
// maxClipTimestampDrift).
var eventFilenameTimePattern = regexp.MustCompile(
	`_(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_[IF]_\d+\.ts$`,
)

// parseEventFilenameTime tries to extract the date/time embedded in a real
// device-reported file name -- false if it does not match the known pattern
// (e.g. a raw body without filename metadata, or another firmware with a
// different convention; the format is verified, never assumed).
func parseEventFilenameTime(filename string) (time.Time, bool) {
	m := eventFilenameTimePattern.FindStringSubmatch(filename)
	if m == nil {
		return time.Time{}, false
	}
	parts := make([]int, 6)
	for i, s := range m[1:] {
		v, err := strconv.Atoi(s)
		if err != nil {
			return time.Time{}, false
		}
		parts[i] = v
	}
	year, month, day, hour, minute, second := parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
	if month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59 || second > 59 {
		return time.Time{}, false
	}
	return time.Date(year, time.Month(month), day, hour, minute, second, 0, time.UTC), true
}

// maxClipTimestampDrift: the JC261 keeps its own internal queue with OLD
// unsent files (observed retrying a day-old event for HOURS), unrelated to
// our requests. When a NEW clip was requested via EVIDEO (the manual path --
// it never knows the exact name in advance, unlike UPLOADFILE) and that old
// file arrived while the new request was pending, the fallback correlation
// ("the device's most recent pending request", without an exact name)
// accepted it anyway -- silently attaching VIDEO FROM ANOTHER DAY to a real
// alarm from TODAY. Not a cross-tenant leak (same device/tenant), but
// misattributed evidence -- unacceptable when the clip is the proof of a real
// incident. 10 minutes is generous against the fixed 1-minute segment this
// hardware records, without being so tight that a slightly unsynchronized
// device clock rejects legitimate uploads.
const maxClipTimestampDrift = 10 * time.Minute

// handleUpload receives the real file the device uploads (configured with
// UPLOAD,http://<host>:<port>/upload/<IMEI>#). Correlation is by EXACT file
// name (migration 0044): the device sends its own "filename" metadata field
// (see extractUploadedFile), and each request stores in advance which name it
// expects per camera (front_file_name/cabin_file_name, see
// RequestClipForAlarm). This distinguishes concurrent requests from the same
// device and resolves which CAMERA each upload belongs to unambiguously.
// Fallback: if the device sends no "filename" (multipart without that
// metadata, or a raw non-multipart body), it falls back to the device's most
// recent pending request -- an upload is never lost just because exact
// correlation is missing.
func (b *Bridge) handleUpload(w http.ResponseWriter, r *http.Request) {
	imei := r.PathValue("imei")
	if imei == "" {
		http.Error(w, "imei required", http.StatusBadRequest)
		return
	}

	// The real barrier against a CRITICAL security review finding: this
	// endpoint is public and only knows the IMEI in the path, with no other
	// credential -- before this check, anyone on the internet who knew a
	// device's IMEI could overwrite/destroy THAT tenant's stored video clip.
	// Requiring a live authenticated GT06 session for the IMEI raises the
	// attack to "forge the device's full GT06 login". Rejected BEFORE reading
	// a single body byte -- the multipart is not even parsed.
	if !b.gt06.HasActiveSession(imei) {
		log.Printf("alarmclip: upload from %s rejected -- no live authenticated GT06 session for this IMEI", imei)
		http.Error(w, "no active session for this device", http.StatusUnauthorized)
		return
	}

	// http.MaxBytesReader, NEVER an unbounded r.Body -- this endpoint is
	// public (port 8083, reachable from the internet), the same network
	// hardening applied to jt808server/gt06server/jt1078bridge.
	// maxClipUploadBytes is generous for a 1-minute low-resolution dashcam
	// segment without exposing process memory to an unbounded body.
	r.Body = http.MaxBytesReader(w, r.Body, maxClipUploadBytes)

	fileReader, deviceFileName, err := extractUploadedFile(r, imei)
	if err != nil {
		log.Printf("alarmclip: reading file uploaded by %s: %v", imei, err)
		http.Error(w, "invalid body", http.StatusBadRequest)
		return
	}

	// Native photo (Picture,out#/in#, see photo.go): arrives on this same
	// endpoint. It is recognized BEFORE the clip logic and never touches it.
	buffered := bufio.NewReaderSize(fileReader, 512)
	head, _ := buffered.Peek(8)
	fileReader = buffered
	if looksLikePhoto(deviceFileName, head) {
		data, err := io.ReadAll(io.LimitReader(buffered, maxPhotoBytes+1))
		if err != nil || len(data) > maxPhotoBytes {
			log.Printf("alarmclip: photo from %s unreadable or too large (%d bytes): %v", imei, len(data), err)
			http.Error(w, "invalid photo", http.StatusBadRequest)
			return
		}
		if b.photos != nil && b.photos.deliver(imei, deviceFileName, data) {
			log.Printf("alarmclip: photo uploaded by %s (file %q, %d bytes) delivered to the open request", imei, deviceFileName, len(data))
		} else if ch, late := b.lateTarget(imei, deviceFileName); late && b.photoSink != nil {
			// Late reply to a recent "Picture": used for the preview (next
			// time the tile asks, it comes from the cache instantly, without
			// bothering the camera again).
			b.photoSink(imei, ch, data)
			log.Printf("alarmclip: photo from %s channel %d arrived late (file %q, %d bytes), stored for the preview", imei, ch, deviceFileName, len(data))
		} else {
			// No open request: the device sent it on its own (or too late).
			// Not stored -- nobody asked for it and there is nowhere to show it.
			log.Printf("alarmclip: photo uploaded by %s (file %q, %d bytes) with no open request, discarded", imei, deviceFileName, len(data))
		}
		w.WriteHeader(http.StatusOK)
		return
	}

	var dev db.Device
	var clipID, cabinFileName, tenantID string
	var isSecondary bool
	var alarmTime time.Time
	err = db.WithBypass(r.Context(), b.pool, func(ctx context.Context, tx pgx.Tx) error {
		d, err := db.LookupDeviceByIMEI(ctx, tx, imei)
		if err != nil {
			return err
		}
		dev = d
		if deviceFileName != "" {
			// Security review finding: this query used to resolve the clip by
			// EXACT FILE NAME REGARDLESS of status -- an upload matching the
			// front_file_name of a clip already 'ready'/'failed'/'unsupported'
			// from WEEKS ago still resolved to that row, and storage.Upload()
			// (below) overwrites the deterministic R2 key BEFORE
			// mark_alarm_clip_ready/attach_alarm_clip_secondary can reject the
			// UPDATE for a terminal status -- the R2 object was replaced even
			// though the metadata UPDATE failed. Now the primary (front)
			// correlation requires the clip to still be active (like the
			// fallback below), and the secondary (cabin) one requires that
			// slot to still be empty -- closing the overwrite window for both
			// cameras without breaking the real flow (the cabin file MUST be
			// able to arrive after the front file put the clip in 'ready', see
			// 0044).
			row := tx.QueryRow(ctx,
				`SELECT id, alarm_time, COALESCE(cabin_file_name, ''), (cabin_file_name = $2)
				 FROM alarm_video_clips
				 WHERE device_id = $1 AND (
				     (front_file_name = $2 AND status IN ('requested', 'uploading'))
				     OR (cabin_file_name = $2 AND storage_key_secondary IS NULL)
				 )
				 ORDER BY requested_at DESC LIMIT 1`,
				dev.ID, deviceFileName,
			)
			if scanErr := row.Scan(&clipID, &alarmTime, &cabinFileName, &isSecondary); scanErr == nil {
				return nil
			}
			// No match by name -- fall through to the fallback below instead
			// of returning an error, so the attempt is not lost.
		}
		row := tx.QueryRow(ctx,
			`SELECT id, alarm_time, COALESCE(cabin_file_name, ''), false FROM alarm_video_clips
			 WHERE device_id = $1 AND status IN ('requested', 'uploading')
			 ORDER BY requested_at DESC LIMIT 1`,
			dev.ID,
		)
		return row.Scan(&clipID, &alarmTime, &cabinFileName, &isSecondary)
	})
	if err != nil {
		if dev.ID == uuid.Nil {
			log.Printf("alarmclip: upload from %s (file %q) by an unprovisioned device, rejected: %v", imei, deviceFileName, err)
			http.Error(w, "unknown device", http.StatusNotFound)
			return
		}
		// No pending request. NEVER answer with an error here: the device
		// treats it as "retry later" and re-uploads the SAME file over
		// cellular data on every reconnect (observed on a JC261: ~1.6 MB every
		// few minutes). See handleUnrequestedUpload.
		b.handleUnrequestedUpload(w, r, dev, imei, deviceFileName, fileReader)
		return
	}
	tenantID = dev.TenantID.String()

	// The uploaded file may NOT be the one belonging to the pending request
	// resolved above (see maxClipTimestampDrift): this dashcam has its own
	// upload queue unrelated to our requests, and the "most recent pending
	// request" fallback cannot tell "this answers my request" from "the
	// device decided to send something else now". The real file name carries
	// its own embedded date/time (the REAL recorded event), compared against
	// the time of the alarm that originated the request. A large drift is the
	// real signal that it is NOT the right file -- instead of attaching it
	// anyway (misattributed), it is quarantined: uploaded to a SEPARATE key
	// (the content is never lost) and the original request is left EXACTLY
	// as it was -- neither 'ready' with the wrong video nor needlessly
	// 'failed' (the right file may still arrive); the existing staleness job
	// closes it if it never does.
	if parsedTime, ok := parseEventFilenameTime(deviceFileName); ok {
		drift := parsedTime.Sub(alarmTime)
		if drift < 0 {
			drift = -drift
		}
		if drift > maxClipTimestampDrift {
			quarantineKey := fmt.Sprintf("tenants/%s/alarm-clips/unmatched/%s-%s.ts", tenantID, imei, uuid.NewString())
			if err := b.storage.Upload(r.Context(), quarantineKey, fileReader); err != nil {
				log.Printf(
					"alarmclip: %s: file %q does not match the time of request %s (alarm_time=%s, file=%s, drift=%s) -- AND quarantining it failed: %v",
					imei, deviceFileName, clipID, alarmTime, parsedTime, drift, err,
				)
				http.Error(w, "internal error", http.StatusInternalServerError)
				return
			}
			log.Printf(
				"alarmclip: %s: file %q quarantined (%s) -- does not match the time of request %s (alarm_time=%s, file=%s, drift=%s), the request stays pending",
				imei, deviceFileName, quarantineKey, clipID, alarmTime, parsedTime, drift,
			)
			w.WriteHeader(http.StatusOK)
			return
		}
	}

	// Secondary (cabin) camera: uploads to its own key and only ATTACHES
	// (attach_alarm_clip_secondary, migration 0044) -- it never touches
	// status/completed_at, the row may already be 'ready' since the front
	// file arrived. Best-effort end to end: a failure here NEVER marks the
	// clip as failed (the front file is already a complete valid clip), it
	// is only logged.
	if isSecondary {
		key := fmt.Sprintf("tenants/%s/alarm-clips/%s-cabina.ts", tenantID, clipID)
		if err := b.storage.Upload(r.Context(), key, fileReader); err != nil {
			log.Printf("alarmclip: uploading secondary camera of clip %s to storage: %v", clipID, err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
		err = db.WithBypass(r.Context(), b.pool, func(ctx context.Context, tx pgx.Tx) error {
			_, err := tx.Exec(ctx, `SELECT attach_alarm_clip_secondary($1, $2)`, clipID, key)
			return err
		})
		if err != nil {
			log.Printf("alarmclip: attaching secondary camera of clip %s: %v", clipID, err)
		} else {
			log.Printf("alarmclip: secondary camera of clip %s (device %s) uploaded and stored at %s", clipID, imei, key)
		}
		w.WriteHeader(http.StatusOK)
		return
	}

	key := fmt.Sprintf("tenants/%s/alarm-clips/%s.ts", tenantID, clipID)
	if err := b.storage.Upload(r.Context(), key, fileReader); err != nil {
		log.Printf("alarmclip: uploading clip %s to storage: %v", clipID, err)
		b.markFailed(r.Context(), clipID, "failed", "could not upload the file to storage")
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	err = db.WithBypass(r.Context(), b.pool, func(ctx context.Context, tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `SELECT mark_alarm_clip_ready($1, $2, NULL, NULL)`, clipID, key)
		return err
	})
	if err != nil {
		log.Printf("alarmclip: marking clip %s ready: %v", clipID, err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	log.Printf("alarmclip: clip %s (device %s) uploaded and stored at %s", clipID, imei, key)
	w.WriteHeader(http.StatusOK)

	// Chained, NEVER in parallel with the front file: a GT06 device has only
	// ONE pending command slot per connection (F1), and sending two
	// UPLOADFILE commands almost at once would hit "the device already has a
	// pending command". Only NOW that the front file confirmed its real upload
	// is the cabin file requested -- best-effort; a failure here does not
	// affect the clip already 'ready' with the front file.
	if cabinFileName != "" {
		text := fmt.Sprintf("%s,%s#", clipUploadFileCommand, cabinFileName)
		if err := b.gt06.SendRawCommandFireAndForget(imei, text); err != nil {
			log.Printf("alarmclip: secondary camera request %s to %s failed (best-effort, does not affect the ready clip): %v", text, imei, err)
		} else {
			log.Printf("alarmclip: %s sent to %s (secondary camera, best-effort)", text, imei)
		}
	}
}

// handleUnrequestedUpload handles a file the device uploads with no pending
// request. Two cases:
//
//  1. It is EXACTLY the file requested for an alarm, and that request was
//     marked failed because the device took longer than the wait time (the
//     staleness job closes it after 5-10 min): real evidence that arrived
//     late -> it is recovered (recover_alarm_clip_late, 0053).
//  2. It matches nothing: accepted with 200 and discarded, so the device
//     drops it from its queue and stops retrying.
//
// Both cases record a device health event for the platform. It inherits
// handleUpload's barrier: it is only reached with a live authenticated GT06
// session for that IMEI.
func (b *Bridge) handleUnrequestedUpload(w http.ResponseWriter, r *http.Request, dev db.Device, imei, fileName string, file io.Reader) {
	ctx := r.Context()
	if fileName != "" {
		var clipID, tenantID string
		var isFront bool
		err := db.WithBypass(ctx, b.pool, func(ctx context.Context, tx pgx.Tx) error {
			return tx.QueryRow(ctx,
				`SELECT id::text, tenant_id::text, front_file_name = $2
				   FROM alarm_video_clips
				  WHERE device_id = $1
				    AND ((front_file_name = $2 AND status = 'failed' AND storage_key IS NULL)
				      OR (cabin_file_name = $2 AND status = 'ready' AND storage_key_secondary IS NULL))
				  ORDER BY requested_at DESC LIMIT 1`,
				dev.ID, fileName,
			).Scan(&clipID, &tenantID, &isFront)
		})
		if err == nil {
			suffix, fn := ".ts", `SELECT recover_alarm_clip_late($1, $2)`
			if !isFront {
				suffix, fn = "-cabina.ts", `SELECT attach_alarm_clip_secondary($1, $2)`
			}
			key := fmt.Sprintf("tenants/%s/alarm-clips/%s%s", tenantID, clipID, suffix)
			if err := b.storage.Upload(ctx, key, file); err != nil {
				// Real evidence: having the device retry is better than
				// losing it.
				log.Printf("alarmclip: %s: could not store recovered clip %s (%q): %v", imei, clipID, fileName, err)
				http.Error(w, "internal error", http.StatusInternalServerError)
				return
			}
			err = db.WithBypass(ctx, b.pool, func(ctx context.Context, tx pgx.Tx) error {
				if _, err := tx.Exec(ctx, fn, clipID, key); err != nil {
					return err
				}
				return db.RecordDeviceHealthEvent(ctx, tx, dev.ID, "clip_recovered_late", fileName, "info",
					"Alarm clip recovered: the camera uploaded it after the wait time",
					map[string]any{"filename": fileName, "clip_id": clipID}, 0)
			})
			if err != nil {
				log.Printf("alarmclip: %s: storing recovered clip %s: %v", imei, clipID, err)
			} else {
				log.Printf("alarmclip: %s: clip %s recovered (file %q arrived late), stored at %s", imei, clipID, fileName, key)
			}
			w.WriteHeader(http.StatusOK)
			return
		}
	}

	n, _ := io.Copy(io.Discard, file)
	err := db.WithBypass(ctx, b.pool, func(ctx context.Context, tx pgx.Tx) error {
		return db.RecordDeviceHealthEvent(ctx, tx, dev.ID, "upload_unrequested", fileName, "warning",
			"The camera uploaded a file nobody requested (wastes data if repeated)",
			map[string]any{"filename": fileName}, n)
	})
	if err != nil {
		log.Printf("alarmclip: %s: recording health event: %v", imei, err)
	}
	log.Printf("alarmclip: upload from %s (file %q, %d bytes) with no pending request: accepted and discarded so the device stops retrying", imei, fileName, n)
	w.WriteHeader(http.StatusOK)
}

// extractUploadedFile: the device does NOT upload the .ts as the whole
// request body -- it wraps it in multipart/form-data with several fields
// (confirmed byte by byte against the first real file received from a
// JC261): "filename" (the EVENT_...ts name), "timestamp" (epoch millis),
// "sign" (a base64 signature/hash, not validated today), and "file" (the
// clip content, with Content-Type "multipart/form-data" -- mislabeled by the
// device, irrelevant for extraction). Without this, handleUpload stored the
// WHOLE multipart envelope (boundaries + metadata fields + the file) as the
// .ts, producing a file the player could never demux.
//
// It also returns the "filename" field (empty if absent) -- see handleUpload,
// exact name correlation (migration 0044).
//
// r.MultipartReader() reads straight from r.Body without buffering
// everything in memory first (unlike r.ParseMultipartForm) -- important
// because the real file can be several MB.
func extractUploadedFile(r *http.Request, imei string) (io.Reader, string, error) {
	mr, err := r.MultipartReader()
	if err != nil {
		// Content-Type is not multipart/form-data -- assume the raw body IS
		// the file (kept as a cheap defensive fallback in case another
		// firmware uploads differently; never confirmed necessary).
		return r.Body, "", nil
	}
	var deviceFileName string
	for {
		part, err := mr.NextPart()
		if err == io.EOF {
			return nil, "", fmt.Errorf("multipart without any \"file\" field")
		}
		if err != nil {
			return nil, "", fmt.Errorf("reading multipart: %w", err)
		}
		if part.FormName() == "file" {
			return part, deviceFileName, nil
		}
		// Metadata fields (filename/timestamp/sign) -- filename is used for
		// real correlation (see above); the rest is only logged for
		// diagnostics. NextPart() advances to the next boundary without
		// draining the current one first.
		if value, err := io.ReadAll(io.LimitReader(part, 4096)); err == nil {
			log.Printf("alarmclip: %s: multipart field %q = %q", imei, part.FormName(), string(value))
			if part.FormName() == "filename" {
				deviceFileName = string(value)
			}
		}
	}
}

// handleFilelist receives the device's report of which clips it has
// (configured with FILELIST,http://<host>:<port>/filelist/<IMEI>#). Discovery
// phase: it only logs the raw body, without assuming its format yet.
func (b *Bridge) handleFilelist(w http.ResponseWriter, r *http.Request) {
	imei := r.PathValue("imei")
	body, err := io.ReadAll(io.LimitReader(r.Body, 64*1024))
	if err != nil {
		http.Error(w, "error reading body", http.StatusBadRequest)
		return
	}
	log.Printf("alarmclip: filelist reported by %s: %q", imei, string(body))
	w.WriteHeader(http.StatusOK)
}

func (b *Bridge) markUploading(ctx context.Context, clipID string) {
	err := db.WithBypass(ctx, b.pool, func(ctx context.Context, tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `UPDATE alarm_video_clips SET status = 'uploading' WHERE id = $1 AND status = 'requested'`, clipID)
		return err
	})
	if err != nil {
		log.Printf("alarmclip: marking clip %s in progress: %v", clipID, err)
	}
}

func (b *Bridge) markFailed(ctx context.Context, clipID, status, detail string) {
	err := db.WithBypass(ctx, b.pool, func(ctx context.Context, tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `SELECT mark_alarm_clip_failed($1, $2, $3)`, clipID, status, detail)
		return err
	})
	if err != nil {
		log.Printf("alarmclip: marking clip %s as %s: %v", clipID, status, err)
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
