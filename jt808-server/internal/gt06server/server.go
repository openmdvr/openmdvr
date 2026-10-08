// Package gt06server implements the TCP server for the GT06 protocol
// (Concox GT06 and compatible GPS trackers, plus Jimi IoT dashcams that use a
// GT06 flavor for telemetry) -- a port and binary protocol completely
// separate from jt808server (cameras/MDVR). It has no external protocol
// dependency (unlike jt808server, which uses go-jt808/protocol) because the
// supported subset is small enough not to justify one.
//
// Since this package supports remote commands (engine cut/resume, video
// start/stop, configuration), it has ITS OWN Registry (registry.go). It
// deliberately does NOT share the session.Registry used by jt808server/
// jt1078bridge (connSession is a completely different type), keeping the
// blast radius on that already-reviewed code minimal.
package gt06server

import (
	"context"
	"fmt"
	"log"
	"net"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/openmdvr/openmdvr/jt808-server/internal/datausage"
)

type Server struct {
	pool          *pgxpool.Pool
	listenAddr    string
	maxConns      int
	registry      *Registry
	clipRequester ClipRequester
}

// SetClipRequester wires automatic alarm clip requests (see ClipRequester in
// clip_hook.go). Called from main.go AFTER building alarmclip.Bridge (which
// itself needs gt06Dispatcher, already built with this Server -- a setter
// avoids reordering that construction chain). Nil-safe: not calling it keeps
// the previous behavior (no automatic clip requests).
func (s *Server) SetClipRequester(cr ClipRequester) {
	s.clipRequester = cr
}

// New creates the GT06 server. maxConns caps concurrent connections, for the
// same reason as jt808server.New: the port is necessarily public (field
// trackers connect over their own cellular network). maxConns <= 0 means
// "unlimited" (only for controlled local testing). registry is where each
// connection registers after login; it is consumed by a Dispatcher
// (commands.go) built separately in cmd/server/main.go to send active
// commands -- this Server only writes to it, never reads it.
func New(pool *pgxpool.Pool, listenAddr string, maxConns int, registry *Registry) *Server {
	return &Server{pool: pool, listenAddr: listenAddr, maxConns: maxConns, registry: registry}
}

// ListenAndServe accepts connections until ctx is cancelled. Each connection
// runs in its own goroutine; a panic or error in one does not affect the
// others -- same pattern as jt808server.Server.ListenAndServe.
func (s *Server) ListenAndServe(ctx context.Context) error {
	lc := net.ListenConfig{}
	ln, err := lc.Listen(ctx, "tcp", s.listenAddr)
	if err != nil {
		return fmt.Errorf("gt06server: listening on %s: %w", s.listenAddr, err)
	}
	defer func() { _ = ln.Close() }()
	log.Printf("gt06: listening on %s (max %d concurrent connections)", s.listenAddr, s.maxConns)

	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()

	var sem chan struct{}
	if s.maxConns > 0 {
		sem = make(chan struct{}, s.maxConns)
	}

	for {
		conn, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			log.Printf("gt06: error accepting connection: %v", err)
			continue
		}

		// datausage.Wrap BEFORE handing the connection to handleConn, so SIM
		// data usage is counted -- same as jt808server.
		dc := datausage.Wrap(conn)

		if sem != nil {
			select {
			case sem <- struct{}{}:
				go func() {
					defer func() { <-sem }()
					handleConn(ctx, s.pool, s.registry, s.clipRequester, dc)
				}()
			default:
				log.Printf("gt06: concurrent connection limit of %d reached, rejecting %s", s.maxConns, conn.RemoteAddr())
				_ = conn.Close()
			}
			continue
		}
		go handleConn(ctx, s.pool, s.registry, s.clipRequester, dc)
	}
}
