"""
End-to-end test of the video bridge without real hardware:

1. Opens a JT808 signaling connection (registration + auth) and keeps it alive.
2. Requests video via the bridge HTTP endpoint (POST /api/v1/9101).
3. Opens a SEPARATE JT1078 video connection and sends real H.264 frames
   (generated with ffmpeg, not fake bytes), fragmented into JT1078 packets
   the way real hardware does when a frame exceeds 950 bytes per packet --
   this exercises the reassembler, not only the atomic path.
4. Queries the ZLMediaKit API (getMediaList) to confirm the stream was
   published.

It does not test "does the video look right in a browser?" -- that needs a
player. It tests that the full pipeline (JT808 signaling -> 0x9101 -> JT1078
connection -> reassembly -> RTP -> ZLMediaKit) delivers real H.264 bytes in a
structurally correct way.

Usage:
    python simulate_video.py --terminal 13800000001 --h264 <path to a .h264 file> --zlm-secret <secret>
"""
import argparse
import json
import socket
import struct
import threading
import time
import urllib.request

FRAME = 0x7E


# --- reused from simulate.py (JT808 framing) ---

def bcd_encode(digits: str, nbytes: int) -> bytes:
    digits = digits.zfill(nbytes * 2)
    return bytes(int(digits[i : i + 2], 16) for i in range(0, len(digits), 2))


