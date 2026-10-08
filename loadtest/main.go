// Command loadtest simulates many GT06 GPS trackers against a running
// OpenMDVR device server to measure concurrent-connection capacity and
// end-to-end ingestion throughput.
//
// Every simulated device opens its own TCP connection, logs in with its
// IMEI, waits for the login ACK, and then reports a GPS position every
// -interval (with jitter) plus a periodic heartbeat — the same traffic
// pattern as a real tracker on a cellular link.
//
// Devices must be provisioned first (see provision.sql), otherwise the
// server will (correctly) refuse the login.
//
//	go run . -addr 127.0.0.1:5023 -count 5000 -imei-base 990000000000000 \
//	         -interval 10s -ramp 60s -duration 5m
package main

import (
	"bufio"
	"encoding/binary"
	"errors"
	"flag"
	"fmt"
	"math"
	"math/rand"
	"net"
	"os"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

type stats struct {
	connected     atomic.Int64
	loginOK       atomic.Int64
	loginFailed   atomic.Int64
	positionsSent atomic.Int64
	heartbeats    atomic.Int64
	dropped       atomic.Int64

	mu        sync.Mutex
	loginLats []time.Duration
}

func main() {
	addr := flag.String("addr", "127.0.0.1:5023", "GT06 server address")
	count := flag.Int("count", 1000, "number of simulated devices")
	imeiBase := flag.Uint64("imei-base", 990000000000000, "first IMEI (15 digits); device i uses imei-base+i")
	interval := flag.Duration("interval", 10*time.Second, "position report interval per device")
	heartbeat := flag.Duration("heartbeat", 3*time.Minute, "heartbeat interval per device")
	ramp := flag.Duration("ramp", 30*time.Second, "time over which all devices connect")
	duration := flag.Duration("duration", 2*time.Minute, "steady-state duration after ramp-up")
	lat := flag.Float64("lat", 32.71, "center latitude")
	lon := flag.Float64("lon", -117.16, "center longitude")
	flag.Parse()

	if *imeiBase+uint64(*count) > 999999999999999 {
		fmt.Fprintln(os.Stderr, "imei-base + count exceeds 15 digits")
		os.Exit(2)
	}

	st := &stats{}
	stop := time.Now().Add(*ramp + *duration)
	var wg sync.WaitGroup
	step := time.Duration(0)
	if *count > 1 {
		step = *ramp / time.Duration(*count)
	}

	start := time.Now()
	go reporter(st, start, stop)

	for i := 0; i < *count; i++ {
		wg.Add(1)
		imei := fmt.Sprintf("%015d", *imeiBase+uint64(i))
		// Spread devices around the center so they are not all at one point.
		dlat := *lat + (rand.Float64()-0.5)*0.4
		dlon := *lon + (rand.Float64()-0.5)*0.4
		go func() {
			defer wg.Done()
			runDevice(st, *addr, imei, dlat, dlon, *interval, *heartbeat, stop)
		}()
		time.Sleep(step)
	}
	wg.Wait()
	summary(st, *count, time.Since(start), *ramp, *duration)
}

func runDevice(st *stats, addr, imei string, lat, lon float64, interval, hb time.Duration, stop time.Time) {
	conn, err := net.DialTimeout("tcp", addr, 10*time.Second)
	if err != nil {
		st.loginFailed.Add(1)
		return
	}
	defer conn.Close()
	st.connected.Add(1)
	defer st.connected.Add(-1)

	r := bufio.NewReader(conn)
	var serial uint16 = 1

	t0 := time.Now()
	if _, err := conn.Write(frame(0x01, imeiPayload(imei), serial)); err != nil {
		st.loginFailed.Add(1)
		return
	}
	_ = conn.SetReadDeadline(time.Now().Add(15 * time.Second))
	proto, err := readFrame(r)
	if err != nil || proto != 0x01 {
		st.loginFailed.Add(1)
		return
	}
	lat0 := time.Since(t0)
	st.loginOK.Add(1)
	st.mu.Lock()
	st.loginLats = append(st.loginLats, lat0)
	st.mu.Unlock()

	// Drain server ACKs (heartbeat ACKs etc.) so the socket never backs up,
	// and detect server-side disconnects.
	closed := make(chan struct{})
	go func() {
		defer close(closed)
		for {
			_ = conn.SetReadDeadline(time.Now().Add(30 * time.Minute))
			if _, err := readFrame(r); err != nil {
				return
			}
		}
	}()

	// Random phase so devices do not report in lockstep.
	next := time.Now().Add(time.Duration(rand.Int63n(int64(interval))))
	nextHB := time.Now().Add(hb)
	heading := rand.Float64() * 2 * math.Pi
	for time.Now().Before(stop) {
		select {
		case <-closed:
			st.dropped.Add(1)
			return
		case <-time.After(time.Until(next)):
		}
		serial++
		// ~40 km/h drive in a gently curving direction.
		heading += (rand.Float64() - 0.5) * 0.3
		step := 40.0 / 3600.0 * interval.Seconds() / 111.0
		lat += step * math.Cos(heading)
		lon += step * math.Sin(heading)
		course := int(math.Mod(heading*180/math.Pi+360, 360))
		payload := append(gpsBlock(lat, lon, 40, course), lbsBlock()...)
		if _, err := conn.Write(frame(0x22, payload, serial)); err != nil {
			st.dropped.Add(1)
			return
		}
		st.positionsSent.Add(1)
		if time.Now().After(nextHB) {
			serial++
			if _, err := conn.Write(frame(0x13, []byte{0x46, 0x06, 0x04, 0x02, 0x01}, serial)); err != nil {
				st.dropped.Add(1)
				return
			}
			st.heartbeats.Add(1)
			nextHB = time.Now().Add(hb)
		}
		next = next.Add(interval + time.Duration(rand.Int63n(int64(interval/10)+1)))
	}
}

func reporter(st *stats, start, stop time.Time) {
	var last int64
	t := time.NewTicker(10 * time.Second)
	defer t.Stop()
	for now := range t.C {
		if now.After(stop) {
			return
		}
		sent := st.positionsSent.Load()
		fmt.Printf("[%5.0fs] connected=%d loginOK=%d loginFailed=%d dropped=%d positions/s=%.1f\n",
			now.Sub(start).Seconds(), st.connected.Load(), st.loginOK.Load(), st.loginFailed.Load(),
			st.dropped.Load(), float64(sent-last)/10)
		last = sent
	}
}

func summary(st *stats, count int, total, ramp, dur time.Duration) {
	st.mu.Lock()
	lats := append([]time.Duration(nil), st.loginLats...)
	st.mu.Unlock()
	sort.Slice(lats, func(i, j int) bool { return lats[i] < lats[j] })
	pct := func(p float64) time.Duration {
		if len(lats) == 0 {
			return 0
		}
		return lats[int(math.Min(float64(len(lats)-1), p*float64(len(lats))))]
	}
	fmt.Println("\n=== OpenMDVR GT06 load test summary ===")
	fmt.Printf("devices requested     : %d\n", count)
	fmt.Printf("logins OK / failed    : %d / %d\n", st.loginOK.Load(), st.loginFailed.Load())
	fmt.Printf("dropped by server     : %d\n", st.dropped.Load())
	fmt.Printf("login latency p50/p95/p99/max: %v / %v / %v / %v\n",
		pct(0.50).Round(time.Millisecond), pct(0.95).Round(time.Millisecond),
		pct(0.99).Round(time.Millisecond), pct(1).Round(time.Millisecond))
	fmt.Printf("positions sent        : %d (%.1f/s over steady state)\n",
		st.positionsSent.Load(), float64(st.positionsSent.Load())/dur.Seconds())
	fmt.Printf("heartbeats sent       : %d\n", st.heartbeats.Load())
	fmt.Printf("wall time             : %v (ramp %v + steady %v)\n", total.Round(time.Second), ramp, dur)
	fmt.Println("Compare 'positions sent' with rows inserted in gps_positions for the same window (see README).")
}

// --- GT06 framing (independent of the server implementation) ---

func crc16X25(data []byte) uint16 {
	crc := uint16(0xFFFF)
	for _, b := range data {
		crc ^= uint16(b)
		for i := 0; i < 8; i++ {
			if crc&1 != 0 {
				crc = (crc >> 1) ^ 0x8408
			} else {
				crc >>= 1
			}
		}
	}
	return ^crc
}

func frame(proto byte, payload []byte, serial uint16) []byte {
	body := append([]byte{proto}, payload...)
	body = binary.BigEndian.AppendUint16(body, serial)
	length := byte(len(body) + 2)
	crc := crc16X25(append([]byte{length}, body...))
	out := []byte{0x78, 0x78, length}
	out = append(out, body...)
	out = binary.BigEndian.AppendUint16(out, crc)
	return append(out, 0x0D, 0x0A)
}

func imeiPayload(imei string) []byte {
	padded := "0" + imei
	out := make([]byte, 8)
	for i := 0; i < 8; i++ {
		out[i] = (padded[2*i]-'0')<<4 | (padded[2*i+1] - '0')
	}
	return out
}

func gpsBlock(lat, lon float64, speed, course int) []byte {
	now := time.Now().UTC()
	b := []byte{byte(now.Year() - 2000), byte(now.Month()), byte(now.Day()),
		byte(now.Hour()), byte(now.Minute()), byte(now.Second()), 0xCC}
	b = binary.BigEndian.AppendUint32(b, uint32(math.Round(math.Abs(lat)*60*30000)))
	b = binary.BigEndian.AppendUint32(b, uint32(math.Round(math.Abs(lon)*60*30000)))
	b = append(b, byte(speed))
	flags := uint16(course&0x03FF) | 1<<12 // GPS fixed
	if lat >= 0 {
		flags |= 1 << 10
	}
	if lon < 0 {
		flags |= 1 << 11
	}
	return binary.BigEndian.AppendUint16(b, flags)
}

func lbsBlock() []byte {
	return []byte{0x01, 0xCC, 0x00, 0x26, 0x33, 0x00, 0x0E, 0x7F}
}

// readFrame reads one 0x7878 or 0x7979 frame and returns its protocol number.
func readFrame(r *bufio.Reader) (byte, error) {
	hdr := make([]byte, 2)
	if _, err := ioReadFull(r, hdr); err != nil {
		return 0, err
	}
	var n int
	switch {
	case hdr[0] == 0x78 && hdr[1] == 0x78:
		l, err := r.ReadByte()
		if err != nil {
			return 0, err
		}
		n = int(l)
	case hdr[0] == 0x79 && hdr[1] == 0x79:
		lb := make([]byte, 2)
		if _, err := ioReadFull(r, lb); err != nil {
			return 0, err
		}
		n = int(binary.BigEndian.Uint16(lb))
	default:
		return 0, errors.New("bad frame header")
	}
	rest := make([]byte, n+2) // body+crc (n) + 0x0D0A
	if _, err := ioReadFull(r, rest); err != nil {
		return 0, err
	}
	if n < 1 {
		return 0, errors.New("empty frame")
	}
	return rest[0], nil
}

func ioReadFull(r *bufio.Reader, b []byte) (int, error) {
	read := 0
	for read < len(b) {
		n, err := r.Read(b[read:])
		read += n
		if err != nil {
			return read, err
		}
	}
	return read, nil
}
