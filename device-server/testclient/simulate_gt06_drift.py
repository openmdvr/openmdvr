"""End-to-end test of the GPS quality filter (internal/gpsfilter) against a
running GT06 server: a STATIONARY unit reporting drift (position wandering up
to ~20 m with junk speeds of 0-9 km/h), a 2.5 km multipath outlier, and then
a real departure at ~40 km/h. Reuses the codec from simulate_gt06.py.

Usage:
    python simulate_gt06_drift.py --imei 999000111222444 [--host 127.0.0.1 --port 5023]

Afterwards, inspect gps_positions for that device: drift readings should be
stored at a single point with speed 0 and the original reading in raw; the
outlier must not appear; the trip must be stored as-is.
"""

import argparse
import math
import random
import socket
import time

from simulate_gt06 import build_frame, gps_block, imei_to_payload, lbs_block, parse_frame, recv_frame

BASE_LAT, BASE_LON = 32.49910, -116.92128


def offset(lat, lon, d, bearing):
    rad = math.pi / 180
    return (
        lat + d * math.cos(bearing * rad) / 111320,
        lon + d * math.sin(bearing * rad) / (111320 * math.cos(lat * rad)),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--imei", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5023)
    args = ap.parse_args()

    rng = random.Random(42)
    sock = socket.create_connection((args.host, args.port), timeout=5.0)
    serial = 1
    sock.sendall(build_frame(0x01, imei_to_payload(args.imei), serial))
    proto, _ = parse_frame(recv_frame(sock))
    assert proto == 0x01, f"expected login ACK, got 0x{proto:02x}"
    print("login OK")

    def send(lat, lon, speed, course=90):
        nonlocal serial
        serial += 1
        sock.sendall(build_frame(0x12, gps_block(lat, lon, int(speed), course, True) + lbs_block(), serial))
        time.sleep(1.05)  # distinct timestamp per reading (1 s resolution)

    # 1) Stationary with drift.
    send(BASE_LAT, BASE_LON, 0)
    for _ in range(39):
        lat, lon = offset(BASE_LAT, BASE_LON, rng.random() * 20, rng.random() * 360)
        send(lat, lon, rng.random() * 9, rng.randint(0, 359))
    print("drift sent")

    # 2) 2.5 km multipath outlier and back.
    lat, lon = offset(BASE_LAT, BASE_LON, 2500, 200)
    send(lat, lon, 0)
    send(*offset(BASE_LAT, BASE_LON, 5, 10), 3)
    print("outlier sent")

    # 3) Real departure eastbound at ~40 km/h.
    lat, lon = BASE_LAT, BASE_LON
    for _ in range(12):
        lat, lon = offset(lat, lon, 40 / 3.6 * 1.05, 90)
        send(lat, lon, 40, 90)
    print("trip sent")
    sock.close()


if __name__ == "__main__":
    main()
