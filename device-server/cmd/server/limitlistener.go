package main

import (
	"net"
	"sync"
)

// limitListener caps how many concurrent TCP connections a net/http.Server
// accepts -- the same semaphore approach gt06server/jt808server/jt1078bridge
// use (MaxConnections) for their own TCP listeners. Reimplemented here rather
// than pulling in golang.org/x/net/netutil just for this, because net/http
// has no native concurrent-connection cap.
//
// Found in a security review: alarmClipSrv (public port 8083, each
// connection may buffer up to 100MB in RAM) was the only public network
// server in this process without a concurrent-connection limit.
type limitListener struct {
	net.Listener
	sem chan struct{}
}

// newLimitListener wraps ln, allowing at most max live connections at once.
// max <= 0 disables the limit (same rule as maxConns in
// gt06server/jt808server -- only for controlled local testing).
func newLimitListener(ln net.Listener, max int) net.Listener {
	if max <= 0 {
		return ln
	}
	return &limitListener{Listener: ln, sem: make(chan struct{}, max)}
}

func (l *limitListener) Accept() (net.Conn, error) {
	l.sem <- struct{}{}
	conn, err := l.Listener.Accept()
	if err != nil {
		<-l.sem
		return nil, err
	}
	return &limitConn{Conn: conn, release: func() { <-l.sem }}, nil
}

// limitConn releases its semaphore slot on Close. closeOnce prevents a double
// release if Close() is called more than once (net/http sometimes does so on
// its own error paths).
type limitConn struct {
	net.Conn
	release   func()
	closeOnce sync.Once
}

func (c *limitConn) Close() error {
	c.closeOnce.Do(c.release)
	return c.Conn.Close()
}
