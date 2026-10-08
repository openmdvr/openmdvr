# Roadmap

OpenMDVR is early-stage software. It already runs with real hardware, but it
is **not** production-certified. Priorities, roughly in order:

## Security & compliance
- [ ] Login rate limiting and lockout policy
- [ ] Optional OIDC / SAML single sign-on and MFA
- [ ] Audit log export (who saw which video/location, when) for supply-chain security programs
- [ ] Encryption-at-rest guidance and key management for clips
- [ ] Independent penetration test; SBOM (CycloneDX) published with each release
- [ ] Map security controls to NIST SP 800-53 / CIS Controls

## Platform
- [ ] English/Spanish UI (i18n) — the dashboard is currently Spanish only
- [ ] Replace react-leaflet (Hippocratic license) with plain Leaflet bindings
- [ ] One-command install script and hardened production compose profile
- [ ] Horizontal scaling guide (multiple device-server instances behind a TCP load balancer)
- [ ] Published load-test results on reference hardware for each release

## Devices
- [ ] JT808 remote configuration, photo and recorded-video playback
- [ ] ADAS/DSM event payloads and evidence photos
- [ ] Two-way audio (talk-back)
- [ ] Teltonika and Queclink protocols
- [ ] CAN bus / OBD telemetry

See [docs/protocols.md](docs/protocols.md) for protocol details and open
issues labeled `good first issue` to get started.
