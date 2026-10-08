# jt808-server

The device-facing server of OpenMDVR. A single Go binary that:

- speaks **JT/T 808-2019** signaling with MDVRs/dashcams and **JT/T 1078-2016**
  video, translating that video to standard RTP for ZLMediaKit;
- speaks the binary **GT06** protocol (Concox-compatible GPS trackers and
  dashcams that push video over RTMP);
- serves the media-server hooks, live-view metering, preview snapshots,
  remote commands and alarm clip uploads.

All tenant data is written to PostgreSQL through `SECURITY DEFINER` functions;
the API (`/api`) and dashboard (`/web`) never talk to devices directly.

## Package layout

| Package | Responsibility |
|---|---|
| `cmd/server` | Wiring: builds every component once and starts the listeners. |
| `internal/config` | Configuration from environment variables only. |
| `internal/db` | The only layer that talks to Postgres (device lookup, positions, alarms, usage, health events). |
| `internal/session` | Per-connection JT808 state and the registry used to send commands to a live device. |
| `internal/jt808server` | JT808 TCP server: registration, auth, heartbeat, location/alarm reports. |
| `internal/gt06server` | GT06 TCP server: framing, CRC-16/X-25, login, positions, alarms, heartbeat status, camera event reports, text commands with reply correlation. |
| `internal/gpsfilter` | GPS quality filter applied before any position is stored (drift, outliers, late samples). |
| `internal/datausage` | Counts real rx/tx bytes per device connection (cellular data reporting). |
| `internal/videobridge` | Protocol-agnostic live video core: `Protocol` interface, hook dispatcher, one-time playback tickets, active-stream registry, live-view meter, snapshots. |
| `internal/jt1078bridge` | `videobridge.Protocol` for JT1078: 0x9101 signaling, packet reassembly, NALU split, RTP/FU-A packetization. |
| `internal/gt06videobridge` | `videobridge.Protocol` for GT06 dashcams that push RTMP (`RTMP,ON/OFF` commands). |
| `internal/commands` | Protocol-agnostic remote command layer (e.g. `engine_stop`/`engine_resume`) plus raw GT06 configuration commands. |
| `internal/alarmclip` | Alarm-linked clip retrieval and native photo capture over the public HTTP upload endpoint. |
| `internal/storage` | Object storage abstraction (S3-compatible) used for uploaded clips. |

Adding a new video protocol means implementing `videobridge.Protocol` in a new
package and registering it in `videobridge.NewDispatcher` in `cmd/server` — no
changes to the existing protocol packages. ZLMediaKit only accepts one URL per
hook type, which is why the hooks go through a shared dispatcher.

## Running

```bash
cd infra
docker compose up -d --build jt808-server
```

The server connects to Postgres as the restricted `app_user` role. Because the
tenant is only known after resolving the device identifier, device-side
transactions run with `app.bypass_rls='true'` (see `internal/db/db.go`);
schema integrity triggers still apply.

### Ports and configuration

