# Scalability

Every tracker and camera holds a persistent TCP connection to the device
server and reports on a fixed interval. Capacity therefore comes down to two
numbers: how many connections the device server can hold, and how many
positions per second the whole pipeline can store.

This page reports what was measured, on what hardware, and how to reproduce
it. All numbers come from the load-test tool in [`loadtest/`](../loadtest).

## Test setup

| | |
|---|---|
| Machine | One desktop, Docker Desktop VM with 20 vCPU and 16 GB RAM |
| Device server | `jt808-server` image from this repository, **limited to 2 vCPU and 4 GB** (`--cpus=2 --memory=4g`), `PG_MAX_CONNS=16` |
| Database | `timescale/timescaledb:2.17.2-pg16`, all migrations applied, default configuration, not CPU-limited |
| Load generator | `loadtest`, running on the same machine in its own container |
| Traffic | GT06 protocol. Every simulated device opens its own TCP connection, logs in with its IMEI, waits for the login ACK, reports a GPS position every interval (with jitter) and sends a heartbeat every 3 minutes |

Client, server and database share one machine, so the results are a
conservative lower bound: the load generator competes for the same CPUs. The
database disk is the Docker Desktop virtual disk, which has much slower
`fsync` than a server SSD.

"Stored" means rows actually present in `gps_positions` after the run,
counted in the database, compared with the positions the clients sent.

## Results

| Devices | Report interval | Logins OK | Login latency p95 | Positions sent | Stored | Device server CPU / RAM | PostgreSQL CPU |
|---|---|---|---|---|---|---|---|
| 5,000 | 10 s | 5,000 / 5,000 | 2.1 s | 104,258 | 100% | 0.4 vCPU / 153 MB | not limiting |
| 20,000 | 10 s | 20,000 / 20,000 | 3 ms | 465,916 | 100% | 1.0 to 1.7 vCPU / 580 MB | 3.7 to 6.4 cores |
| 50,000 | 30 s | 50,000 / 50,000 | 2 ms | 549,759 | 100% | 1.8 vCPU / 1.0 to 1.3 GB | 6 to 6.5 cores |

The 50,000-device run sustained about 2,300 messages per second (positions
plus heartbeats) with zero failed logins, zero dropped connections and zero
lost positions. Memory on the device server stays around 20 to 26 KB per
connection.

A report interval of 10 seconds is more aggressive than most real trackers,
which report every 20 to 60 seconds while moving and less often when parked.

## The bottleneck we found and fixed

The first 20,000-device run failed: only 12,104 devices could log in (p95
login latency 12.9 s), and about half of the positions were still queued when
the run ended. Neither the device server (0.3 vCPU) nor PostgreSQL (under one
core) was busy.

`pg_stat_activity` showed 13 of 16 database connections waiting on
`Lock:object` and one on `IO:WALSync`. `insert_gps_position()` sends a
`pg_notify` for the live map, and PostgreSQL serializes the commit of every
notifying transaction behind a single database-wide lock that is held while
the WAL is flushed to disk. With synchronous commit, ingestion for the whole
platform was capped at one commit per `fsync`, about 550 positions per
second on this disk, regardless of cores or connections.

The fix is one statement: transactions that store GPS positions commit with
`synchronous_commit = off`, scoped to that transaction only
(`relaxCommitForTelemetry` in `jt808-server/internal/db/telemetry.go`).
Alarms, billing and every API write keep fully durable commits, and any
transaction that also inserts an alarm is forced back to a durable commit.
After the fix, the same 20,000-device run logged in every device with a p95
of 3 ms and stored every position.

The trade-off: if PostgreSQL itself crashes, positions acknowledged in
roughly the last 0.6 seconds can be lost. The database is never corrupted,
and a crash of the device server loses nothing extra. For telemetry that
arrives every 10 to 60 seconds per device, this is the usual choice.

## Sizing notes

- **Connections are cheap.** The connection limit per listener
  (`GT06_MAX_CONNECTIONS`, `JT808_MAX_CONNECTIONS`, default 5,000) is a safety
  cap, not a design limit. Raise it together with `DEVICE_SERVER_NOFILE`
  (the container's open-file limit, default 65536). Every capacity and
  rate limit is an environment variable; see the "Capacity and operational
  limits" section of `infra/.env.example`.
- **The device server is CPU-bound by message rate.** At about 2,300
  messages per second a 2-vCPU server is close to its limit. Add vCPUs, or
  run more device servers on separate ports.
- **PostgreSQL does the heavy work.** Each position runs the GPS quality
  filter bookkeeping, speed limits, geofence evaluation and the live-map
  notification inside `insert_gps_position()`, roughly 2.5 to 3 ms of database
  CPU per position. Plan several cores for the database above about 2,000
  positions per second.
- **Video is a different budget.** These tests cover telemetry only. Live
  video is bounded by bandwidth and the media server, and is controlled per
  tenant with server-enforced quotas.

Possible next steps, not yet implemented: batching several positions per
transaction in the device server, and moving geofence evaluation for very
large fleets out of the insert path.

## Reproducing

```bash
# 1. Start the database and apply migrations
./infra/init-env.sh
docker compose -f infra/docker-compose.yml up -d postgres migrate

# 2. Provision test devices (throwaway tenant "loadtest")
docker compose -f infra/docker-compose.yml exec -T postgres \
  psql -U postgres -d openmdvr -v count=20000 -v imei_base=990000000000000 \
  -f - < loadtest/provision.sql

# 3. Start the device server with a higher connection cap
GT06_MAX_CONNECTIONS=60000 DEVICE_SERVER_NOFILE=100000 \
  docker compose -f infra/docker-compose.yml up -d jt808-server

# 4. Run the load generator (from a machine or container that can reach port 5023)
cd loadtest
go run . -addr 127.0.0.1:5023 -count 20000 -interval 10s -ramp 120s -duration 180s

# 5. Compare positions sent with rows stored
docker compose -f infra/docker-compose.yml exec postgres \
  psql -U postgres -d openmdvr -c "SELECT count(*) FROM gps_positions"

# 6. Clean up
docker compose -f infra/docker-compose.yml exec -T postgres \
  psql -U postgres -d openmdvr -f - < loadtest/cleanup.sql
```

For more than about 28,000 connections from one client host, widen the
ephemeral port range (`net.ipv4.ip_local_port_range`) and raise `ulimit -n`
on both sides.
