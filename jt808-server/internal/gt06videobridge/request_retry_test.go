package gt06videobridge

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/openmdvr/openmdvr/jt808-server/internal/commands"
)

type scriptedSender struct {
	mu    sync.Mutex
	calls int
	errs  []error // one error per call; nil = success
}

func (s *scriptedSender) SendCommand(ctx context.Context, deviceKey, commandType string, timeout time.Duration) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	i := s.calls
	s.calls++
	if i < len(s.errs) && s.errs[i] != nil {
		return "", s.errs[i]
	}
	return "RTMP:OK!", nil
}

// A busy command slot is no longer a user-visible error: it is retried until
// it frees up (seen while a photo or an RTMP,OFF was still in progress).
func TestSendRequestVideo_RetriesWhileBusy(t *testing.T) {
	busy := commands.ErrCommandBusy
	s := &scriptedSender{errs: []error{busy, busy, nil}}
	b := newTestBridge("live")
	b.sender = s
	reply, err := b.sendRequestVideo(context.Background(), "490154203237518", time.Now().Add(10*time.Second))
	if err != nil || reply != "RTMP:OK!" {
		t.Fatalf("reply=%q err=%v, want success after two 'busy'", reply, err)
	}
	if s.calls != 3 {
		t.Fatalf("calls = %d, want 3", s.calls)
	}
}

// Not connected is not retried: the error comes back immediately.
func TestSendRequestVideo_NotConnectedFailsFast(t *testing.T) {
	s := &scriptedSender{errs: []error{commands.ErrDeviceNotConnected}}
	b := newTestBridge("live")
	b.sender = s
	start := time.Now()
	_, err := b.sendRequestVideo(context.Background(), "x", time.Now().Add(10*time.Second))
	if !errors.Is(err, commands.ErrDeviceNotConnected) || time.Since(start) > time.Second || s.calls != 1 {
		t.Fatalf("err=%v calls=%d, want immediate failure", err, s.calls)
	}
}

// Busy until the end of the budget: it gives up in time.
func TestSendRequestVideo_GivesUpAtDeadline(t *testing.T) {
	busy := commands.ErrCommandBusy
	s := &scriptedSender{errs: []error{busy, busy, busy, busy, busy, busy}}
	b := newTestBridge("live")
	b.sender = s
	start := time.Now()
	_, err := b.sendRequestVideo(context.Background(), "x", time.Now().Add(2500*time.Millisecond))
	if !errors.Is(err, commands.ErrCommandBusy) {
		t.Fatalf("err=%v, want busy when the budget runs out", err)
	}
	if time.Since(start) > 3*time.Second {
		t.Fatalf("took %s, exceeded the budget", time.Since(start))
	}
}
