// Package jt808server implements the TCP server that speaks JT/T 808-2019
// with MDVRs/dashcams. It uses github.com/cuteLittleDevil/go-jt808/protocol
// (MIT) as the low-level codec -- framing, escape/unescape, checksum, and the
// structs of each message type -- but all session logic, tenant resolution
// and database writes are our own: this is where our trust boundary against
// thousands of untrusted field devices lives, so it is not delegated to
// external code.
package jt808server

import (
	"context"
	"fmt"
	"log"
	"net"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/openmdvr/openmdvr/jt808-server/internal/datausage"
	"github.com/openmdvr/openmdvr/jt808-server/internal/session"
)

type Server struct {
	pool       *pgxpool.Pool
	registry   *session.Registry
	listenAddr string
	maxConns   int
}

// New creates the server. maxConns bounds how many concurrent connections are
// accepted: the JT808 port is necessarily public (field MDVRs connect over
// their own cellular network), so without this limit an attacker with network
// access could open unbounded connections (goroutine + buffers + Session
// each), exhausting the whole process's memory (finding from the server's
// security review). maxConns <= 0 means "no limit" (controlled local tests
// only).
//
// registry is shared with the video bridge (jt1078bridge), so it can find a
// device's already open JT808 connection to send it a 0x9101 when someone
// asks to watch its video.
func New(pool *pgxpool.Pool, registry *session.Registry, listenAddr string, maxConns int) *Server {
	return &Server{pool: pool, registry: registry, listenAddr: listenAddr, maxConns: maxConns}
}

// ListenAndServe accepts connections until ctx is cancelled. Each connection
// runs in its own goroutine; a panic or error in one does not affect the
// others.
func (s *Server) ListenAndServe(ctx context.Context) error {
	lc := net.ListenConfig{}
	ln, err := lc.Listen(ctx, "tcp", s.listenAddr)
	if err != nil {
		return fmt.Errorf("jt808server: listening on %s: %w", s.listenAddr, err)
	}
	defer func() { _ = ln.Close() }()
	log.Printf("jt808: listening on %s (max %d concurrent connections)", s.listenAddr, s.maxConns)

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
			log.Printf("jt808: error accepting connection: %v", err)
			continue
		}

		// datausage.Wrap BEFORE handing the connection to handleConn: counts
		// the device's REAL cellular SIM data usage, transparently for the
		// rest of the code (CountingConn still implements net.Conn).
		dc := datausage.Wrap(conn)

		if sem != nil {
			select {
			case sem <- struct{}{}:
				go func() {
					defer func() { <-sem }()
					handleConn(ctx, s.pool, s.registry, dc)
				}()
			default:
				log.Printf("jt808: limit of %d concurrent connections reached, rejecting %s", s.maxConns, conn.RemoteAddr())
				_ = conn.Close()
			}
			continue
		}
		go handleConn(ctx, s.pool, s.registry, dc)
	}
}
