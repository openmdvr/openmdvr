package gt06server

import (
	"context"
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/openmdvr/openmdvr/jt808-server/internal/commands"
)

// commandText translates the protocol-agnostic vocabulary (see
// internal/commands.Sender, device_commands.command_type) into real GT06
// text. engine_stop/engine_resume use the text from the primary protocol
// document (sections 6.4/6.5, "DYD#"/"HFYD#"). Many clones in practice
// expect "Relay,1#"/"Relay,0#" instead (a widely used open-source GT06
// encoder defaults to those and reserves DYD#/HFYD# for a specific model).
// If real hardware does not cut the engine with DYD#, change ONLY this table
// -- the same approach that resolved the real position protocol number
// discrepancy (classic 0x12 vs Wanway 0x22).
// request_video/stop_video (Jimi IoT JC261/JC400) start/stop the RTMP push;
// the text matches the vendor's own cloud API (proNo:128,
// cmdContent:RTMP,ON,INOUT), which sends this same command over the same
// GT06 channel. "INOUT" starts both cameras (front + cabin) as one push.
//
// Alarm clip retrieval commands (see internal/alarmclip) are not in this
// table: they need dynamic parameters, so they are built and sent with
// SendRawCommand* directly from internal/alarmclip.
var commandText = map[string]string{
	"engine_stop":   "DYD#",
	"engine_resume": "HFYD#",
	"request_video": "RTMP,ON,INOUT#",
	"stop_video":    "RTMP,OFF#",
}

// Dispatcher implements commands.Sender for GT06: it resolves an IMEI in the
// Registry, builds a real 0x80 frame and waits for the device's 0x15 reply on
// the SAME connection (see connSession.pending for why correlation is per
// connection rather than via the Server Flag Bit).
type Dispatcher struct {
	registry *Registry
}

func NewDispatcher(registry *Registry) *Dispatcher {
	return &Dispatcher{registry: registry}
}

// SendCommand implements commands.Sender -- fixed agnostic vocabulary (see
// commandText), no dynamic parameters.
func (d *Dispatcher) SendCommand(ctx context.Context, imei, commandType string, timeout time.Duration) (string, error) {
	text, known := commandText[commandType]
	if !known {
		return "", fmt.Errorf("gt06: unsupported command_type: %s", commandType)
	}
	return d.SendRawCommand(ctx, imei, text, timeout)
}

// SendRawCommand sends LITERAL text over the imei's authenticated GT06
// connection. It is deliberately outside the commands.Sender interface:
// some commands need dynamic parameters that do not fit the fixed symbolic
// commandText/command_type vocabulary. Used by the configuration command
// catalog (SERVER/APN/TIMEZONE/etc., see internal/commands.RawHandler) and
// native photo capture; engine and live video commands go through
// SendCommand. It includes the 4 zero bytes of Server Flag Bit (section 6.1
// of the protocol document) -- confirmed to work this way against real
// hardware for every configuration command tested.
func (d *Dispatcher) SendRawCommand(ctx context.Context, imei, text string, timeout time.Duration) (string, error) {
	return d.sendRawCommand(ctx, imei, text, timeout, true)
}

// SendRawCommandUnpadded is like SendRawCommand but WITHOUT the 4-byte Server
// Flag Bit. Field reports for this same hardware (JC261) indicate the
// firmware's video subsystem expects the ASCII text to start IMMEDIATELY
// after the length byte, without the 4-zero padding the generic command
// dispatcher tolerates (RTMP/SERVER/TIMER/DYD -- probably because those
// search for the keyword as a substring instead of requiring it at an exact
// offset). Kept for offset-sensitive video-subsystem commands.
func (d *Dispatcher) SendRawCommandUnpadded(ctx context.Context, imei, text string, timeout time.Duration) (string, error) {
	return d.sendRawCommand(ctx, imei, text, timeout, false)
}

