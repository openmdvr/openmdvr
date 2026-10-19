# Using a phone as a fake camera

Without MDVR hardware you can still exercise the full video pipeline
(JT808 signaling → 0x9101 → JT1078 → bridge → ZLMediaKit → dashboard) with
live video from a phone instead of a static `.h264` file. The phone pushes
RTMP to ZLMediaKit, and `simulate_video_live.py` re-sends that stream as a
simulated JT808/JT1078 device — the same path a real MDVR takes.

Requirements: Android platform-tools (`adb`), ffmpeg on `PATH`, Python 3.9+,
and any RTMP broadcasting app on the phone (for example Larix Broadcaster,
available for Android and iOS).

## 1. Connect the phone with `adb reverse`

`adb reverse` makes the phone's `127.0.0.1:1935` reach port 1935 on your
machine, so no port has to be exposed on your network.

**Wireless debugging (recommended; USB connections can be flaky):** enable
Developer options → Wireless debugging → "Pair device with pairing code".
The pairing screen shows a pairing `ip:port` and a 6-digit code; the main
wireless-debugging screen shows a different connection `ip:port`.

```sh
adb pair <ip>:<pairing-port>          # enter the code when prompted
adb connect <ip>:<connection-port>
adb devices                           # should list the phone as "device"
adb -s <ip>:<connection-port> reverse tcp:1935 tcp:1935
```

**USB:** enable USB debugging, connect the cable and accept the
"Allow USB debugging?" prompt on the unlocked phone:

```sh
adb devices                           # must say "device", not "unauthorized"
adb reverse tcp:1935 tcp:1935
```

If `adb devices` lists more than one device, pass `-s <serial>` before
`reverse`. If the phone goes `offline`, `adb kill-server && adb start-server`
usually recovers it; otherwise switch to wireless debugging.

Note: Windows shells do not understand `\` line continuations — keep each
command on one line.

## 2. Broadcast from the phone

Create a new RTMP connection in the app:

- **URL:** `rtmp://127.0.0.1/live`
- **Stream key:** any name, e.g. `phone`

Start broadcasting. Optionally confirm that ZLMediaKit received it:

```sh
curl "http://127.0.0.1/index/api/getMediaList?secret=<ZLM_API_SECRET>&stream=phone"
```

Lower the app's video resolution/bitrate to dashcam-like values (around
640p / 700 kbps). The Python simulator is single-threaded and synchronous; at
the app's default 1080p/multi-Mbps settings it lags. This is a limit of the
test script, not of the Go bridge.

## 3. Run the simulated device

```sh
python simulate_video_live.py --terminal 13800000001 --rtmp-source rtmp://127.0.0.1:1935/live/phone --zlm-secret <ZLM_API_SECRET>
```

`<ZLM_API_SECRET>` comes from `infra/.env`. The terminal ID must belong to a
device already provisioned through the dashboard or API (same requirement as
`simulate_video.py`). The script performs JT808 registration, requests video
over HTTP and converts every frame it reads from RTMP into real JT1078
packets (including fragmentation of large frames). In the dashboard the
device looks exactly like a real MDVR streaming. Stop with Ctrl+C.

## Known limitations

- **No audio.** The script reads video only (`ffmpeg -an`), and the JT1078
  bridge relays video frames only (`internal/jt1078bridge/relay.go`).
- **Not a substitute for pushing RTMP directly.** Pushing RTMP straight to
  ZLMediaKit (`rtmp://127.0.0.1:1935/live/<name>`) is simpler but only tests
  ZLMediaKit, not the JT1078 bridge. Use it to check the media server; use
  `simulate_video_live.py` to test the product pipeline.
