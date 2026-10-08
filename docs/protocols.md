# Device protocols

OpenMDVR runs every device listener in a single Go binary
(`jt808-server/`). Location data from every protocol goes through the same
database path (`insert_gps_position()`), so geofences, speed limits, the GPS
quality filter, live push and reports work identically for all of them.
Video protocols implement one interface (`videobridge.Protocol`), so a new
camera family plugs in without touching the others.

| Family | Transport | Status |
|---|---|---|
| **JT/T 808-2019** (MDVRs, many dashcams) | TCP | Registration/auth, heartbeat, location (0x0200) incl. ACC & status bits, basic alarms (incl. ADAS/DSM flags as bits) |
| **JT/T 1078-2016** (live video for JT808 devices) | TCP → RTP → ZLMediaKit | Live video per channel (0x9101/0x9102); bridge does H.264 reassembly + RFC 6184 FU-A fragmentation |
| **GT06 / Concox family** (low-cost trackers, Jimi dashcams) | TCP | Login, heartbeat with ignition/relay state, position and alarm (both 0x22/0x26 and 0x12/0x16 numbering), command replies (0x15/0x21), engine stop/resume, camera event reports (0x95) |
| **RTMP push** (Jimi JC261 / JC400 dashcams) | RTMP → ZLMediaKit | Live video for two cameras (front/cabin) started via GT06 text command, authorized in `on_publish`; native photo capture; event clip upload over HTTP |
| **Browser delivery** | WebRTC (default), HTTP-FLV | Low-latency playback **with audio** (AAC → Opus transcoding in a pinned ZLMediaKit build) |

## Not yet supported (good contribution targets)

- JT808 vendor ADAS/DSM payloads (0x64/0x65): distance, event photos.
- JT808 remote parameters (0x8103), photo (0x8801), media upload
  (0x0800/0x0801/0x1211/0x1212), recorded-video playback.
- CAN bus / OBD data, fuel, door sensors (persisted as typed fields).
- GT06 LBS (cell tower) positions and the many vendor variants.
- Two-way audio (talk-back) — server side is ready (Opus → AAC); the device
  command for the JC261 is still unknown.
- Teltonika, Queclink, Ruptela and other popular tracker protocols.

## Adding a protocol

1. Add a listener package under `jt808-server/internal/` following the
   hardening checklist in [security-model.md](security-model.md#3-device-ingestion-internet-facing-tcp).
2. Resolve the device by its identifier, then write positions with
   the GPS-quality-filtered position insert in `internal/db` and alarms
   with `db.InsertAlarm`.
3. For video, implement `videobridge.Protocol` and register it in
   `cmd/server/main.go`.
4. Add a Python simulator under `jt808-server/testclient/` written
   independently of the server code, so tests prove interoperability rather
   than that the code agrees with itself.
5. Add a migration extending the `device_protocol` enum and identifier checks.