| Variable | Default | Purpose |
|---|---|---|
| `JT808_LISTEN_ADDR` / `JT808_MAX_CONNECTIONS` | `:8808` / 5000 | JT808 signaling (public). |
| `JT1078_LISTEN_ADDR` / `JT1078_LISTEN_PORT` / `JT1078_MAX_CONNECTIONS` | `:8081` / 8081 / 5000 | JT1078 video (public). |
| `PUBLIC_IP` | `127.0.0.1` | Address sent to devices in 0x9101; must be reachable from the cellular network. |
| `GT06_LISTEN_ADDR` / `GT06_MAX_CONNECTIONS` | `:5023` / 5000 | GT06 (public). |
| `GT06_VIDEO_APP` | `live` | RTMP app name GT06 dashcams publish under. |
| `ALARM_CLIP_LISTEN_ADDR` / `ALARM_CLIP_MAX_CONNECTIONS` | `:8083` / 200 | Device file uploads (public). |
| `ALARM_CLIP_PUBLIC_BASE_URL` | `http://127.0.0.1:8083` | Upload URL sent to devices. |
| `HTTP_LISTEN_ADDR` | `:8082` | Internal control API and media-server hooks. **Never expose publicly.** |
| `ZLM_BASE_URL`, `ZLM_API_SECRET` | | ZLMediaKit API. |
| `ZLM_PLAY_URL_FORMAT`, `ZLM_WEBRTC_PLAY_BASE_URL`, `ZLM_GT06_PLAY_BASE_URL` | | Public playback URLs returned to the browser. |
| `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `APP_USER_PASSWORD`, `PG_MAX_CONNS` | | Postgres (pool shared by all tenants' ingestion; default 16). |
| `R2_ENDPOINT`, `R2_ACCESS_KEY`, `R2_SECRET_KEY`, `R2_BUCKET` | | Optional S3-compatible bucket for clips. |

### Provisioning a test device

Device provisioning is a platform action (RLS bypass only), never device
self-service. Use the dashboard/API, or directly:

```bash
docker exec -i openmdvr-postgres psql -U postgres -d openmdvr <<'EOSQL'
INSERT INTO tenants (name) VALUES ('Test fleet') RETURNING id;
INSERT INTO devices (tenant_id, jt808_terminal_id, label, status)
VALUES ('<tenant id>', '13800000001', 'Test MDVR', 'active');
EOSQL
```

**Leading zeros:** the BCD decoder strips leading zeros from the terminal
number, so a device configured as `013800000001` arrives as `13800000001`.
`devices.jt808_terminal_id` has a `CHECK` that rejects leading zeros so this
mistake cannot be made at provisioning time.

## Testing

```bash
go build ./... && go vet ./... && go test ./...
```

Tests that need Postgres or real hardware are intentionally excluded from the
unit suites; those paths are exercised with the simulators below against a
running stack.

### Simulators (`testclient/`)

The simulators are independent implementations that share no code with the
server — reusing the same codec would only prove the code understands itself,
not that it interoperates with a different implementer.

| Script | What it does |
|---|---|
| `simulate.py` | JT808 terminal: register, auth, heartbeat, location with an emergency alarm. |
| `simulate_video.py` | JT808 signaling + 0x9101 request + JT1078 push of a real H.264 file, then verifies the stream in ZLMediaKit. |
| `simulate_video_live.py` | Same, but forwards a live RTMP source (e.g. a phone, see `README_phone.md`). |
| `verify_usage_event.py` | Plays a stream over HTTP-FLV and checks that a real `usage_events` row is written. |
| `simulate_gt06.py` | GT06 tracker: login, position, heartbeat, SOS alarm. |
| `simulate_gt06_drift.py` | GT06 stationary drift + multipath outlier + real departure, to check the GPS filter end to end. |
| `simulate_gt06_photo.py` | GT06 dashcam answering native photo commands and uploading JPEGs. |

```bash
# short test clip (any real video works too)
ffmpeg -f lavfi -i testsrc=duration=2:size=320x240:rate=5 \
  -pix_fmt yuv420p -c:v libx264 -profile:v baseline -x264-params keyint=5 \
  -f h264 test.h264

python testclient/simulate.py --terminal 13800000001
python testclient/simulate_video.py --terminal 13800000001 --channel 1 --fps 5 \
  --h264 test.h264 --zlm-secret "$ZLM_API_SECRET"
