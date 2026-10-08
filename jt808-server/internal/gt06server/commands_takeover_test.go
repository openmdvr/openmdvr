package gt06server

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"

	"github.com/openmdvr/openmdvr/jt808-server/internal/commands"
)

// Tests for the safe command-slot handover (canTakeOver/isStrayReply,
// commands.go). The original F1 test
// (TestDispatcher_SendCommand_TimeoutThenLateReplyNeverLeaksIntoNextCommand)
// is unchanged: there is never a handover between engine commands.

func newTakeoverFixture(t *testing.T) (*Dispatcher, *connSession, net.Conn) {
	t.Helper()
	registry := NewRegistry()
	server, client := net.Pipe()
	t.Cleanup(func() { server.Close(); client.Close() })
	sess := &connSession{IMEI: "868720063843126", Authenticated: true, conn: server}
	registry.Register(sess.IMEI, sess)
	runFakeServerReadLoop(sess, server)
	return NewDispatcher(registry), sess, client
}

func readCommand(t *testing.T, client net.Conn) parsedFrame {
	t.Helper()
	buf := make([]byte, 256)
	n, err := client.Read(buf)
	if err != nil {
		t.Fatalf("reading command: %v", err)
	}
	return parseFrame(buf[:n])
}

// The camera does not answer RTMP,OFF (sent when leaving the page), and
// requesting video again used to fail for 2 minutes with "already has a
// pending command". After the caller's timeout, another video command may
// take the slot.
func TestTakeover_VideoAfterAbandonedVideo(t *testing.T) {
	d, sess, client := newTakeoverFixture(t)
	go func() { readCommand(t, client) }() // the camera reads RTMP,OFF and never replies
	if _, err := d.SendCommand(context.Background(), sess.IMEI, "stop_video", 30*time.Millisecond); !errors.Is(err, commands.ErrCommandTimeout) {
		t.Fatalf("stop_video: %v, expected a timeout", err)
	}

	done := make(chan error, 1)
	go func() {
		reply, err := d.SendCommand(context.Background(), sess.IMEI, "request_video", time.Second)
		if err == nil && reply != "RTMP:OK!" {
			err = errors.New("unexpected reply: " + reply)
		}
		done <- err
	}()
	pf := readCommand(t, client)
	_, _ = client.Write(buildCommandReplyFrame("RTMP:OK!", pf.Serial))
	if err := <-done; err != nil {
		t.Fatalf("request_video after an abandoned stop_video should be usable: %v", err)
	}
}

func TestTakeover_NotBeforeCallerGivesUp(t *testing.T) {
	d, sess, client := newTakeoverFixture(t)
	first := make(chan struct{})
	go func() {
		_, _ = d.SendCommand(context.Background(), sess.IMEI, "stop_video", 2*time.Second)
		close(first)
	}()
	pf := readCommand(t, client)
	if _, err := d.SendRawCommand(context.Background(), sess.IMEI, "Picture,out#", time.Second); !errors.Is(err, commands.ErrCommandBusy) {
		t.Fatalf("while the caller is still waiting the slot must not be handed over: err = %v", err)
	}
	_, _ = client.Write(buildCommandReplyFrame("RTMP:OK!", pf.Serial))
	<-first
}

// A late reply tagged with ANOTHER command never resolves the current
// pending command; its own reply does.
func TestStrayReplyIsDiscarded(t *testing.T) {
	d, sess, client := newTakeoverFixture(t)
	done := make(chan string, 1)
	go func() {
		reply, _ := d.SendRawCommand(context.Background(), sess.IMEI, "Picture,in#", time.Second)
		done <- reply
	}()
	pf := readCommand(t, client)
	_, _ = client.Write(buildCommandReplyFrame("RTMP:OK!", pf.Serial)) // late reply from another command
	select {
	case r := <-done:
		t.Fatalf("a reply from another command resolved the pending one: %q", r)
	case <-time.After(80 * time.Millisecond):
	}
	_, _ = client.Write(buildCommandReplyFrame("PICTURE:OK!", pf.Serial))
	if r := <-done; r != "PICTURE:OK!" {
		t.Fatalf("the pending command should resolve with ITS reply, got %q", r)
	}
}

// An abandoned video command yields to an engine command, and the late video
// reply NEVER confirms the engine cut (F1 with different keywords).
func TestTakeover_EngineAfterVideo_LateVideoReplyNeverConfirmsEngine(t *testing.T) {
	d, sess, client := newTakeoverFixture(t)
	go func() { readCommand(t, client) }()
	_, _ = d.SendCommand(context.Background(), sess.IMEI, "request_video", 30*time.Millisecond)

	done := make(chan string, 1)
	go func() {
		reply, _ := d.SendCommand(context.Background(), sess.IMEI, "engine_stop", time.Second)
		done <- reply
	}()
	pf := readCommand(t, client)
	_, _ = client.Write(buildCommandReplyFrame("RTMP:OK!", pf.Serial)) // late video reply
	select {
	case r := <-done:
		t.Fatalf("the late video reply confirmed the engine cut: %q", r)
	case <-time.After(80 * time.Millisecond):
	}
	_, _ = client.Write(buildCommandReplyFrame("DYD=Success!", pf.Serial))
	if r := <-done; r != "DYD=Success!" {
		t.Fatalf("engine_stop should receive ITS reply, got %q", r)
	}
}

func TestKeywords(t *testing.T) {
	for text, want := range map[string]string{"RTMP,ON,INOUT#": "RTMP", "DYD#": "DYD", "Picture,inout#": "PICTURE", "HFYD#": "HFYD"} {
		if got := commandKeyword(text); got != want {
			t.Errorf("commandKeyword(%q) = %q, want %q", text, got, want)
		}
	}
	for text, want := range map[string]string{"RTMP:OK!": "RTMP", "DYD=Success!": "DYD", "HFYD=Success!": "HFYD", "busy": "BUSY", "PICTURE:OK!": "PICTURE"} {
		if got := replyKeyword(text); got != want {
			t.Errorf("replyKeyword(%q) = %q, want %q", text, got, want)
		}
	}
}
