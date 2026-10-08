package gt06server

import (
	"context"
	"errors"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/openmdvr/openmdvr/jt808-server/internal/commands"
)

var errCommandFrameInvalid = errors.New("invalid command frame received in test")

// buildCommandReplyFrame builds a real 0x15 frame (same "information" layout
// as the outgoing 0x80) with the given reply text.
func buildCommandReplyFrame(text string, serial uint16) []byte {
	content := []byte(text)
	payload := []byte{byte(4 + len(content) + 2), 0, 0, 0, 0}
	payload = append(payload, content...)
	payload = append(payload, 0x00, 0x02)
	return encodeFrame(protoCommandReply, payload, serial)
}

// runFakeServerReadLoop simulates the part of conn.go these tests need: it
// reads frames from the "server" end of the net.Pipe and calls
// handleCommandReply on a 0x15. Without it SendCommand never learns about the
// reply the test "device" writes on the other end (net.Pipe is synchronous: a
// device Write blocks forever unless the server side Reads, which in
// production is exactly what conn.go does).
func runFakeServerReadLoop(sess *connSession, conn net.Conn) {
	go func() {
		reader := &packetReader{}
		buf := make([]byte, 256)
		for {
			n, err := conn.Read(buf)
			if err != nil {
				return
			}
			frames, err := reader.Feed(buf[:n])
			if err != nil {
				return
			}
			for _, f := range frames {
				pf := parseFrame(f)
				if pf.CRCValid && (pf.ProtocolNumber == protoCommandReply || pf.ProtocolNumber == protoCommandReplyJC261) {
					handleCommandReply("test", sess, pf)
				}
			}
		}
	}()
}

func TestEncodeCommandFrame_StructureMatchesSpec(t *testing.T) {
	frame := encodeCommandFrame("DYD#", 7, true)
	pf := parseFrame(frame)
	if !pf.CRCValid {
		t.Fatal("invalid CRC in the encoded command frame")
	}
	if pf.ProtocolNumber != protoCommand {
		t.Errorf("ProtocolNumber = 0x%02X, want 0x%02X", pf.ProtocolNumber, protoCommand)
	}
	if pf.Serial != 7 {
		t.Errorf("Serial = %d, want 7", pf.Serial)
	}

	// Payload = LengthOfCommand(1) + ServerFlagBit(4) + "DYD#"(4) + language(2).
	if len(pf.Payload) != 1+4+4+2 {
		t.Fatalf("len(Payload) = %d, want 11", len(pf.Payload))
	}
	if pf.Payload[0] != byte(4+4+2) {
		t.Errorf("LengthOfCommand = %d, want %d (formula from the protocol document)", pf.Payload[0], 4+4+2)
	}
	for i := 1; i <= 4; i++ {
		if pf.Payload[i] != 0 {
			t.Errorf("Server Flag Bit[%d] = 0x%02X, want 0x00 (always zero, not used for correlation)", i-1, pf.Payload[i])
		}
	}
	if got := string(pf.Payload[5:9]); got != "DYD#" {
		t.Errorf("content = %q, want %q", got, "DYD#")
	}
	if pf.Payload[9] != 0x00 || pf.Payload[10] != 0x02 {
		t.Errorf("language = %02X %02X, want 00 02 (English)", pf.Payload[9], pf.Payload[10])
	}
}

// TestEncodeCommandFrame_Unpadded covers SendRawCommandUnpadded: without the
// standard 4-zero Server Flag Bit, LengthOfCommand and the payload shrink by
// exactly 4 bytes, with the content starting IMMEDIATELY after the length
// byte.
func TestEncodeCommandFrame_Unpadded(t *testing.T) {
	frame := encodeCommandFrame("VIDEO_TIMELINE,1,2#", 9, false)
	pf := parseFrame(frame)
	if !pf.CRCValid {
		t.Fatal("invalid CRC in the encoded command frame")
	}

	content := "VIDEO_TIMELINE,1,2#"
	// Payload = LengthOfCommand(1) + content(20, WITHOUT Server Flag Bit) + language(2).
	if len(pf.Payload) != 1+len(content)+2 {
		t.Fatalf("len(Payload) = %d, want %d", len(pf.Payload), 1+len(content)+2)
	}
	if pf.Payload[0] != byte(len(content)+2) {
		t.Errorf("LengthOfCommand = %d, want %d (without the 4 Server Flag Bit bytes)", pf.Payload[0], len(content)+2)
	}
	if got := string(pf.Payload[1 : 1+len(content)]); got != content {
		t.Errorf("content = %q, want %q (must start at byte 1, no padding)", got, content)
	}
	last := 1 + len(content)
	if pf.Payload[last] != 0x00 || pf.Payload[last+1] != 0x02 {
		t.Errorf("language = %02X %02X, want 00 02 (English)", pf.Payload[last], pf.Payload[last+1])
	}
}

