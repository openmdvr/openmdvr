"""Simulates a JC261/JC400-style dashcam answering a native photo request: it
authenticates over GT06, waits for the server's "Picture,out#"/"Picture,in#"
command, replies "PICTURE" (protocol 0x21, like the real device) and uploads a
JPEG via HTTP multipart to /upload/<imei> (the same endpoint used for clips).

Usage:
    python simulate_gt06_photo.py --imei 999000111222555 [--host 127.0.0.1 --port 5023 --upload-port 8083]

While it runs, request the preview photo for that device from the dashboard
or with POST /devices/{id}/snapshot.
"""

import argparse
import socket
import time
import urllib.request
import uuid

from simulate_gt06 import build_frame, imei_to_payload, parse_frame, recv_frame

# Minimal valid 1x1 JPEG (enough: the server recognizes it by the FF D8 FF
# magic bytes and returns it as-is).
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9"
)


def upload_photo(host, port, imei, name, data):
    boundary = uuid.uuid4().hex
    parts = []
    for field, value in (("filename", name), ("timestamp", str(int(time.time() * 1000))), ("sign", "sim")):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"\r\n\r\n{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\nContent-Type: image/jpeg\r\n\r\n".encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"http://{host}:{port}/upload/{imei}",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--imei", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5023)
    ap.add_argument("--upload-port", type=int, default=8083)
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--busy-first", action="store_true", help='reply "busy" to the first Picture command (as a real device does when busy)')
    ap.add_argument("--front-delay", type=float, default=0, help="extra seconds before uploading the front photo (real devices sometimes take 30 s+)")
    args = ap.parse_args()
    busy_pending = args.busy_first

    sock = socket.create_connection((args.host, args.port), timeout=5.0)
    serial = 1
    sock.sendall(build_frame(0x01, imei_to_payload(args.imei), serial))
    proto, _ = parse_frame(recv_frame(sock))
    assert proto == 0x01, f"expected login ACK, got 0x{proto:02x}"
    print("login OK, waiting for Picture commands...", flush=True)

    deadline = time.time() + args.seconds
    while time.time() < deadline:
        try:
            frame = recv_frame(sock, timeout=2.0)
        except Exception:
            continue
        proto, _ = parse_frame(frame)
        if proto != 0x80:
            continue
        text = frame.decode("ascii", "ignore")
        print(f"command received: {text!r}", flush=True)
        if "Picture" not in text:
            continue
        serial += 1
        if busy_pending:
            busy_pending = False
            sock.sendall(build_frame(0x21, b"\x00\x00\x00\x00\x01" + b"busy", serial))
            print("replied busy", flush=True)
            continue
        sock.sendall(build_frame(0x21, b"\x00\x00\x00\x00\x01" + b"PICTURE:OK!", serial))
        # File names as produced by real hardware:
        # CMD_<imei>_<code>_<date>_<I|F>_<n>.jpg -- the cabin photo arrives first.
        sides = ["I", "F"] if ",inout" in text else (["F"] if ",out" in text else ["I"])
        time.sleep(1.2)  # the device takes the photo and uploads it
        for n, side in enumerate(sides, start=10):
            if side == "F" and args.front_delay:
                time.sleep(args.front_delay)
            name = f"CMD_{args.imei}_00000000_{time.strftime('%Y_%m_%d_%H_%M_%S')}_{side}_{n}.jpg"
            status = upload_photo(args.host, args.upload_port, args.imei, name, TINY_JPEG)
            print(f"photo {name} uploaded (HTTP {status})", flush=True)
    sock.close()


if __name__ == "__main__":
    main()