// SendRawCommandFireAndForget sends the command and does NOT wait for a
// synchronous reply. Confirmed on real hardware: for clip retrieval commands
// (e.g. HVIDEO) the device NEVER answers on the normal command reply channel
// (0x15/0x21), even after 30s; per vendor protocol documentation the real
// confirmation arrives through a separate alarm sub-protocol (0x69, not
// implemented). Blocking the caller (the clip retrieval HTTP API) waiting for
// something that never arrives produced spurious "did not respond in time"
// errors even when the request worked -- today the only real success signal
// is the file arriving later via POST /upload/{imei}.
//
// It STILL registers sess.pending like sendRawCommand (to keep the F1
// protection: a late reply to THIS command must never resolve a DIFFERENT
// command sent later). Waiting for that possible reply runs in its own
// goroutine and never blocks the caller.
func (d *Dispatcher) SendRawCommandFireAndForget(imei, text string) error {
	_, p, err := writeCommandAndRegisterPending(d.registry, imei, text, true, fireAndForgetAbandonAfter)
	if err != nil {
		return err
	}
	go func() {
		select {
		case reply := <-p.ch:
			log.Printf("gt06: %s replied (late, out of band) to %q: %q", imei, text, reply)
		case <-time.After(maxAbandonedPendingAge):
		}
	}()
	return nil
}

// HasActiveSession reports whether a live AUTHENTICATED GT06 connection
// exists for this IMEI right now. It is an extra barrier for
// internal/alarmclip (the public clip upload endpoint, port 8083), which has
// NO other way to authenticate the caller beyond the IMEI in the path.
// Security review finding: without this check, anyone on the internet who
// only knew an IMEI (not secret -- printed on the hardware and sent in clear
// in the GT06 login) could overwrite/destroy THAT tenant's video clip with an
// anonymous curl, without ever opening a real TCP connection. Requiring a
// live authenticated session raises the attack from "one anonymous HTTP
// POST" to "forge the device's full GT06 login" -- the same trust threshold
// this protocol already accepts everywhere (GT06 has no cryptographic
// authentication).
func (d *Dispatcher) HasActiveSession(imei string) bool {
	sess, ok := d.registry.Get(imei)
	return ok && sess.Authenticated
}

