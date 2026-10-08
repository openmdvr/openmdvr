# OpenMDVR and GPS-first fleet platforms

Most fleet software, commercial or open source, grew out of the same idea: a
server that decodes location reports from as many tracker brands as
possible. Many commercial and white-label products are that same
open-source GPS engine with a new interface and logo. That model is good at
one thing, wide tracker coverage, and it shapes everything else the
platform can do.

OpenMDVR was designed for a different job: fleet video as security
evidence, with GPS, alarms and remote control in the same system. This page
explains where the two approaches differ.

## Summary

| | GPS-first platforms | OpenMDVR |
|---|---|---|
| Starting point | Location reports from trackers | Video, evidence and isolation, with GPS in the same core |
| Live video | Added later, when present; often HLS with seconds of delay and no audio | WebRTC with low latency and audio, front and cabin cameras |
| Camera families | Usually one streaming model | Pull (JT/T 1078 MDVRs) and push (RTMP dashcams) behind one interface |
| Evidence | Video and alarms live side by side | Clips retrieved automatically per alarm, matched by device timestamp, quarantined on mismatch |
| Stream access | Protected by the application session | One-time tickets checked by the media server before the first byte |
| Tenant isolation | Users, groups and permissions in application code | PostgreSQL Row Level Security, forced on every business table |
| Adding a new device | New decoder; features built around GPS | New decoder plus contracts that bring geofences, alarms, video, metering and quotas with them |
| Bandwidth cost | Rarely controlled | Byte-accurate metering and server-enforced quotas per tenant |
| GPS quality | Static filters | Adaptive per-device drift model, original reading always kept |

## Video first, not video added

When video is bolted onto a GPS server, it inherits a GPS server's
assumptions: one stream format, a player embedded next to the map, and
access control that ends at the web session.

In OpenMDVR, video is a subsystem with its own contract,
`videobridge.Protocol`. Two device families implement it today:

- **JT/T 1078 MDVRs.** The platform asks the device to stream, and a Go
  bridge translates JT1078 packets into RTP for the media server.
- **RTMP dashcams** such as the Jimi JC261/JC400. The device pushes its
  stream, and every publish is authorized in the media server's
  `on_publish` hook.

Playback runs over WebRTC, so live video arrives in well under a second.
Camera audio is transcoded to Opus only while someone is watching, so an
idle camera costs no CPU. Before anyone opens a stream, a native photo from
the camera gives a preview at a fraction of the data.

## Evidence you can trust

An alarm with the wrong video is worse than an alarm with none. When a
camera reports an event, OpenMDVR requests the recorded clip automatically,
checks the timestamp embedded in the file name the device itself reported,
and attaches it only if it belongs to that alarm. A file that does not
match is stored apart and never shown as evidence for the wrong incident.

## Security below the application

GPS-first platforms typically enforce who sees what in application code.
That works until one query forgets a filter.

- **Row Level Security in PostgreSQL.** Every business table has a tenant id
  and a forced policy. A buggy query returns nothing from another tenant.
- **Time-series data** is reachable only through security-barrier views and
  audited write functions, never through direct grants on the hypertables.
- **One-time video tickets** bound to tenant, device, channel and stream
  namespace, validated inside the media server.
- **Real-time session revocation.** Disabling a user or suspending a tenant
  ends their access on the next request, including open live streams.
- **Hardened device ingestion.** Panic recovery per connection, bounded
  buffers, connection caps and bounds on device-reported timestamps.

See [security-model.md](security-model.md).

## A core built to be extended

A new protocol in OpenMDVR does not get "GPS support". It gets the whole
platform:

| Contract | What a new protocol gets for free |
|---|---|
| `insert_gps_position()` | GPS quality filter, geofences, speed limits, live map push, reports, retention |
| `videobridge.Protocol` | tickets, playback authorization, metering, quotas, snapshots, idle shutdown |
| `commands.Sender` | audited remote commands with strict reply correlation |
| `insert_alarm()` | in-app notifications and HMAC-signed webhooks |

## Scale

Every device keeps a persistent TCP connection. In our load test, a device
server limited to 2 vCPU and 4 GB held 50,000 simulated trackers and stored
every position they sent, with zero failed logins. The connection limit per
listener is a configuration value, not a design limit. In the browser, maps
render with WebGL vector tiles and clustered markers, and live positions
reach every dashboard through one database listener per API process.

Method and full results are in [scalability.md](scalability.md).

## Cost control for operators

Video over cellular is the most expensive thing a fleet platform does.
OpenMDVR meters live video from the bytes actually delivered, enforces
per-session and monthly quotas per tenant on the server, and shuts streams
down when nobody is watching.

## When a GPS-first platform is enough

If you only need location tracking for a mixed fleet of trackers and video
does not matter, a GPS-first server does that job. If you need video you
can rely on as evidence, isolation you can audit in the database, and an
architecture where new camera protocols plug in without touching the rest
of the system, that is what OpenMDVR is built for.
