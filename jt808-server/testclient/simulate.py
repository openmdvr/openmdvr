"""
Minimal JT/T808-2013 terminal simulator for end-to-end testing of jt808-server
without real hardware. It deliberately does NOT share code with the Go server:
building messages with the same library would only prove that the library
decodes itself, not that the server interoperates with an independent
implementation -- which is exactly what happens with real devices from
different manufacturers.

Usage:
    python simulate.py --host 127.0.0.1 --port 8808 --terminal 013800000001

The terminal_id must already exist in the `devices` table (jt808_terminal_id)
with status='active'. Provisioning devices is a platform action, not something
this script can do.
"""
import argparse
import socket
import struct
import time

FRAME = 0x7E


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


def build_frame(msg_id: int, phone_bcd: bytes, serial: int, body: bytes) -> bytes:
    # Message properties: bits 0-9 = body length, the rest zero
    # (2013 protocol, no encryption, no sub-packages).
    prop = len(body) & 0x3FF
    header = struct.pack(">HH", msg_id, prop) + phone_bcd + struct.pack(">H", serial)
    payload = header + body
    payload += bytes([checksum(payload)])
    return escape(payload)


def recv_frame(sock: socket.socket, timeout=5.0) -> bytes:
    sock.settimeout(timeout)
    buf = bytearray()
    started = False
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("connection closed by the server while waiting for a reply")
        if b[0] == FRAME:
            if not started:
                started = True
                buf += b
                continue
            buf += b
            return bytes(buf)
        if started:
            buf += b


def parse_reply(frame: bytes):
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
    phone = unescaped[4:10]
    serial = struct.unpack(">H", bytes(unescaped[10:12]))[0]
    body = bytes(unescaped[12 : 12 + body_len])
    return msg_id, serial, phone, body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8808)
    ap.add_argument("--terminal", required=True, help="jt808_terminal_id, up to 12 digits")
    ap.add_argument("--lat", type=float, default=32.5027)
    ap.add_argument("--lon", type=float, default=-117.0037)
    args = ap.parse_args()

    phone_bcd = bcd_encode(args.terminal, 6)
    serial = 0

    sock = socket.create_connection((args.host, args.port), timeout=5.0)
    print(f"connected to {args.host}:{args.port}")

    # --- 0x0100 registration ---
    reg_body = (
        struct.pack(">HH", 0, 0)  # province, city
        + b"SIMUL".ljust(5, b"\x00")  # manufacturer (5 bytes in 2013)
        + b"SIMULATOR-TESTCLIENT".ljust(20, b"\x00")  # model (20 bytes)
        + b"SIM0001"  # terminal id (7 bytes)
        + bytes([1])  # plate color: blue
        + "SIM-TEST".encode("gbk")
    )
    serial += 1
    frame = build_frame(0x0100, phone_bcd, serial, reg_body)
    sock.sendall(frame)
    msg_id, _, _, body = parse_reply(recv_frame(sock))
    assert msg_id == 0x8100, f"expected 0x8100, got 0x{msg_id:04x}"
    result = body[2]
    print(f"registration -> result={result} (0=success) auth_code={body[3:]!r}")
    if result != 0:
        print("registration rejected, aborting (is the device provisioned and active in the DB?)")
        return

    # --- 0x0102 authentication (exercise this path too, not only 0x0100) ---
    serial += 1
    frame = build_frame(0x0102, phone_bcd, serial, body[3:])
    sock.sendall(frame)
    msg_id, _, _, resp_body = parse_reply(recv_frame(sock))
    assert msg_id == 0x8001
    print(f"auth (0x0102) -> result={resp_body[4]} (0=success)")

    # --- 0x0002 heartbeat ---
    serial += 1
    frame = build_frame(0x0002, phone_bcd, serial, b"")
    sock.sendall(frame)
    msg_id, _, _, resp_body = parse_reply(recv_frame(sock))
    assert msg_id == 0x8001
    print(f"heartbeat -> result={resp_body[4]} (0=success)")

    # --- 0x0200 location report with emergency alarm (bit0) ---
    now = time.gmtime(time.time() + 8 * 3600)  # JT808 uses GMT+8
    date_bcd = bcd_encode(time.strftime("%y%m%d%H%M%S", now), 6)
    alarm_sign = 0x00000001  # bit0 = EmergencyAlarm
    status_sign = 0x00000002  # bit1 = positioned (fix); north+east (bits 2/3 clear)
    lat_scaled = int(round(abs(args.lat) * 1_000_000))
    lon_scaled = int(round(abs(args.lon) * 1_000_000))
    if args.lat < 0:
        status_sign |= 1 << 2  # south
    if args.lon < 0:
        status_sign |= 1 << 3  # west
    loc_body = struct.pack(
        ">IIIIHHH", alarm_sign, status_sign, lat_scaled, lon_scaled, 0, 0, 0
    ) + date_bcd
    serial += 1
    frame = build_frame(0x0200, phone_bcd, serial, loc_body)
    sock.sendall(frame)
    msg_id, _, _, resp_body = parse_reply(recv_frame(sock))
    assert msg_id == 0x8001
    print(f"location+alarm -> result={resp_body[4]} (0=success)")

    sock.close()
    print("simulation complete")


if __name__ == "__main__":
    main()