func TestHandleCommandReply_ResolvesPendingChannel(t *testing.T) {
	pending := &pendingCommand{ch: make(chan string, 1), createdAt: time.Now(), keyword: "DYD"}
	sess := &connSession{pending: pending}

	pf := parseFrame(buildCommandReplyFrame("DYD=Success!", 7))
	if !pf.CRCValid {
		t.Fatal("invalid CRC in the test frame")
	}

	handleCommandReply("test", sess, pf)

	// Security review finding: this test used to check only that the channel
	// was empty AFTER handleCommandReply had already drained it -- it passed
	// even if the function was a no-op, never verifying the right text
	// actually travelled through the channel. It now reads BEFORE clearing.
	select {
	case reply := <-pending.ch:
		if reply != "DYD=Success!" {
			t.Errorf("reply = %q, want %q", reply, "DYD=Success!")
		}
	default:
		t.Fatal("pending.ch received no value -- handleCommandReply did not deliver the reply")
	}

	sess.mu.Lock()
	stillSet := sess.pending != nil
	sess.mu.Unlock()
	if stillSet {
		t.Error("sess.pending still set after delivering the reply, want cleared")
	}
}

func TestHandleCommandReply_NoPendingCommand_NeverPanics(t *testing.T) {
	sess := &connSession{} // no pending -- an unexpected or late 0x15
	pf := parseFrame(buildCommandReplyFrame("DYD=Success!", 1))

	handleCommandReply("test", sess, pf) // must not panic or block
}

func TestHandleCommandReply_TooShortPayload_Ignored(t *testing.T) {
	pending := &pendingCommand{ch: make(chan string, 1), createdAt: time.Now()}
	sess := &connSession{pending: pending}
	frame := encodeFrame(protoCommandReply, []byte{0x01, 0x02}, 1) // 2-byte payload, below the minimum of 7
	pf := parseFrame(frame)

	handleCommandReply("test", sess, pf)

	select {
	case <-pending.ch:
		t.Error("pending.ch received a value from a too-short payload, want ignored")
	default:
	}
	sess.mu.Lock()
	stillSet := sess.pending != nil
	sess.mu.Unlock()
	if !stillSet {
		t.Error("sess.pending was cleared by an invalid payload, want kept (it may be noise, not the real reply)")
	}
}

// TestHandleCommandReply_JC261RealPayload_ResolvesPendingChannel reproduces
// real JC261 behavior: the firmware answers the 0x80 command with protocol
// number 0x21 (not 0x15) and a byte layout NOT documented in the protocol
// document -- payload captured from real hardware after sending
// "RTMP,ON,INOUT#": `00 00 00 00 01` + ASCII "RTMP:OK!".
func TestHandleCommandReply_JC261RealPayload_ResolvesPendingChannel(t *testing.T) {
	pending := &pendingCommand{ch: make(chan string, 1), createdAt: time.Now(), keyword: "RTMP"}
	sess := &connSession{pending: pending}

	realPayload := []byte{0x00, 0x00, 0x00, 0x00, 0x01, 'R', 'T', 'M', 'P', ':', 'O', 'K', '!'}
	frame := encodeFrame(protoCommandReplyJC261, realPayload, 1)
	pf := parseFrame(frame)
	if !pf.CRCValid {
		t.Fatal("invalid CRC in the test frame")
	}

	handleCommandReply("test", sess, pf)

	select {
	case reply := <-pending.ch:
		if reply != "RTMP:OK!" {
			t.Errorf("reply = %q, want %q", reply, "RTMP:OK!")
		}
	default:
		t.Fatal("pending.ch received no value -- the real JC261 0x21 did not resolve the pending command")
	}
}

