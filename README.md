<p align="center">
  <img src="web/public/logo-mark.png" width="96" alt="OpenMDVR">
</p>

<h1 align="center">OpenMDVR</h1>

<p align="center">
  Self-hosted fleet video and GPS platform, built security-first.<br>
  Live dashcam video with audio, real-time tracking, alarms with video evidence,
  and tenant isolation enforced by the database.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  <a href="../../actions/workflows/ci.yml"><img alt="CI" src="../../actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Status: early stage" src="https://img.shields.io/badge/status-early%20stage-orange">
</p>

---

## Why OpenMDVR

Trucks carry the goods an economy runs on. Their cameras and trackers
usually report to closed cloud platforms the fleet cannot inspect, often
hosted abroad, and in the MDVR world often built on vendor software with a
public history of serious vulnerabilities. Location history and in-cab
video are sensitive supply-chain data, and carriers should be able to keep
them on infrastructure they control.

OpenMDVR is a platform you can audit, run on your own servers and harden.
It handles MDVRs, dashcams and GPS trackers in one system, and it was
designed around two things most fleet software treats as afterthoughts:
video as evidence, and security that does not depend on every line of
application code being correct.

## A different foundation

Most fleet platforms on the market share the same lineage. They are GPS
servers first: built to decode location reports from as many tracker brands
as possible, with users and permissions handled in application code, and
with video added later, if at all. Many white-label offerings are the same
open-source GPS engine under a different logo, so they inherit the same
strengths and the same limits.

OpenMDVR starts from the other end.

**Video is a first-class subsystem, not a plugin.**
Live video plays in the browser over WebRTC with low latency and with
audio, which is transcoded on the fly only while someone is watching. Both
device families are supported: JT/T 1078 MDVRs, where the platform asks the
device to stream, and RTMP dashcams such as the Jimi JC261/JC400, where the
device pushes. Front and cabin cameras are handled independently. A native
photo preview is shown before anyone opens a live stream, so a glance at a
camera costs kilobytes instead of minutes of cellular video.

**Alarms come with evidence.**
Camera events trigger automatic retrieval of the recorded clip. Each clip is
matched to its exact alarm by the timestamp embedded in the device's own
file name, and anything that does not match is quarantined instead of being
attached to the wrong incident.

**Isolation is enforced inside PostgreSQL.**
Every business table carries a tenant id and is protected by Row Level
Security, forced for the application role. A buggy query returns nothing
from another tenant instead of leaking it. Time-series tables are reachable
only through security-barrier views and audited write functions, and
isolation is tested with multiple tenants on every change.

**Video access uses one-time tickets.**
A stream URL is useless on its own. The API issues a single-use ticket bound
to tenant, device, channel and stream namespace, and the media server checks
it in its playback hook before serving the first byte. RTMP publishing is
authorized the same way.

**The core is protocol-agnostic.**
Devices plug into contracts, not into features. A new protocol implements a
decoder and the interfaces it needs, and inherits everything built on top:

| Contract | What a new protocol gets for free |
|---|---|
| `insert_gps_position()`, the single ingestion path | GPS quality filter, geofences, speed limits, live map push, reports, retention |
| `videobridge.Protocol` | playback tickets, authorization hooks, metering, quotas, snapshots, idle shutdown |
| `commands.Sender` | audited remote commands with strict reply correlation |
| `insert_alarm()` | in-app notifications and HMAC-signed webhooks |

**Cost is controlled on the server.**
Live video is metered from the bytes actually delivered, with per-session
and monthly quotas per tenant that the server enforces. A forgotten browser
tab cannot stream all day on a cellular plan.

**GPS data is cleaned, never destroyed.**
An adaptive per-device model removes parked drift, multipath jumps and
impossible speeds, so reports and geofences do not count kilometers a truck
never drove. Every corrected point keeps the original reading.

## Features

- Live video over WebRTC with audio, multi-camera, docked or floating players
- Real-time map pushed over Server-Sent Events, with clustered markers and vector maps
- Route history with speed-colored lines, stops, playback and events on the map
- Geofences with entry, exit and dwell events
- Alarms, notifications and signed outbound webhooks
- Recorded clip retrieval for camera events
- Remote engine stop and resume with typed confirmation and a full audit trail
- Vehicles, drivers, shifts, routes, and reports for distance and engine hours
- Scoped read-only or read-write API keys with usage auditing
- Usage metering, quotas and an optional billing module
- Devices: JT/T 808-2019 with JT/T 1078-2016 video, the GT06 tracker family,
  and RTMP dashcams of the Jimi JC261/JC400 family

## Scale

Every device holds a persistent TCP connection, so capacity is measured in
concurrent connections and in positions stored per second. In our load test,
a device server limited to **2 vCPU and 4 GB held 50,000 simulated trackers**
with zero failed logins (p95 login latency 2 ms) and stored every one of the
549,759 positions they sent, at about 2,300 messages per second.

The device server is written in Go, with one lightweight goroutine per
connection (about 20 to 26 KB each), bounded buffers, panic recovery per
connection and a single database ingestion path. The default limit of 5,000
connections per listener is a safety cap set in configuration, not an
architectural ceiling. On the browser side, maps render with WebGL vector
tiles and clustered markers, and position updates reach every open dashboard
through a single `LISTEN/NOTIFY` fan-out per API process instead of one
database connection per viewer.

Hardware, method, the bottleneck we found and fixed along the way, and how to
reproduce the numbers are in [docs/scalability.md](docs/scalability.md).

## Quick start

```bash
git clone https://github.com/openmdvr/openmdvr.git
cd openmdvr
./infra/init-env.sh        # creates infra/.env with random secrets
docker compose -f infra/docker-compose.yml up -d --build
docker compose -f infra/docker-compose.yml exec api python scripts/bootstrap_admin.py
```

Open <http://127.0.0.1:5175> and sign in. To try it without hardware, use
the device simulators in [jt808-server/testclient](jt808-server/testclient).

## Architecture

```
 MDVR, dashcam, tracker ──TCP──▶ device server (Go) ──▶ PostgreSQL + TimescaleDB (RLS)
 JT808 · JT1078 · GT06 · RTMP         │                        ▲
                                      ▼                        │
                                ZLMediaKit ──WebRTC──▶ Browser ◀── API (FastAPI) ── SSE
```

A modular monolith: one Go binary for every device protocol, one API, one
database. No Kubernetes or message broker is required, and it runs on a
single small VM. See [docs/architecture.md](docs/architecture.md),
[docs/security-model.md](docs/security-model.md) and
[docs/protocols.md](docs/protocols.md).

## Security

Security is the reason this project exists. Several widely deployed MDVR
platforms have publicly documented critical vulnerabilities: SQL injection,
default credentials, arbitrary file upload and path traversal.
[docs/mdvr-platform-security.md](docs/mdvr-platform-security.md) lists those
records with their sources and shows how OpenMDVR is designed against each
pattern, including what it cannot fix.

Read the [security model](docs/security-model.md) and report vulnerabilities
privately as described in [SECURITY.md](SECURITY.md).

## Contributing

Packet captures from real devices, security reviews, translations and code
are all welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) and the
[roadmap](ROADMAP.md).

## Status

Early stage, running with real hardware in a pilot. OpenMDVR is not
certified under any regulatory program such as C-TPAT or FMCSA ELD. It can
support those programs, but it is not a compliance guarantee.

## License

[Apache License 2.0](LICENSE). Third-party notices are in [NOTICE](NOTICE).