func (d *Dispatcher) sendRawCommand(ctx context.Context, imei, text string, timeout time.Duration, includeServerFlagBit bool) (string, error) {
	_, p, err := writeCommandAndRegisterPending(d.registry, imei, text, includeServerFlagBit, timeout)
	if err != nil {
		return "", err
	}

	select {
	case reply := <-p.ch:
		return reply, nil
	case <-time.After(timeout):
		// Deliberately do NOT clear sess.pending here -- the command WAS
		// written to the socket and the device may still answer at any time
		// (its own speed/fix guardrail, section 6.4, can delay the reply
		// beyond what this caller is willing to wait). Keeping it pending
		// until the real reply arrives (or maxAbandonedPendingAge) is what
		// prevents a later attempt from mistaking that late reply for its
		// own.
		return "", fmt.Errorf("gt06: %w", commands.ErrCommandTimeout)
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

// writeCommandAndRegisterPending is the part SHARED by sendRawCommand (waits
// for the reply, blocking the caller) and SendRawCommandFireAndForget (does
// not wait; a reply, if any, is resolved in the background). It looks up the
// session, registers a new pendingCommand (rejecting with ErrCommandBusy if a
// live one exists, see the F2 finding) and writes the frame to the socket. It
// returns the session and the registered pendingCommand.
func writeCommandAndRegisterPending(registry *Registry, imei, text string, includeServerFlagBit bool, callerTimeout time.Duration) (*connSession, *pendingCommand, error) {
	sess, ok := registry.Get(imei)
	if !ok || !sess.Authenticated {
		return nil, nil, fmt.Errorf("gt06: %w", commands.ErrDeviceNotConnected)
	}

	serial := sess.nextSerial()

	sess.mu.Lock()
	// An existing pending command can only be "reclaimed" once it is old
	// enough to assume its own reply will never arrive (see
	// maxAbandonedPendingAge in session.go), or via the keyword-checked
	// handover in canTakeOver. This prevents finding F2: there is never a
	// window where two different commands share the same reply expectation
	// on this connection.
	keyword := commandKeyword(text)
	now := time.Now()
	if sess.pending != nil && now.Sub(sess.pending.createdAt) < maxAbandonedPendingAge && !canTakeOver(sess.pending, keyword, now) {
		sess.mu.Unlock()
		return nil, nil, fmt.Errorf("gt06: %w", commands.ErrCommandBusy)
	}
	p := &pendingCommand{ch: make(chan string, 1), createdAt: now, keyword: keyword, abandonAt: now.Add(callerTimeout)}
	sess.pending = p
	sess.mu.Unlock()

	frame := encodeCommandFrame(text, serial, includeServerFlagBit)
	if err := sess.writeFrame(frame, writeTimeout); err != nil {
		// Cleared here on purpose -- the write itself failed, NOTHING left
		// through the socket, so no real reply can arrive late and be
		// confused with a future command.
		sess.mu.Lock()
		if sess.pending == p {
			sess.pending = nil
		}
		sess.mu.Unlock()
		return nil, nil, fmt.Errorf("gt06: writing command: %w", err)
	}

	return sess, p, nil
}

// encodeCommandFrame builds a 0x80 frame (section 6.1 of the protocol
// document): Length of Command (1 byte, = [4 bytes of Server Flag Bit if
// includeServerFlagBit] + N content bytes + 2 language bytes -- matches the
// length formula of a widely used open-source GT06 encoder byte for byte
// with includeServerFlagBit=true) + Server Flag Bit (4, always zero -- see
// connSession.pending for why it is not used for correlation; omitted when
// includeServerFlagBit is false) + ASCII content + language (2, English).
// It reuses encodeFrame (encode.go) as is, without duplicating the
// framing/CRC arithmetic.
//
// includeServerFlagBit=false exists for SendRawCommandUnpadded -- see its
// doc comment.
func encodeCommandFrame(text string, serial uint16, includeServerFlagBit bool) []byte {
	content := []byte(text)
	flagBitLen := 0
	if includeServerFlagBit {
		flagBitLen = 4
	}
	lengthOfCommand := byte(flagBitLen + len(content) + 2)
	payload := make([]byte, 0, 1+flagBitLen+len(content)+2)
	payload = append(payload, lengthOfCommand)
	if includeServerFlagBit {
		payload = append(payload, 0, 0, 0, 0) // Server Flag Bit
	}
	payload = append(payload, content...)
	payload = append(payload, 0x00, 0x02) // language: English (section 6.1.7)
	return encodeFrame(protoCommand, payload, serial)
}

// handleCommandReply processes a command reply -- either 0x15 (section 6.2
// of the protocol document: Length of Command(1) + Server Flag Bit(4) + ASCII
// content + language(2)) or 0x21 (protoCommandReplyJC261, a different layout
// NOT documented in the protocol document -- found on real hardware, see the
// constant in handlers.go). Instead of assuming a fixed offset (valid for
// only ONE of the two layouts), it extracts the first run of printable ASCII
// from the whole payload -- this works for both without distinguishing the
// protocol number, and is robust to future firmware with yet another layout.
//
// It resolves this connection's pending command, if any. A reply without
// recognizable text (noise, corrupt payload) NEVER clears sess.pending --
// that would leave a real command unable to resolve if its legitimate reply
// arrives later. A reply WITH text but nothing pending (arrived late, after
// SendCommand timed out) is logged and discarded, never breaking the
// connection.
func handleCommandReply(remote string, sess *connSession, pf parsedFrame) {
	text := extractPrintableASCII(pf.Payload)
	if text == "" {
		log.Printf("gt06: %s: 0x%02X without recognizable text content, ignored (may be noise)", remote, pf.ProtocolNumber)
		return
	}

	sess.mu.Lock()
	p := sess.pending
	if p != nil && isStrayReply(p, text) {
		// LATE reply to another command (tagged with another command's
		// keyword, see isStrayReply): it never resolves the current command.
		sess.mu.Unlock()
		log.Printf("gt06: %s: late reply from another command discarded (pending=%s) imei=%s: %q", remote, p.keyword, sess.IMEI, text)
		return
	}
	sess.pending = nil
	sess.mu.Unlock()

	if p == nil {
		log.Printf("gt06: %s: 0x%02X received with no pending command for imei=%s, ignored: %q", remote, pf.ProtocolNumber, sess.IMEI, text)
		return
	}
	select {
	case p.ch <- text:
	default:
	}
}

// --- Safe command-slot handover -----------------------------------------
//
// There is ONE slot per connection (the protocol carries no reliable
// correlation ID, see connSession.pending). Previously an unanswered command
// occupied it for maxAbandonedPendingAge (2 min) no matter what: if the
// camera did not answer an RTMP,OFF, nobody could view video or request a
// photo for 2 minutes.
//
// The JC261 DOES tag its replies: "RTMP:OK!", "PICTURE:OK!", "DYD=Success!",
// "HFYD=...". That allows isolation by CONTENT in addition to time, without
// reopening finding F1 (a late reply wrongly confirming ANOTHER command):
//   - isStrayReply: a reply tagged with ANOTHER known command is discarded
//     and never resolves the current pending command.
//   - canTakeOver: a new command may take the slot from a pending one ONLY
//     if the pending command's caller already gave up (abandonAt), its
//     replies are always tagged (so its late reply is recognized and
//     discarded), and NEVER between two engine commands -- the exact F1
//     scenario stays as strict as before.
// An UNTAGGED reply (e.g. "busy") still resolves the current pending
// command; for an engine command that is never "success"
// (device_commands.py requires "success" in the text), so the worst case is
// marking it failed.

// fireAndForgetAbandonAfter: for SendRawCommandFireAndForget (nobody waits
// for the reply), when it counts as abandoned for handover purposes.
const fireAndForgetAbandonAfter = 15 * time.Second

// Commands whose replies ALWAYS carry their own tag (confirmed against a
// real JC261 and the Concox protocol document).
var videoKeywords = map[string]bool{"RTMP": true, "PICTURE": true}
var engineKeywords = map[string]bool{"DYD": true, "HFYD": true}

// commandKeyword: "RTMP,ON,INOUT#" -> "RTMP", "DYD#" -> "DYD".
func commandKeyword(text string) string {
	end := strings.IndexAny(text, ",#")
	if end < 0 {
		end = len(text)
	}
	return strings.ToUpper(strings.TrimSpace(text[:end]))
}

// replyKeyword: "RTMP:OK!" -> "RTMP", "DYD=Success!" -> "DYD", "busy" -> "BUSY".
func replyKeyword(text string) string {
	t := strings.TrimSpace(text)
	end := strings.IndexAny(t, "=:, ")
	if end < 0 {
		end = len(t)
	}
	return strings.ToUpper(t[:end])
}

func tagged(kw string) bool { return videoKeywords[kw] || engineKeywords[kw] }

func isStrayReply(p *pendingCommand, text string) bool {
	kw := replyKeyword(text)
	return tagged(kw) && kw != p.keyword
}

func canTakeOver(old *pendingCommand, newKeyword string, now time.Time) bool {
	if now.Before(old.abandonAt) || !tagged(old.keyword) || !tagged(newKeyword) {
		return false
	}
	if engineKeywords[old.keyword] && engineKeywords[newKeyword] {
		return false // F1: never between engine commands
	}
	// Video<->video (same keyword included: a mix-up there carries no
	// physical risk), video<->engine in either direction (different
	// keywords: the late reply is recognized and discarded).
	return true
}

// extractPrintableASCII returns the first run of printable ASCII bytes
// (0x20-0x7E) in the payload, or "" if there is none. It stops at the first
// non-printable byte after a run has started, so a binary suffix (the 2-byte
// language field in the 0x15 layout, or any other padding) never leaks into
// the returned text.
func extractPrintableASCII(payload []byte) string {
	start := -1
	for i, b := range payload {
		if b >= 0x20 && b <= 0x7E {
			if start == -1 {
				start = i
			}
			continue
		}
		if start != -1 {
			return string(payload[start:i])
		}
	}
	if start == -1 {
		return ""
	}
	return string(payload[start:])
}