func TestExtractPrintableASCII(t *testing.T) {
	cases := []struct {
		name    string
		payload []byte
		want    string
	}{
		{"empty", nil, ""},
		{"all non-printable", []byte{0x00, 0x01, 0x02}, ""},
		{"standard 0x15 layout (length+flag+text+language)", append(append([]byte{18, 0, 0, 0, 0}, []byte("DYD=Success!")...), 0x00, 0x02), "DYD=Success!"},
		{"real JC261 layout (flag+extra byte+text, no language)", []byte{0x00, 0x00, 0x00, 0x00, 0x01, 'R', 'T', 'M', 'P', ':', 'O', 'K', '!'}, "RTMP:OK!"},
		{"text at the start, no leading padding", []byte("OK"), "OK"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := extractPrintableASCII(c.payload); got != c.want {
				t.Errorf("extractPrintableASCII(%v) = %q, want %q", c.payload, got, c.want)
			}
		})
	}
}

func TestDispatcher_SendCommand_DeviceNotConnected(t *testing.T) {
	d := NewDispatcher(NewRegistry())
	_, err := d.SendCommand(context.Background(), "868720063843126", "engine_stop", time.Second)
	if !errors.Is(err, commands.ErrDeviceNotConnected) {
		t.Errorf("err = %v, want errors.Is(err, commands.ErrDeviceNotConnected)", err)
	}
}

func TestDispatcher_SendCommand_UnauthenticatedSession_TreatedAsNotConnected(t *testing.T) {
	registry := NewRegistry()
	sess := &connSession{IMEI: "868720063843126", Authenticated: false}
	registry.Register(sess.IMEI, sess)
	d := NewDispatcher(registry)

	_, err := d.SendCommand(context.Background(), sess.IMEI, "engine_stop", time.Second)
	if !errors.Is(err, commands.ErrDeviceNotConnected) {
		t.Errorf("err = %v, want errors.Is(err, commands.ErrDeviceNotConnected)", err)
	}
}

// TestDispatcher_HasActiveSession is a regression test for a critical
// security review finding: internal/alarmclip requires this before accepting
// a public upload by IMEI -- with no session, or an unauthenticated one (GT06
// login never completed), it must report false.
func TestDispatcher_HasActiveSession(t *testing.T) {
	registry := NewRegistry()
	d := NewDispatcher(registry)

	if d.HasActiveSession("868720063843126") {
		t.Error("HasActiveSession() = true for an IMEI with no registered connection, want false")
	}

	unauth := &connSession{IMEI: "868720063843126", Authenticated: false}
	registry.Register(unauth.IMEI, unauth)
	if d.HasActiveSession(unauth.IMEI) {
		t.Error("HasActiveSession() = true for a session without a completed GT06 login, want false")
	}

	auth := &connSession{IMEI: "868720063843126", Authenticated: true}
	registry.Register(auth.IMEI, auth)
	if !d.HasActiveSession(auth.IMEI) {
		t.Error("HasActiveSession() = false for a real authenticated session, want true")
	}
}

func TestDispatcher_SendCommand_UnknownCommandType(t *testing.T) {
	registry := NewRegistry()
	server, client := net.Pipe()
	defer server.Close()
	defer client.Close()
	sess := &connSession{IMEI: "868720063843126", Authenticated: true, conn: server}
	registry.Register(sess.IMEI, sess)
	d := NewDispatcher(registry)

	_, err := d.SendCommand(context.Background(), sess.IMEI, "drain_tank", time.Second)
	if err == nil {
		t.Fatal("err = nil, want an error for an unknown command_type")
	}
	if errors.Is(err, commands.ErrDeviceNotConnected) {
		t.Error("an unknown command_type should not be reported as device not connected")
	}
}

