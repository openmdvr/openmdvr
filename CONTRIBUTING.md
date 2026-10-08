# Contributing to OpenMDVR

Thanks for helping make fleet video and telematics safer and open. Every kind
of contribution counts: code, protocol captures from real devices, docs,
translations, security reviews and bug reports.

## Ground rules

- Be respectful — see the [Code of Conduct](CODE_OF_CONDUCT.md).
- **Security issues go through [SECURITY.md](SECURITY.md)**, never public issues.
- Never commit secrets, real device identifiers (IMEI / terminal IDs), SIM
  numbers, customer data or real IP addresses. Use `example.com`,
  `203.0.113.0/24` and the fake IMEI `490154203237518` in tests and docs.
- By contributing you agree that your work is licensed under Apache-2.0.
  Sign off your commits (`git commit -s`) to certify the
  [Developer Certificate of Origin](https://developercertificate.org/).

## Development setup

Requirements: Docker + Docker Compose, Go 1.25, Python 3.12, Node 20+.

```bash
git clone https://github.com/openmdvr/openmdvr.git
cd openmdvr
cp infra/.env.example infra/.env        # dev defaults, change secrets for prod
docker compose -f infra/docker-compose.yml up -d --build
```

Then open the dashboard (default `http://127.0.0.1:5175`) and create the first
platform admin with `api/scripts` (see [api/README.md](api/README.md)).

No hardware? Use the simulators in `jt808-server/testclient/`
(`simulate.py`, `simulate_gt06.py`, `simulate_video.py`).

## Running tests

```bash
# Go (device servers, video bridge)
cd jt808-server && go vet ./... && go test ./...

# API (needs the Postgres container running)
cd api && pip install -r requirements.txt -r requirements-dev.txt && pytest

# Row Level Security isolation suite
cd infra/postgres/tests && pytest

# Web
cd web && npm ci && npx tsc -b && npx vite build
```

## Pull requests

1. Open an issue first for anything larger than a small fix.
2. Keep PRs focused; include tests for behavior changes.
3. Changes to **authentication, RLS policies, tenant isolation, device
   ingestion or remote commands** need an explicit security review from a
   maintainer before merge. Explain the threat you considered in the PR.
4. New database changes go in a new numbered file in
   `infra/postgres/migrations/` — never edit an applied migration.
5. Every place that serves bytes to a client (live view, playback, download)
   must record a `usage_events` row.
6. Code, comments and docs are in English. The dashboard UI is currently in
   Spanish; i18n contributions are very welcome.

## Adding a device protocol

Location protocols write positions through the single
`insert_gps_position()` path, so geofences, speed limits, the GPS quality
filter and live push work automatically. Video protocols implement the
`videobridge.Protocol` interface in `jt808-server/internal/videobridge`.
See [docs/architecture.md](docs/architecture.md). Packet captures (hex dumps)
from real devices are extremely valuable — attach them to the issue with
identifiers redacted.
