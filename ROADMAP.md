# Roadmap

OpenMDVR is early-stage software. It already runs with real hardware, but it
is **not** production-certified. Priorities, roughly in order:

## Scope

OpenMDVR is built for carriers and operators that want to self-host their
fleet video and location data as a modular monolith — one binary, one API,
one database, run by one person. The load test already stores every position
from 50,000 simulated trackers on a 2 vCPU / 4 GB server
([docs/scalability.md](docs/scalability.md)).

Running thousands or more devices **is possible** — it needs more
infrastructure and some distribution (multiple device servers behind a TCP
load balancer, batching the insert path). We do not yet publish a turnkey
guide for that, because it is not the common case; the horizontal-scaling
guide below tracks it. OpenMDVR can grow in that direction, it just is not,
out of the box, an elastic SaaS for hundreds of thousands of devices under
one roof.

## Security & compliance
- [ ] Login rate limiting and lockout policy
- [ ] Optional OIDC / SAML single sign-on and MFA
- [ ] Audit log export (who saw which video/location, when) for supply-chain security programs
- [ ] Encryption-at-rest guidance and key management for clips
- [ ] Independent penetration test; SBOM (CycloneDX) published with each release
- [ ] Map security controls to NIST SP 800-53 / CIS Controls

## Platform
- [x] English/Spanish UI (i18n) — runtime language switch, new locales via a single JSON file
- [ ] Replace react-leaflet (Hippocratic license) with plain Leaflet bindings
- [ ] One-command install script and hardened production compose profile
- [ ] Horizontal scaling guide (multiple device-server instances behind a TCP load balancer)
- [x] Load-test results on reference hardware ([docs/scalability.md](docs/scalability.md)); repeat for each release

## Devices
- [ ] JT808 remote configuration, photo and recorded-video playback
- [ ] ADAS/DSM event payloads and evidence photos
- [ ] Two-way audio (talk-back)
- [ ] Teltonika and Queclink protocols
- [ ] CAN bus / OBD telemetry

See [docs/protocols.md](docs/protocols.md) for protocol details and open
issues labeled `good first issue` to get started.
