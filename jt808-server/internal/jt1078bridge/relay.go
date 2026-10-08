package jt1078bridge

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/url"
	"time"

	"github.com/cuteLittleDevil/go-jt808/protocol/jt1078"
)

// videoIdleTimeout and videoReadBufSize: same approach as the JT808
// signaling server (internal/jt808server/conn.go). A device streaming video
// sends data constantly, so a timeout shorter than the signaling one is
// reasonable and detects dead streams faster.
const (
	videoIdleTimeout = 30 * time.Second
	videoReadBufSize = 8192
	zlmDialTimeout   = 5 * time.Second
)

// ListenAndServe accepts JT1078 video connections until ctx is cancelled.
// maxConns bounds concurrent connections for the same reason as the JT808
// server: the port is public, so the same mitigation applies from the start.
func (b *Bridge) ListenAndServe(ctx context.Context, maxConns int) error {
	lc := net.ListenConfig{}
	ln, err := lc.Listen(ctx, "tcp", b.cfg.ListenAddr)
	if err != nil {
		return fmt.Errorf("jt1078bridge: listening on %s: %w", b.cfg.ListenAddr, err)
	}
	defer func() { _ = ln.Close() }()
	log.Printf("jt1078bridge: listening for video on %s (max %d concurrent connections)", b.cfg.ListenAddr, maxConns)

	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()

	var sem chan struct{}
	if maxConns > 0 {
		sem = make(chan struct{}, maxConns)
	}

	for {
		conn, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			log.Printf("jt1078bridge: error accepting connection: %v", err)
			continue
		}

		if sem != nil {
			select {
			case sem <- struct{}{}:
				go func() {
					defer func() { <-sem }()
					b.handleVideoConn(ctx, conn)
				}()
			default:
				log.Printf("jt1078bridge: limit of %d concurrent connections reached, rejecting %s", maxConns, conn.RemoteAddr())
				_ = conn.Close()
			}
			continue
		}
		go b.handleVideoConn(ctx, conn)
	}
}

func (b *Bridge) handleVideoConn(ctx context.Context, conn net.Conn) {
	remote := conn.RemoteAddr().String()
	var zlmConn net.Conn
	var activeStreamID string
	defer func() {
		_ = conn.Close()
		if zlmConn != nil {
			_ = zlmConn.Close()
		}
		if activeStreamID != "" {
			b.active.MarkInactive(activeStreamID)
		}
		if r := recover(); r != nil {
			log.Printf("jt1078bridge: %s: recovered panic, closing this connection: %v", remote, r)
		}
		log.Printf("jt1078bridge: video connection closed: %s", remote)
	}()
	log.Printf("jt1078bridge: video connection accepted: %s", remote)

	var (
		reader      packetReader
		reassembler Reassembler
		rtpStream   *RTPStream
		matched     bool
	)
	buf := make([]byte, videoReadBufSize)

	for {
		if err := conn.SetReadDeadline(time.Now().Add(videoIdleTimeout)); err != nil {
			log.Printf("jt1078bridge: %s: error setting read deadline: %v", remote, err)
			return
		}
		n, err := conn.Read(buf)
		if err != nil {
			logVideoReadError(remote, err)
			return
		}

		packets, err := reader.Feed(buf[:n])
		if err != nil {
			log.Printf("jt1078bridge: %s: corrupt JT1078 stream, closing: %v", remote, err)
			return
		}

		for _, p := range packets {
			if !matched {
				entry, ok := b.pending.take(p.Sim, p.LogicChannel)
				if !ok {
					log.Printf("jt1078bridge: %s: video connection without a pending request (sim=%s channel=%d), closing", remote, p.Sim, p.LogicChannel)
					return
				}
				addr, err := zlmRTPAddr(b.cfg.ZLMBaseURL, entry.zlmPort)
				if err != nil {
					log.Printf("jt1078bridge: %s: could not resolve ZLMediaKit address: %v", remote, err)
					return
				}
				zc, err := net.DialTimeout("tcp", addr, zlmDialTimeout)
				if err != nil {
					log.Printf("jt1078bridge: %s: could not connect to ZLMediaKit on port %d: %v", remote, entry.zlmPort, err)
					return
				}
				zlmConn = zc
				rtpStream, err = NewRTPStream(payloadTypeFor(p))
				if err != nil {
					log.Printf("jt1078bridge: %s: generating SSRC: %v", remote, err)
					return
				}
				matched = true
				activeStreamID = entry.streamID
				cutoffCtx, cancel := context.WithCancel(context.Background())
				b.active.MarkActive(activeStreamID, fmt.Sprintf(b.cfg.ZLMPlayURLFormat, activeStreamID), entry.maxSeconds, cancel)
				go b.enforceLiveViewLimit(cutoffCtx, activeStreamID, p.Sim, p.LogicChannel, entry.maxSeconds)
				log.Printf("jt1078bridge: %s: video matched to stream_id=%s (zlm port=%d, limit=%ds)", remote, entry.streamID, entry.zlmPort, entry.maxSeconds)
			}

			frame, ok := reassembler.Feed(p)
			if !ok {
				continue
			}
			if frame.DataType != jt1078.DataTypeI && frame.DataType != jt1078.DataTypeP && frame.DataType != jt1078.DataTypeB {
				// Audio and passthrough are out of scope (see README):
				// discarded without error, not an unexpected condition.
				continue
			}

			out := rtpStream.FrameToRTP(frame.Data, frame.TimestampMs)
			if len(out) == 0 {
				continue
			}
			if err := zlmConn.SetWriteDeadline(time.Now().Add(5 * time.Second)); err != nil {
				log.Printf("jt1078bridge: %s: error setting write deadline to zlm: %v", remote, err)
				return
			}
			if _, err := zlmConn.Write(out); err != nil {
				log.Printf("jt1078bridge: %s: error writing RTP to zlm: %v", remote, err)
				return
			}
		}
	}
}

// payloadTypeFor assumes H.264 by default: by far the most common codec in
// the low-cost MDVR segment this project targets. JT1078 carries no codec
// field in the video packet header -- that would be reported via the 0x1003
// message (audio/video attributes) the device sends separately, not
// implemented yet (pending validation against real hardware to see what each
// vendor sends). When that is implemented, this value should come from there
// instead of a fixed default.
func payloadTypeFor(_ *jt1078.Packet) uint8 {
	return PayloadTypeH264
}

// zlmRTPAddr takes the host from the ZLMediaKit HTTP API base URL (e.g.
// "http://zlmediakit:80") and combines it with the RTP port openRtpServer
// assigned -- two DIFFERENT ports on the same host (the HTTP API one and the
// one opened specifically for this stream).
func zlmRTPAddr(baseURL string, rtpPort int) (string, error) {
	u, err := url.Parse(baseURL)
	if err != nil {
		return "", fmt.Errorf("parsing ZLMBaseURL %q: %w", baseURL, err)
	}
	host := u.Hostname()
	if host == "" {
		return "", fmt.Errorf("ZLMBaseURL %q has no host", baseURL)
	}
	return net.JoinHostPort(host, fmt.Sprintf("%d", rtpPort)), nil
}

func logVideoReadError(remote string, err error) {
	if errors.Is(err, io.EOF) {
		return
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		log.Printf("jt1078bridge: %s: closing due to inactivity (%s without data)", remote, videoIdleTimeout)
		return
	}
	log.Printf("jt1078bridge: %s: read error: %v", remote, err)
}
