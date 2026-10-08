"""
Minimal GT06 tracker simulator (GT06-compatible variant, byte layout checked
against the public protocol specification) for end-to-end testing of
internal/gt06server without real hardware. It is deliberately INDEPENDENT of
the Go server -- same rationale as testclient/simulate.py (JT808): reusing the
same code to both build AND decode messages would only prove that the code
understands itself, not that it interoperates with a different implementer
(which is exactly what happens with real hardware).

Usage:
    python simulate_gt06.py --host 127.0.0.1 --port 5023 --imei 123456789012345

The IMEI must already exist in `devices.gt06_imei` with protocol='gt06' and
status='active'. Provisioning devices is a platform action, not something this
script can do.
"""
import argparse
import socket
import struct
import time


def crc16_x25(data: bytes) -> int:
    """CRC-16/X-25 -- independent implementation of
    jt808-server/internal/gt06server/checksum.go, verified separately against
    the standard check value from the CRC catalogue
    (crc16_x25(b"123456789") == 0x906E)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return (~crc) & 0xFFFF


assert crc16_x25(b"123456789") == 0x906E, "crc16_x25 does not match the standard CRC-16/X-25 check value"


def build_frame(protocol_number: int, payload: bytes, serial: int) -> bytes:
    body = bytes([protocol_number]) + payload + struct.pack(">H", serial)
    length_byte = len(body) + 2  # the CRC (2 bytes) counts toward the declared length
    crc_input = bytes([length_byte]) + body
    crc = crc16_x25(crc_input)
    return bytes([0x78, 0x78, length_byte]) + body + struct.pack(">H", crc) + b"\x0d\x0a"


def imei_to_payload(imei: str) -> bytes:
    """15-digit IMEI -> 8 bytes: prepend a '0' to make 16 hex chars and pack
    as BCD -- the exact inverse of parseIMEI in handlers.go (8-byte hex dump,
    first nibble discarded)."""
    assert len(imei) == 15 and imei.isdigit(), "IMEI must be 15 digits"
    padded = "0" + imei  # 16 hex chars
    return bytes(int(padded[i : i + 2], 16) for i in range(0, 16, 2))


def gps_block(lat: float, lon: float, speed_kmh: int, course: int, gps_fix: bool) -> bytes:
    now = time.gmtime()
    date_time = bytes([now.tm_year - 2000, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min, now.tm_sec])
    satellites = bytes([0xCC])  # GPS info length + satellite count

    lat_north = lat >= 0
    lon_east = lon >= 0
    lat_raw = round(abs(lat) * 60 * 30000)
    lon_raw = round(abs(lon) * 60 * 30000)

    flags = (course & 0x03FF)
    if gps_fix:
        flags |= 1 << 12
    if lat_north:
        flags |= 1 << 10
    if not lon_east:
        flags |= 1 << 11

    return (
        date_time
        + satellites
        + struct.pack(">I", lat_raw)
        + struct.pack(">I", lon_raw)
        + bytes([speed_kmh & 0xFF])
        + struct.pack(">H", flags)
    )


def lbs_block() -> bytes:
    """MCC/MNC/LAC/CellID -- intentionally ignored by the server; sample
    values taken from the protocol specification."""
    return bytes([0x01, 0xCC]) + bytes([0x00]) + bytes([0x26, 0x33]) + bytes([0x00, 0x0E, 0x7F])


def recv_frame(sock: socket.socket, timeout: float = 5.0) -> bytes:
    sock.settimeout(timeout)
    buf = bytearray()
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("connection closed by the server while waiting for a reply")
        buf += b
        if len(buf) >= 2 and buf[-2:] == b"\x0d\x0a" and len(buf) > 4:
            return bytes(buf)


def parse_frame(frame: bytes):
    length_field_size = 1 if frame[0] == 0x78 else 2
    body_start = 2 + length_field_size
    body = frame[body_start:-2]
    protocol_number = body[0]
    serial = struct.unpack(">H", body[-4:-2])[0]
    return protocol_number, serial


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5023)
    ap.add_argument("--imei", required=True, help="gt06_imei, 15 digits")
    ap.add_argument("--lat", type=float, default=32.5027)
    ap.add_argument("--lon", type=float, default=-117.0037)
    args = ap.parse_args()

    serial = 0
    sock = socket.create_connection((args.host, args.port), timeout=5.0)
    print(f"connected to {args.host}:{args.port}")

    # --- 0x01 login ---
    serial += 1
    frame = build_frame(0x01, imei_to_payload(args.imei), serial)
    sock.sendall(frame)
    try:
        reply = recv_frame(sock, timeout=3.0)
    except (ConnectionError, socket.timeout) as e:
        print(f"login got no reply / connection closed -- IMEI not provisioned or device inactive? ({e})")
        return
    proto, reply_serial = parse_frame(reply)
    assert proto == 0x01, f"expected login ACK (0x01), got 0x{proto:02x}"
    assert reply_serial == serial, f"reply serial {reply_serial} != {serial} sent"
    print(f"login OK (imei={args.imei}) -- ACK received with the correct serial")

    # --- 0x22 GPS position ---
    serial += 1
    payload = gps_block(args.lat, args.lon, speed_kmh=45, course=180, gps_fix=True) + lbs_block()
    frame = build_frame(0x22, payload, serial)
    sock.sendall(frame)
    print(f"position sent: lat={args.lat} lon={args.lon} (no ACK expected, 0x22 does not define one)")
    time.sleep(0.3)  # give the server time to process/insert before the next message

    # --- 0x13 heartbeat ---
    serial += 1
    hb_payload = bytes([0x44]) + bytes([0x06]) + bytes([0x04]) + struct.pack(">H", 0x0201)
    frame = build_frame(0x13, hb_payload, serial)
    sock.sendall(frame)
    proto, reply_serial = parse_frame(recv_frame(sock, timeout=3.0))
    assert proto == 0x13, f"expected heartbeat ACK (0x13), got 0x{proto:02x}"
    assert reply_serial == serial
    print("heartbeat OK -- ACK received")

    # --- 0x26 SOS alarm ---
    serial += 1
    gps = gps_block(args.lat, args.lon, speed_kmh=0, course=0, gps_fix=True)
    lbs = lbs_block()
    alarm_trailer = bytes([0x12]) + bytes([0x06]) + bytes([0x04]) + bytes([0x01, 0x01])  # alarm byte = 0x01 (SOS)
    frame = build_frame(0x26, gps + lbs + alarm_trailer, serial)
    sock.sendall(frame)
    proto, reply_serial = parse_frame(recv_frame(sock, timeout=3.0))
    assert proto == 0x26, f"expected alarm ACK (0x26), got 0x{proto:02x}"
    assert reply_serial == serial
    print("SOS alarm OK -- ACK received (check for a row in `alarms` with alarm_type=gt06_sos)")

    sock.close()
    print("simulation completed without errors")


if __name__ == "__main__":
    main()