// TestDispatcher_SendCommand_SuccessRoundTrip verifies the full cycle:
// SendCommand writes a real 0x80 to the socket, a test "device" (the other
// end of a net.Pipe) reads it and replies with a real 0x15, and SendCommand
// returns exactly that text.
func TestDispatcher_SendCommand_SuccessRoundTrip(t *testing.T) {
	registry := NewRegistry()
	server, client := net.Pipe()
	defer server.Close()
	defer client.Close()

	sess := &connSession{
		IMEI:          "868720063843126",
		DeviceID:      uuid.New(),
		TenantID:      uuid.New(),
		Authenticated: true,
		conn:          server,
	}
	registry.Register(sess.IMEI, sess)
	d := NewDispatcher(registry)
	runFakeServerReadLoop(sess, server)

	deviceErrCh := make(chan error, 1)
	go func() {
		buf := make([]byte, 256)
		n, err := client.Read(buf)
		if err != nil {
			deviceErrCh <- err
			return
		}
		pf := parseFrame(buf[:n])
		if !pf.CRCValid || pf.ProtocolNumber != protoCommand {
			deviceErrCh <- errCommandFrameInvalid
			return
		}
		if _, err := client.Write(buildCommandReplyFrame("DYD=Success!", pf.Serial)); err != nil {
			deviceErrCh <- err
			return
		}
		deviceErrCh <- nil
	}()

	reply, err := d.SendCommand(context.Background(), sess.IMEI, "engine_stop", 2*time.Second)
	if err != nil {
		t.Fatalf("SendCommand error = %v, want nil", err)
	}
	if reply != "DYD=Success!" {
		t.Errorf("reply = %q, want %q", reply, "DYD=Success!")
	}
	if err := <-deviceErrCh; err != nil {
		t.Fatalf("test \"device\" side failed: %v", err)
	}
}

// TestDispatcher_SendRawCommand_SendsLiteralTextNotLookedUp confirms why
// SendRawCommand exists (see commands.go): arbitrary text with dynamic
// parameters (e.g. a timestamp for a specific video clip, see
// internal/alarmclip) is sent AS IS over the socket, without going through
// commandText -- unlike SendCommand, which only accepts mapped symbolic
// names.
func TestDispatcher_SendRawCommand_SendsLiteralTextNotLookedUp(t *testing.T) {
	registry := NewRegistry()
	server, client := net.Pipe()
	defer server.Close()
	defer client.Close()

	sess := &connSession{
		IMEI:          "868720063843126",
		DeviceID:      uuid.New(),
		TenantID:      uuid.New(),
		Authenticated: true,
		conn:          server,
	}
	registry.Register(sess.IMEI, sess)
	d := NewDispatcher(registry)
	runFakeServerReadLoop(sess, server)

	const rawText = "PLAYBACK,20260916093000,20260916093100#"
	deviceGotCh := make(chan string, 1)
	deviceErrCh := make(chan error, 1)
	go func() {
		buf := make([]byte, 256)
		n, err := client.Read(buf)
		if err != nil {
			deviceErrCh <- err
			return
		}
		pf := parseFrame(buf[:n])
		if !pf.CRCValid || pf.ProtocolNumber != protoCommand {
			deviceErrCh <- errCommandFrameInvalid
			return
		}
		// extractPrintableASCII is deliberately not used here -- it is meant
		// for INCOMING replies (0x15/0x21), not for inspecting this test's
		// OUTGOING payload (which starts with the length byte + 4 Server
		// Flag Bit zeros, a different layout). strings.Contains is enough to
		// confirm the text travelled literally.
		deviceGotCh <- string(pf.Payload)
		if _, err := client.Write(buildCommandReplyFrame("OK", pf.Serial)); err != nil {
			deviceErrCh <- err
			return
		}
		deviceErrCh <- nil
	}()

	reply, err := d.SendRawCommand(context.Background(), sess.IMEI, rawText, 2*time.Second)
	if err != nil {
		t.Fatalf("SendRawCommand error = %v, want nil", err)
	}
	if reply != "OK" {
		t.Errorf("reply = %q, want %q", reply, "OK")
	}
	if err := <-deviceErrCh; err != nil {
		t.Fatalf("test \"device\" side failed: %v", err)
	}
	if got := <-deviceGotCh; !strings.Contains(got, rawText) {
		t.Errorf("device received %q, want it to contain the literal text %q (must not go through commandText)", got, rawText)
	}
}