python testclient/simulate_gt06.py --imei 123456789012345
```

## Protocol coverage

**JT808:** registration (0x0100), authentication (0x0102), heartbeat
(0x0002), location reports (0x0200) with the full standard JT/T808-2019 alarm
set including basic ADAS/DSM bits, ignition/power status bits, and live video
requests (0x9101). Not yet implemented: vendor-specific extended
payloads (0x64/0x65), parameter configuration (0x8103), photo capture
(0x8801), media upload (0x0800/0x0805/0x1211/0x1212), recorded playback, CAN
bus data, 0x0702/0x0900/0x0107/0x0108.

**GT06:** login (0x01), heartbeat with terminal status (0x13), positions
(0x22 and the classic 0x12 numbering), alarms (0x26/0x16), command replies
(0x15 and the vendor 0x21 variant), camera event reports (0x95), text
commands (engine cut/resume, RTMP start/stop, native photo, clip upload,
device configuration). Not covered: LBS fields (ignored), the extended
`0x7979` header, and vendor variants not confirmed against real hardware.

**Video:** JT1078 is translated to RTP (FU-A fragmentation per RFC 6184) and
published by ZLMediaKit; GT06 dashcams push RTMP directly to ZLMediaKit.
Playback is authorized per request with one-time tickets validated in the
`on_play`/`on_publish` hooks.

## Design notes

### Why a JT1078 bridge

The open-source edition of ZLMediaKit does not speak JT1078. Pointing the
device directly at ZLMediaKit's RTP port is not reliable because JT1078
packets are not bit-compatible with RTP. `internal/jt1078bridge` therefore:

1. Receives a video request (`POST /api/v1/9101` from the API, or the
   `on_stream_not_found` hook).
2. Opens an RTP receiver in ZLMediaKit (`openRtpServer`, `tcp_mode=1`) and
   sends 0x9101 telling the device to connect its video to this process.
3. Pairs the incoming JT1078 connection with the pending request (SIM +
   channel), reassembles fragmented frames, splits NALUs, packetizes them as
   RTP (FU-A for NALUs larger than 1400 bytes — a reassembled I-frame can be
   70 KB+ and RTP receivers reject oversized packets even over TCP), and
   forwards them with the 2-byte length framing ZLMediaKit expects.

`RequestVideo()` is idempotent per stream: a second viewer, or an
`on_stream_not_found` fired in the race window right after a push starts,
reuses the active stream instead of reopening the RTP receiver (which would
drop the device's connection).

### Live-view billing and limits

- **Usage is metered on real bytes**, not on requests: `hook.on_flow_report`
  fires per player session with the actual bytes served, and `usage_events`
  is written from there. `player=false` reports (the bridge's own ingest
  push) are ignored. `general.flowThreshold` must be `0`, otherwise short
  sessions below the threshold never produce a usage event.
- **Per-session limit** (`tenants.max_live_view_seconds`) is enforced on the
  server; the clock starts when the device actually begins sending frames.
- **Monthly quota** (`tenants.live_view_monthly_quota_seconds`) is enforced
  by a central meter (`videobridge/meter.go`): each viewer-requested camera
  is its own billable session, checkpoints are written to `usage_events`
  every 30 s, and all sessions of a tenant are cut within about a second of
  the quota reaching zero. Requests with an exhausted quota fail with HTTP
  402 before any command is sent to the device. Auto-started channels and
  snapshot captures are not billed as viewing time.

### Hardening against untrusted traffic

Device ports are necessarily public, so every input is treated as hostile:

- Per-connection reassembly buffers are capped; the cap is checked on the
  **remainder** after extracting complete packets, never on the raw read
  (otherwise a burst of legitimate video fragments triggers a false positive).
- Concurrent connections are capped per listener (`*_MAX_CONNECTIONS`,
  including the upload server, which also has explicit read/write/idle
  timeouts).
- `recover()` runs per connection, and per message where a third-party
  parser can panic on malformed-but-structurally-valid input — one bad
  message is dropped without closing the connection.
- Coordinates are range-checked before touching the database, and any
  dispatch error is answered with an explicit failure reply so devices do
  not retry forever.
- GT06 timestamps outside `[2020, now+24h]` are replaced with the server
  receive time, so a device cannot create far-future rows that defeat
  retention.
- GT06 command replies are correlated per connection with a single pending
  slot; a late reply can never confirm a different command (engine cut and
  resume are never allowed to resolve each other).
- File uploads are accepted only while an authenticated GT06 session for that
  IMEI is alive, are size-limited, and are correlated by exact file name and
  embedded timestamp; files whose timestamp does not match the request are
  quarantined instead of being attached to the wrong alarm.

### Trust boundaries

- The internal HTTP API (`HTTP_LISTEN_ADDR`) performs no authentication by
  design: it is called only by ZLMediaKit and the backend API, which enforces
  JWT + RLS. Bind it to a private interface only.
- The JT808 authentication code is issued for protocol compatibility but is
  not a cryptographic identity. The real check is whether the terminal ID
  belongs to an active device provisioned for a tenant (see
  `internal/session/session.go`). GT06 has no cryptographic authentication
  either; the same rule applies to the IMEI.

### Third-party dependency audit

`github.com/cuteLittleDevil/go-jt808/protocol` (MIT) is used only as the
low-level codec (framing, escaping, checksum, message structs). Session
logic, tenant resolution and database writes are our own. When bumping its
version: check out the exact tag pinned in `go.sum`, grep the imported code
for network/exec/unsafe/`init()` usage, and run `govulncheck ./...`.
