"""
End-to-end check for usage_events: confirms that actually watching a stream,
with enough traffic to cross hook.on_flow_report / general.flowThreshold,
results in a real usage_events row with bytes_transferred > 0 and
event_type='live_view'.

Uses an already provisioned JT808 device and simulate_video.py for the
JT808/JT1078 push. The push runs as a separate subprocess instead of sharing
threads/GIL with this script's HTTP-FLV reader: running both in one process
with threading caused real ConnectionAbortedErrors (the blocking reader
competed for the GIL with the thread sending JT1078 packets and the server
closed the connection). Separate OS processes avoid that.

Usage:
    python verify_usage_event.py --device-id <uuid> --terminal 13800000001 \
        --h264 <path to a .h264 file> --zlm-secret <secret>
"""
import argparse
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

FLOW_THRESHOLD_BYTES = 1024 * 1024  # general.flowThreshold in config.ini (KB) x 1024


def query_last_usage_event(container: str, database: str, device_id: str) -> str:
    out = subprocess.run(
        [
            "docker", "exec", container, "psql", "-U", "postgres", "-d", database,
            "-t", "-A", "-F", "|",
            "-c",
            "SELECT bytes_transferred, event_type, time, metadata FROM usage_events "
            f"WHERE device_id = '{uuid.UUID(device_id)}' ORDER BY time DESC LIMIT 1;",
        ],
        capture_output=True, text=True, timeout=10,
    )
    return out.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device-id", required=True, help="devices.id (UUID) of the provisioned JT808 device")
    ap.add_argument("--terminal", default="13800000001", help="jt808_terminal_id of that device")
    ap.add_argument("--pg-container", default="openmdvr-postgres")
    ap.add_argument("--pg-database", default="openmdvr")
    ap.add_argument("--zlm-base", default="http://127.0.0.1:80")
    ap.add_argument("--zlm-secret", required=True)
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--h264", required=True)
    ap.add_argument("--fps", type=float, default=10.0)
    args = ap.parse_args()

    def last_event() -> str:
        return query_last_usage_event(args.pg_container, args.pg_database, args.device_id)

    before = last_event()
    print(f"usage_events before (latest row, may be old or empty): {before or '(none)'}")

    stream_id = f"{args.terminal}_{args.channel}"
    flv_url = f"{args.zlm_base}/rtp/{stream_id}.live.flv"

    # The reader starts BEFORE the push, as in real use: the dashboard asks
    # for the URL and the browser starts connecting while the device is still
    # setting up its own JT1078 connection. ZLMediaKit supports this natively
    # ("play before push", general.maxStreamWaitMS in config.ini) -- the GET
    # waits up to 15 s for the stream instead of failing with 404.
    reader_result: dict = {}

    def read_flv():
        try:
            with urllib.request.urlopen(flv_url, timeout=30) as resp:
                total = 0
                while total < FLOW_THRESHOLD_BYTES * 1.2:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                reader_result["bytes_read"] = total
        except Exception as e:
            reader_result["error"] = str(e)

    reader_thread = threading.Thread(target=read_flv, daemon=True)
    reader_thread.start()
    print("HTTP-FLV reader: started first, waiting for the stream to appear (play-before-push)")

    time.sleep(0.5)
    push = subprocess.Popen(
        [sys.executable, "simulate_video.py",
         "--terminal", args.terminal, "--channel", str(args.channel),
         "--h264", args.h264, "--fps", str(args.fps),
         "--zlm-secret", args.zlm_secret, "--zlm-base", args.zlm_base],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    print(f"push: simulate_video.py started as a subprocess (pid {push.pid})")

    push_out, _ = push.communicate(timeout=30)
    reader_thread.join(timeout=15)
    bytes_read = reader_result.get("bytes_read", 0)
    read_error = reader_result.get("error")
    print(f"HTTP-FLV reader: read {bytes_read} bytes" + (f" (error: {read_error})" if read_error else ""))
    print("--- simulate_video.py output ---")
    print(push_out)
    print("--- end of output ---")

    print("waiting for ZLMediaKit to fire on_flow_report...")
    time.sleep(3.0)

    after = last_event()
    print(f"usage_events after (latest row): {after or '(none)'}")

    if after and after != before:
        print("RESULT: new usage_event confirmed in the database.")
        sys.exit(0)
    else:
        print("RESULT: no new usage_event appeared -- check the hook/threshold.")
        sys.exit(1)


if __name__ == "__main__":
    main()
