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
| Device server | `device-server` image from this repository, **limited to 2 vCPU and 4 GB** (`--cpus=2 --memory=4g`), `PG_MAX_CONNS=16` |
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
(`relaxCommitForTelemetry` in `device-server/internal/db/telemetry.go`).
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

## Multiple device servers (horizontal scaling)

A single device server on 2 vCPU handles 50,000 trackers. Beyond that, or
for redundancy, you can run several device-server instances behind a TCP
load balancer. The platform is designed to make this straightforward because
PostgreSQL is the only shared state.

### What already scales without changes

**API replicas** are already stateless in the right way. Every API process
runs a `LISTEN` loop on the same PostgreSQL notification channels
(`gps_positions`, `alarms`, etc.). When the device server writes a position
or alarm, `pg_notify` wakes every API replica simultaneously; each one
pushes the event to whichever SSE clients are connected to it. Start as many
API replicas as needed — nginx round-robins browser requests across them and
they self-coordinate through the database.

**ZLMediaKit** scales as a pool; see the [Media nodes](#media-nodes-video)
section below.

### What the device server needs for multi-instance

Each device-server process keeps an in-memory map of `IMEI → TCP connection`.
Only the node that holds a device's connection can send it a command (engine
cut/restore, photo request, etc.). Two mechanisms close this gap:

**1 — TCP load balancing with source-IP hashing**

Use `ip_hash` (nginx stream) or `balance source` (HAProxy) so that a given
device's IP address is always sent to the same device-server node. Because
4G modems keep the same carrier IP for the lifetime of a session, this is
sticky enough in practice. When a device reconnects after a carrier IP
change it may land on a different node, which is fine — it re-registers
there.

```nginx
# nginx stream block
stream {
  upstream device_servers {
    hash $remote_addr consistent;
    server device-server-a:5023;
    server device-server-b:5023;
    # JT808
    server device-server-a:5080;
    server device-server-b:5080;
  }
  server { listen 5023; proxy_pass device_servers; }
  server { listen 5080; proxy_pass device_servers; }
}
```

**2 — Command routing via PostgreSQL LISTEN/NOTIFY**

*Not yet implemented — this is the planned path.*

The direct HTTP call from the API to the device server (`POST /commands`)
works for a single instance. For multiple instances the API needs to know
which node holds the target device, and that node needs to receive the
command.

The approach adds one table and replaces the direct HTTP call with a
database-mediated fan-out — the same mechanism already used for SSE:

```sql
-- Which node currently holds each device's TCP connection.
-- Inserted on login, deleted on disconnect, heartbeat-updated.
CREATE TABLE device_nodes (
  device_id  UUID PRIMARY KEY REFERENCES devices(id),
  node_id    TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Command flow:
1. API writes the command row to `commands` (durable).
2. API reads `device_nodes` to find `node_id` for that device.
3. API calls `pg_notify('cmd_<node_id>', '{"command_id":"…"}')`.
4. Each device-server listens on `cmd_<its own node_id>`.
5. The right node wakes up, fetches the command row, sends it over the TCP
   connection.

If the NOTIFY is missed (node restart, reconnect window), the device-server
polls `commands WHERE status = 'pending' AND device_id IN (my connected devices)`
on startup and periodically — so no command is silently lost.

### State that is per-process (not shared)

| State | Where | Notes |
|---|---|---|
| IMEI / terminal-ID → TCP conn | in-memory registry | per node; cleaned up on disconnect |
| Outgoing message sequence counter | per-session struct | reset on reconnect; harmless |
| GPS quality-filter state | per-session struct | resets on reconnect; first point after reconnect may be dropped |
| Active stream registry | in-memory map | moving to `active_streams` table closes this gap |
| SSE client queues | API in-memory | per replica; refilled by `pg_notify` on reconnect |
| SSE auth tickets (30 s TTL) | in-memory ticket store | a viewer that lands on a different replica re-fetches a ticket; self-correcting |

### Media nodes (video)

Video is a different budget from telemetry and scales on a different axis.
Playback fan-out to browsers, audio transcoding and RTMP ingest from push
dashcams all run on the media server (ZLMediaKit), which is bandwidth- and
CPU-bound per concurrent viewer, not per connected device. Concurrent viewers
are always far fewer than the fleet size — nobody watches tens of thousands of
cameras at once — and live video is on-demand, metered from real bytes and
capped per tenant by server-enforced quotas.

The media server runs as a **pool of one or more ZLMediaKit nodes**. The
device server stays a single control plane: it chooses a node for each pulled
(JT1078) stream, learns the node each pushed (GT06/RTMP) stream landed on from
the `mediaServerId` in every media hook, records that affinity, and builds each
stream's playback URL for the node that actually holds it. All nodes point
their hooks back at the one device server, so tickets, quota metering and the
active-stream registry stay authoritative in a single place.

A single node is the default and needs no extra configuration — the legacy
`ZLM_*` settings synthesize it. To add capacity:

1. Run another ZLMediaKit instance with its own `ZLM_MEDIA_SERVER_ID` and its
   own ports (RTMP 1935, WebRTC media 8880, the rtp_proxy range).
2. List every node in `ZLM_NODES` (JSON), each with its id (matching that
   node's `mediaServerId`), internal API base/secret, and browser-facing
   playback bases.
3. Add a reverse-proxy rule per node (`/media/<id>/*` → that node). A push
   dashcam publishes to whichever node its `RSERVICE` host points at; the
   platform records that node automatically (`devices.media_node_id`). JT1078
   streams are spread across nodes automatically.

See `infra/.env.example` for the exact `ZLM_NODES` shape.

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
  docker compose -f infra/docker-compose.yml up -d device-server

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
