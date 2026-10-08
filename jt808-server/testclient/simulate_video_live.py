"""
Like simulate_video.py, but the video source is a LIVE stream (for example a
phone running an RTMP broadcasting app) instead of a static .h264 file. This
exercises the real pipeline (JT808 registration -> 0x9101 -> JT1078
connection -> reassembly -> RTP -> ZLMediaKit) with a continuous real video
source and no physical camera.

Unlike pushing RTMP directly to ZLMediaKit (simpler, but it only tests half
of the pipeline), this exercises jt808-server/internal/jt1078bridge end to
end.

Requires ffmpeg on PATH. The phone must be pushing RTMP to ZLMediaKit
(rtmp://127.0.0.1:1935/<app>/<stream>) -- see README_phone.md for how to
connect a phone without opening any port (adb reverse). This script reads that
RTMP stream with ffmpeg (codec copy, no re-encoding) and forwards each access
unit through a simulated JT808/JT1078 device, just as a real MDVR would read
its own camera.

Usage:
    python simulate_video_live.py --terminal 13800000001 \
        --rtmp-source rtmp://127.0.0.1:1935/live/phone \
        --zlm-secret ...

Stop with Ctrl+C.
"""
import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request

import simulate_video as sv


def extract_complete_access_units(buf: bytearray) -> list[bytes]:
    """Like sv.extract_access_units but over a buffer that keeps growing --
    it never consumes the last detected NALU (more data may still arrive for
    it), only the ones confirmed complete because a following NALU already
    started. Modifies buf in place, leaving only the unconfirmed tail."""
    data = bytes(buf)
    starts = sv._find_nalu_starts(data)
    if len(starts) < 2:
        return []

    units: list[bytes] = []
    unit_start = None
    consumed_until = 0
    boundaries = starts + [len(data)]
    for i in range(len(starts) - 1):  # the last start is never processed here
        nalu_start = starts[i]
        nalu_end = boundaries[i + 1]
        nal_type = data[nalu_start] & 0x1F
        prev_start_code_start = nalu_start - (3 if data[nalu_start - 3 : nalu_start] == b"\x00\x00\x01" else 4)
        if unit_start is None:
            unit_start = prev_start_code_start
        if nal_type in (1, 5):  # non-IDR or IDR slice: closes the access unit
            units.append(data[unit_start:nalu_end])
            consumed_until = nalu_end
            unit_start = None

    buf[:consumed_until] = b""
    return units


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jt808-host", default="127.0.0.1")
    ap.add_argument("--jt808-port", type=int, default=8808)
    ap.add_argument("--jt1078-host", default="127.0.0.1")
    ap.add_argument("--jt1078-port", type=int, default=8081)
    ap.add_argument("--http-base", default="http://127.0.0.1:8082")
    ap.add_argument("--zlm-secret", required=True)
    ap.add_argument("--terminal", required=True)
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--rtmp-source", required=True, help="rtmp://127.0.0.1:1935/<app>/<stream> the phone is pushing to")
    ap.add_argument("--ffmpeg-bin", default="ffmpeg")
    args = ap.parse_args()

    phone_bcd = sv.bcd_encode(args.terminal, 6)
    sock = sv.socket.create_connection((args.jt808_host, args.jt808_port), timeout=5.0)
    print(f"jt808: connected to {args.jt808_host}:{args.jt808_port}")

    reg_body = (
        sv.struct.pack(">HH", 0, 0)
        + b"SIMUL".ljust(5, b"\x00")
        + b"SIMULATOR-LIVE-PHONE".ljust(20, b"\x00")
        + b"SIM0003"
        + bytes([1])
        + "SIM-LIV".encode("gbk")
    )
    sock.sendall(sv.build_jt808_frame(0x0100, phone_bcd, 1, reg_body))
    msg_id, body = sv.parse_jt808_reply(sv.recv_jt808_frame(sock))
    assert msg_id == 0x8100 and body[2] == 0, "registration rejected"
    sock.sendall(sv.build_jt808_frame(0x0102, phone_bcd, 2, body[3:]))
    msg_id, _ = sv.parse_jt808_reply(sv.recv_jt808_frame(sock))
    assert msg_id == 0x8001
    print("jt808: registration + auth OK")

    stop = threading.Event()

    def keepalive():
        serial = 3
        while not stop.is_set():
            time.sleep(2)
            if stop.is_set():
                return
            try:
                sock.sendall(sv.build_jt808_frame(0x0002, phone_bcd, serial, b""))
                sv.parse_jt808_reply(sv.recv_jt808_frame(sock, timeout=3.0))
                serial += 1
            except Exception:
                return

    threading.Thread(target=keepalive, daemon=True).start()

    req = json.dumps({"terminalId": args.terminal, "channel": args.channel}).encode()
    http_req = urllib.request.Request(
        f"{args.http_base}/api/v1/9101", data=req, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(http_req, timeout=5) as resp:
        result = json.loads(resp.read())
    print(f"http 9101 -> {result}")
    if result.get("code") != 0:
        print("could not start video, aborting")
        stop.set()
        return

    time.sleep(0.3)
    video_sock = sv.socket.create_connection((args.jt1078_host, args.jt1078_port), timeout=5.0)
    print(f"video: connected to {args.jt1078_host}:{args.jt1078_port}")

    # -c:v copy: the phone already encodes H.264, no re-encoding needed.
    # -bsf:v h264_mp4toannexb: RTMP carries H.264 in AVCC format (length +
    # NALU), but JT1078 needs Annex-B (00 00 01 start codes).
    ffmpeg = subprocess.Popen(
        [args.ffmpeg_bin, "-i", args.rtmp_source, "-an", "-c:v", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "h264", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    print(f"ffmpeg: reading {args.rtmp_source} (Ctrl+C to stop)")

    buf = bytearray()
    total_units = 0
    try:
        while True:
            chunk = ffmpeg.stdout.read(65536)
            if not chunk:
                print("\nffmpeg: the source ended (did the phone stop broadcasting?)")
                break
            buf.extend(chunk)
            units = extract_complete_access_units(buf)
            for au in units:
                ts = int(time.time() * 1000)
                sv.fragment_and_send(video_sock, phone_bcd, args.channel, au, timestamp_ms=ts)
                total_units += 1
            if units:
                print(f"video: {total_units} frames sent so far", end="\r")
    except KeyboardInterrupt:
        print("\ninterrupted, shutting down cleanly...")
    finally:
        stop.set()
        ffmpeg.terminate()
        video_sock.close()
        sock.close()
        print("done.")


if __name__ == "__main__":
    main()