def checksum(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
    return c


def escape(data: bytes) -> bytes:
    out = bytearray([FRAME])
    for b in data:
        if b == FRAME:
            out += bytes([0x7D, 0x02])
        elif b == 0x7D:
            out += bytes([0x7D, 0x01])
        else:
            out.append(b)
    out.append(FRAME)
    return bytes(out)


def build_jt808_frame(msg_id: int, phone_bcd: bytes, serial: int, body: bytes) -> bytes:
    prop = len(body) & 0x3FF
    header = struct.pack(">HH", msg_id, prop) + phone_bcd + struct.pack(">H", serial)
    payload = header + body
    payload += bytes([checksum(payload)])
    return escape(payload)


def recv_jt808_frame(sock: socket.socket, timeout=5.0) -> bytes:
    sock.settimeout(timeout)
    buf = bytearray()
    started = False
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("connection closed while waiting for a reply")
        if b[0] == FRAME:
            if not started:
                started = True
                buf += b
                continue
            buf += b
            return bytes(buf)
        if started:
            buf += b


def parse_jt808_reply(frame: bytes):
    unescaped = bytearray()
    i = 1
    while i < len(frame) - 1:
        if frame[i] == 0x7D and i + 1 < len(frame) - 1:
            if frame[i + 1] == 0x02:
                unescaped.append(0x7E)
                i += 2
                continue
            if frame[i + 1] == 0x01:
                unescaped.append(0x7D)
                i += 2
                continue
        unescaped.append(frame[i])
        i += 1
    msg_id, prop = struct.unpack(">HH", bytes(unescaped[0:4]))
    body_len = prop & 0x3FF
    body = bytes(unescaped[12 : 12 + body_len])
    return msg_id, body


# --- JT1078 (video) ---

MAX_JT1078_BODY = 950


def build_jt1078_packet(sim_bcd: bytes, seq: int, channel: int, data_type: int,
                         subcontract: int, timestamp_ms: int, body: bytes) -> bytes:
    pkt = bytearray()
    pkt += b"01cd"  # fixed ID, literally the ASCII characters "01cd"
    v, p, x, cc = 2, 0, 0, 1
    attr = (v & 0b11) << 6 | (p & 1) << 5 | (x & 1) << 4 | (cc & 0b1111)
    pkt.append(attr)
    m = 1 if subcontract in (0, 2) else 0  # atomic or last fragment = end of frame
    pt = 98  # H264
    sign = (m & 1) << 7 | (pt & 0x7F)
    pkt.append(sign)
    pkt += struct.pack(">H", seq)
    pkt += sim_bcd
    pkt.append(channel)
    pkt.append(((data_type & 0x0F) << 4) | (subcontract & 0x0F))
    # any data_type other than "transparent" (4) carries an 8-byte timestamp
    pkt += struct.pack(">Q", timestamp_ms)
    # I/P/B frames carry the two interval fields (2+2 bytes)
    if data_type in (0, 1, 2):
        pkt += struct.pack(">HH", 0, 0)
    pkt += struct.pack(">H", len(body))
    pkt += body
    return bytes(pkt)


def fragment_and_send(sock: socket.socket, sim_bcd: bytes, channel: int, frame_data: bytes, timestamp_ms: int):
    chunks = [frame_data[i : i + MAX_JT1078_BODY] for i in range(0, len(frame_data), MAX_JT1078_BODY)]
    seq = 1
    for i, chunk in enumerate(chunks):
        if len(chunks) == 1:
            subcontract = 0  # atomic
        elif i == 0:
            subcontract = 1  # first
        elif i == len(chunks) - 1:
            subcontract = 2  # last
        else:
            subcontract = 3  # middle
        pkt = build_jt1078_packet(sim_bcd, seq, channel, data_type=0, subcontract=subcontract,
                                   timestamp_ms=timestamp_ms, body=chunk)
        sock.sendall(pkt)
        seq += 1
    print(f"video: sent 1 frame in {len(chunks)} JT1078 packet(s) ({len(frame_data)} bytes total)")


def _find_nalu_starts(data: bytes):
    starts = []
    i = 0
    n = len(data)
    while i < n - 3:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            starts.append(i + 3)
            i += 3
        elif i < n - 4 and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 0 and data[i + 3] == 1:
            starts.append(i + 4)
            i += 4
        else:
            i += 1
    return starts


def extract_access_units(h264_path: str):
    """Split a real Annex-B H.264 file into access units (frames): each group
    of NALUs (optional SPS/PPS/SEI + 1 slice) from the preceding start code to
    the end of the slice NALU (type 1 or 5), as an MDVR encoder would emit
    them frame by frame."""
    data = open(h264_path, "rb").read()
    starts = _find_nalu_starts(data)
    boundaries = starts + [len(data)]

    units = []
    unit_start = None
    for i in range(len(starts)):
        nalu_start = starts[i]
        nalu_end = boundaries[i + 1]
        if len(data) <= nalu_start:
            continue
        nal_type = data[nalu_start] & 0x1F
        prev_start_code_start = nalu_start - (3 if data[nalu_start - 3 : nalu_start] == b"\x00\x00\x01" else 4)
        if unit_start is None:
            unit_start = prev_start_code_start
        if nal_type in (1, 5):  # non-IDR or IDR slice: closes the access unit
            units.append(data[unit_start:nalu_end])
            unit_start = None
    return units


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jt808-host", default="127.0.0.1")
    ap.add_argument("--jt808-port", type=int, default=8808)
    ap.add_argument("--jt1078-host", default="127.0.0.1")
    ap.add_argument("--jt1078-port", type=int, default=8081)
    ap.add_argument("--http-base", default="http://127.0.0.1:8082")
    ap.add_argument("--zlm-base", default="http://127.0.0.1:80")
    ap.add_argument("--zlm-secret", required=True)
    ap.add_argument("--terminal", required=True)
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--h264", required=True)
    ap.add_argument("--fps", type=float, default=10.0)
    args = ap.parse_args()

    phone_bcd = bcd_encode(args.terminal, 6)
    sock = socket.create_connection((args.jt808_host, args.jt808_port), timeout=5.0)
    print(f"jt808: connected to {args.jt808_host}:{args.jt808_port}")

    reg_body = (
        struct.pack(">HH", 0, 0)
        + b"SIMUL".ljust(5, b"\x00")
        + b"SIMULATOR-VIDEO-TEST".ljust(20, b"\x00")
        + b"SIM0002"
        + bytes([1])
        + "SIM-VID".encode("gbk")
    )
    sock.sendall(build_jt808_frame(0x0100, phone_bcd, 1, reg_body))
    msg_id, body = parse_jt808_reply(recv_jt808_frame(sock))
    assert msg_id == 0x8100
    result = body[2]
    print(f"jt808: registration -> result={result}")
    if result != 0:
        print("registration rejected, aborting")
        return

    sock.sendall(build_jt808_frame(0x0102, phone_bcd, 2, body[3:]))
    msg_id, resp_body = parse_jt808_reply(recv_jt808_frame(sock))
    assert msg_id == 0x8001
    print(f"jt808: auth -> result={resp_body[4]}")

    # Keep the JT808 connection alive in the background for the rest of the
    # test (the server's session registry only exists while the socket is
    # open).
    stop = threading.Event()

    def keepalive():
        serial = 3
        while not stop.is_set():
            time.sleep(2)
            if stop.is_set():
                return
            try:
                sock.sendall(build_jt808_frame(0x0002, phone_bcd, serial, b""))
                parse_jt808_reply(recv_jt808_frame(sock, timeout=3.0))
                serial += 1
            except Exception:
                return

    threading.Thread(target=keepalive, daemon=True).start()

    # --- request video ---
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
    stream_id = f"{args.terminal}_{args.channel}"

    # --- connect the JT1078 "video" link and stream the real frames ---
    access_units = extract_access_units(args.h264)
    print(f"video: {len(access_units)} access units (frames) extracted from {args.h264}")

    time.sleep(0.3)  # give the 0x9101 time to arrive and the server time to be ready
    video_sock = socket.create_connection((args.jt1078_host, args.jt1078_port), timeout=5.0)
    print(f"video: connected to {args.jt1078_host}:{args.jt1078_port}")

    start_ms = int(time.time() * 1000)
    frame_interval_s = 1.0 / args.fps
    checked_mid_stream = False
    for i, au in enumerate(access_units):
        ts = start_ms + int(i * frame_interval_s * 1000)
        fragment_and_send(video_sock, phone_bcd, args.channel, au, timestamp_ms=ts)
        time.sleep(frame_interval_s)
        if not checked_mid_stream and i == min(15, len(access_units) - 1):
            checked_mid_stream = True
            media = query_media_list(args.zlm_base, args.zlm_secret, stream_id)
            print(f"zlm getMediaList (mid-stream) -> {json.dumps(media, ensure_ascii=False)}")

    print("video: all frames sent, video connection stays open a few more seconds")

    # Finding the stream can take a few seconds (ZLMediaKit needs SPS+PPS and
    # enough frames to confirm the track) -- poll instead of assuming a fixed
    # delay.
    found = False
    for _ in range(10):
        media = query_media_list(args.zlm_base, args.zlm_secret, stream_id)
        if media.get("data"):
            found = True
            print(f"zlm getMediaList -> {json.dumps(media, ensure_ascii=False)}")
            break
        time.sleep(0.5)

    if found:
        flv_url = f"{args.zlm_base}/rtp/{stream_id}.live.flv"
        try:
            with urllib.request.urlopen(flv_url, timeout=5) as resp:
                chunk = resp.read(4096)
            print(f"HTTP-FLV {flv_url} -> {len(chunk)} bytes received, first bytes: {chunk[:3]!r} (should be b'FLV')")
            if chunk[:3] == b"FLV":
                print("RESULT: STREAM PUBLISHED AND PLAYABLE OVER HTTP-FLV")
            else:
                print("RESULT: stream published but the HTTP-FLV response does not look valid")
        except Exception as e:
            print(f"RESULT: stream published but HTTP-FLV could not be read: {e}")
    else:
        print("RESULT: stream not found in ZLMediaKit after waiting")

    video_sock.close()
    stop.set()
    sock.close()


def query_media_list(zlm_base: str, secret: str, stream_id: str) -> dict:
    check_url = f"{zlm_base}/index/api/getMediaList?secret={secret}&stream={stream_id}"
    with urllib.request.urlopen(check_url, timeout=5) as resp:
        return json.loads(resp.read())


if __name__ == "__main__":
    main()
