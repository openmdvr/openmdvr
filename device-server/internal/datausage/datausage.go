// Package datausage counts REAL rx/tx bytes of each jt808server/gt06server
// TCP connection -- i.e. the device's cellular SIM data consumption
// (heartbeats, positions, alarms, video commands, clip uploads). This is a
// different dimension from usage_events, which measures bytes SERVED to the
// browser, never what the device consumes on its own line.
//
// CountingConn wraps net.Conn transparently (same interface). It is applied
// at Accept(), before any protocol logic, so no existing code (session.New,
// sess.WriteFramed, each protocol's framing) needs to know it is counted.
package datausage

import (
	"net"
	"sync/atomic"
)

type CountingConn struct {
	net.Conn
	rx atomic.Int64
	tx atomic.Int64
}

func Wrap(conn net.Conn) *CountingConn {
	return &CountingConn{Conn: conn}
}

func (c *CountingConn) Read(b []byte) (int, error) {
	n, err := c.Conn.Read(b)
	if n > 0 {
		c.rx.Add(int64(n))
	}
	return n, err
}

func (c *CountingConn) Write(b []byte) (int, error) {
	n, err := c.Conn.Write(b)
	if n > 0 {
		c.tx.Add(int64(n))
	}
	return n, err
}

// TakeDelta returns the bytes accumulated since the last TakeDelta() (or
// since creation) and resets them to zero. It is atomic (atomic.Int64.Swap),
// so a concurrent Read/Write during the flush is never lost or double
// counted: before the Swap it is included in the returned delta, after it it
// stays in the reset counter for the next flush.
func (c *CountingConn) TakeDelta() (rx, tx int64) {
	return c.rx.Swap(0), c.tx.Swap(0)
}