// TestDispatcher_SendRawCommandFireAndForget_ReturnsImmediately covers why
// this method exists (see commands.go): some clip retrieval commands never
// get a synchronous reply, so waiting for one blocked the caller until the
// timeout. The command is sent to a "device" that NEVER replies and the call
// must still return almost immediately.
func TestDispatcher_SendRawCommandFireAndForget_ReturnsImmediately(t *testing.T) {
	registry := NewRegistry()
	server, client := net.Pipe()
	defer server.Close()
	defer client.Close()

	sess := &connSession{
		IMEI:          "868720063843126",
		DeviceID:      uuid.New(),
		TenantID:      uuid.New(),
		Authenticated: true,
		conn:          server,
	}
	registry.Register(sess.IMEI, sess)
	d := NewDispatcher(registry)

	deviceGotCh := make(chan string, 1)
	go func() {
		buf := make([]byte, 256)
		n, err := client.Read(buf)
		if err != nil {
			return
		}
		pf := parseFrame(buf[:n])
		deviceGotCh <- string(pf.Payload)
		// Deliberately NEVER replies -- the real behavior observed for HVIDEO.
	}()

	start := time.Now()
	err := d.SendRawCommandFireAndForget(sess.IMEI, "HVIDEO,2026_09_16_14_43_21,0#")
	elapsed := time.Since(start)
	if err != nil {
		t.Fatalf("SendRawCommandFireAndForget error = %v, want nil", err)
	}
	if elapsed > 500*time.Millisecond {
		t.Errorf("SendRawCommandFireAndForget took %s, want it to return almost immediately (it must not wait for a reply)", elapsed)
	}

	select {
	case got := <-deviceGotCh:
		if !strings.Contains(got, "HVIDEO,2026_09_16_14_43_21,0#") {
			t.Errorf("device received %q, want it to contain the literal command", got)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("the device never received the frame -- SendRawCommandFireAndForget did not write to the socket")
	}
}

// TestDispatcher_SendRawCommandFireAndForget_BusyWhilePending confirms that
// even though it does not wait for a reply, it STILL registers sess.pending
// -- the F1 protection (a late reply to one command wrongly resolving a
// different one sent later) must not be lost just because this method does
// not block the caller.
func TestDispatcher_SendRawCommandFireAndForget_BusyWhilePending(t *testing.T) {
	registry := NewRegistry()
	server, client := net.Pipe()
	defer server.Close()
	defer client.Close()

	sess := &connSession{
		IMEI:          "868720063843126",
		DeviceID:      uuid.New(),
		TenantID:      uuid.New(),
		Authenticated: true,
		conn:          server,
	}
	registry.Register(sess.IMEI, sess)
	d := NewDispatcher(registry)

	go func() {
		buf := make([]byte, 256)
		for {
			if _, err := client.Read(buf); err != nil {
				return
			}
			// Never replies.
		}
	}()

	if err := d.SendRawCommandFireAndForget(sess.IMEI, "HVIDEO,2026_09_16_14_43_21,0#"); err != nil {
		t.Fatalf("first SendRawCommandFireAndForget error = %v, want nil", err)
	}

	err := d.SendRawCommandFireAndForget(sess.IMEI, "HVIDEO,2026_09_16_14_44_21,0#")
	if !errors.Is(err, commands.ErrCommandBusy) {
		t.Errorf("second SendRawCommandFireAndForget error = %v, want ErrCommandBusy", err)
	}
}

func TestDispatcher_SendRawCommand_DeviceNotConnected(t *testing.T) {
	registry := NewRegistry()
	d := NewDispatcher(registry)
	_, err := d.SendRawCommand(context.Background(), "not-registered", "any-text", time.Second)
	if !errors.Is(err, commands.ErrDeviceNotConnected) {
		t.Errorf("err = %v, want wrapping ErrDeviceNotConnected", err)
	}
}

func TestDispatcher_SendCommand_BusyRejectsConcurrentCommand(t *testing.T) {
	registry := NewRegistry()
	server, client := net.Pipe()
	defer server.Close()
	defer client.Close()

	sess := &connSession{IMEI: "868720063843126", Authenticated: true, conn: server}
	registry.Register(sess.IMEI, sess)
	d := NewDispatcher(registry)
	runFakeServerReadLoop(sess, server)

	firstDone := make(chan struct{})
	go func() {
		_, _ = d.SendCommand(context.Background(), sess.IMEI, "engine_stop", 2*time.Second)
		close(firstDone)
	}()

	// Synchronize with the first in-flight command: block until SendCommand
	// actually wrote the frame (which only happens after setting
	// sess.pending), so the second attempt is deterministically concurrent
	// with the first, not a race.
	buf := make([]byte, 256)
	n, err := client.Read(buf)
	if err != nil {
		t.Fatalf("reading the first command: %v", err)
	}
	pf := parseFrame(buf[:n])

	_, err = d.SendCommand(context.Background(), sess.IMEI, "engine_resume", time.Second)
	if !errors.Is(err, commands.ErrCommandBusy) {
		t.Errorf("err = %v, want errors.Is(err, commands.ErrCommandBusy)", err)
	}

	// Release the first command so the goroutine does not hang.
	if _, err := client.Write(buildCommandReplyFrame("DYD=Success!", pf.Serial)); err != nil {
		t.Fatalf("replying to the first command: %v", err)
	}
	<-firstDone
}

// TestDispatcher_SendCommand_TimeoutThenLateReplyNeverLeaksIntoNextCommand
// reproduces EXACTLY a real security review finding (F2): without the fix, a
// 0x15 reply arriving AFTER the caller timed out could resolve the NEXT,
// different command as its own reply -- for a command that cuts fuel to a
// real vehicle, "engine resume" could be reported as successful using the
// late reply to "engine stop", without the device ever confirming the
// resume. See connSession.pending (session.go) for the design that prevents
// it: pending is NEVER cleared just because the caller's timeout expired,
// only when its own real reply arrives.
func TestDispatcher_SendCommand_TimeoutThenLateReplyNeverLeaksIntoNextCommand(t *testing.T) {
	registry := NewRegistry()
	server, client := net.Pipe()
	defer server.Close()
	defer client.Close()

	sess := &connSession{IMEI: "868720063843126", Authenticated: true, conn: server}
	registry.Register(sess.IMEI, sess)
	d := NewDispatcher(registry)
	runFakeServerReadLoop(sess, server)

	// The "device" reads the first command but deliberately takes longer than
	// the caller's timeout to reply -- exactly the real scenario (the
	// device's own speed/GPS-fix guardrail, section 6.4, can delay the
	// reply).
	firstCommandSerial := make(chan uint16, 1)
	go func() {
		buf := make([]byte, 256)
		n, err := client.Read(buf)
		if err != nil {
			return
		}
		pf := parseFrame(buf[:n])
		firstCommandSerial <- pf.Serial
		time.Sleep(150 * time.Millisecond) // slower than the timeout below
		_, _ = client.Write(buildCommandReplyFrame("DYD=Success!", pf.Serial))
	}()

	_, err := d.SendCommand(context.Background(), sess.IMEI, "engine_stop", 30*time.Millisecond)
	if !errors.Is(err, commands.ErrCommandTimeout) {
		t.Fatalf("err = %v, want errors.Is(err, commands.ErrCommandTimeout)", err)
	}
	<-firstCommandSerial

	// pending must NOT have been cleared by the caller's timeout -- the real
	// reply may still arrive.
	sess.mu.Lock()
	pendingAfterTimeout := sess.pending
	sess.mu.Unlock()
	if pendingAfterTimeout == nil {
		t.Fatal("sess.pending was cleared when the caller's timeout expired -- reopens finding F2 (a late reply could resolve the next command)")
	}

	// While the real reply is still in flight, a second command must be
	// rejected as "busy" -- it must NEVER start and wait on a channel the
	// late reply of the FIRST could resolve by mistake.
	_, err = d.SendCommand(context.Background(), sess.IMEI, "engine_resume", 10*time.Millisecond)
	if !errors.Is(err, commands.ErrCommandBusy) {
		t.Fatalf("second command while the late reply was in flight: err = %v, want ErrCommandBusy", err)
	}

	// Wait for the late real reply to arrive and clear pending.
	deadline := time.After(2 * time.Second)
	for {
		sess.mu.Lock()
		cleared := sess.pending == nil
		sess.mu.Unlock()
		if cleared {
			break
		}
		select {
		case <-deadline:
			t.Fatal("the late reply never cleared sess.pending")
		case <-time.After(5 * time.Millisecond):
		}
	}

	// NOW that the first command finally resolved with its own real reply,
	// a new command must work normally -- and, the point of this test, with
	// ITS OWN reply, never the previous command's late one (already consumed
	// above, it cannot reappear).
	go func() {
		buf := make([]byte, 256)
		n, err := client.Read(buf)
		if err != nil {
			return
		}
		pf := parseFrame(buf[:n])
		_, _ = client.Write(buildCommandReplyFrame("HFYD=Success!", pf.Serial))
	}()
	reply, err := d.SendCommand(context.Background(), sess.IMEI, "engine_resume", time.Second)
	if err != nil {
		t.Fatalf("command after the late resolution: err = %v, want nil", err)
	}
	if reply != "HFYD=Success!" {
		t.Errorf("reply = %q, want %q (its own reply, not the previous command's late one)", reply, "HFYD=Success!")
	}
}
