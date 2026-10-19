package alarmclip

// Native photo for JC261/JC400 dashcams: instead of opening the camera's
// RTMP stream and grabbing a frame with ZLMediaKit's getSnap (several seconds
// of cellular video plus the full negotiation), the device is asked for ONE
// photo with its native command and uploads it itself -- a few KB instead of
// hundreds, without touching the video server.
//
// Protocol, confirmed against two independent sources (a vendor command
// reference library and integrator field reports for JC400/JC261):
//   - Command over the authenticated GT06 connection: "Picture,out#" (front
//     camera), "Picture,in#" (cabin). The device replies with the text
//     "PICTURE".
//   - The photo does NOT travel over the TCP connection: the device uploads
//     it over HTTP multipart to the server configured with UPLOAD -- the SAME
//     /upload/{imei} endpoint that receives event clips (handleUpload).
//
// Confirmed on a physical JC261: it replies "PICTURE:OK!", uploads the photo
// in ~1.2 s (~25 KB) and names it CMD_<imei>_<code>_<date>_<I|F>_<n>.jpg. If
// asked for a second photo while still processing the first, it replies
// "busy" -- so near-simultaneous requests for the same device are merged into
// ONE command ("Picture,inout#" for both cameras), "busy" is retried after a
// short wait, and each photo is delivered by the I/F letter in its name (or
// to the first pending channel if the name lacks it).

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// photoUploadTimeout: how long to wait for the photo upload after the device
// accepted the command. Measured on a real JC261: the cabin photo arrives in
// ~1-3 s but the front one sometimes takes 30 s or more. With 20 s it was
// given up and the expensive video path was used; now it waits longer and,
// if it arrives even later, it is still used (see photoLateWindow /
// Bridge.SetPhotoSink).
var photoUploadTimeout = 30 * time.Second

// photoLateWindow: a photo arriving without an open request but within this
// time after the last "Picture" sent to that device is the late reply to that
// request -- it goes to the sink (preview cache) instead of being discarded.
// Outside this window (the device retrying an old photo) it is discarded.
var photoLateWindow = 90 * time.Second

// photoFailCooldown: after a failed request (the camera did not upload
// everything, did not answer, or stayed "busy") no other "Picture" is sent to
// that device for a while. Observed on real hardware: page retries sent one
// Picture after another while the camera was still busy uploading the front
// photo; it then answered "busy" in a burst ~1.5 min later and everything
// stalled. During the cooldown the request answers "no photo" immediately,
// and a photo arriving late lands in the cache for the next attempt.
var photoFailCooldown = 20 * time.Second

var photoFilenameTimePattern = regexp.MustCompile(`(?i)_(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_[IF]_\d+\.jpe?g$`)

// photoLocalTime is the capture time the camera puts in the photo's name, in
// ITS local time (returned with a UTC zone, unconverted).
func photoLocalTime(name string) (time.Time, bool) {
	m := photoFilenameTimePattern.FindStringSubmatch(name)
	if m == nil {
		return time.Time{}, false
	}
	v := make([]int, 6)
	for i, s := range m[1:] {
		n, err := strconv.Atoi(s)
		if err != nil {
			return time.Time{}, false
		}
		v[i] = n
	}
	if v[1] < 1 || v[1] > 12 || v[2] < 1 || v[2] > 31 || v[3] > 23 || v[4] > 59 || v[5] > 59 {
		return time.Time{}, false
	}
	return time.Date(v[0], time.Month(v[1]), v[2], v[3], v[4], v[5], 0, time.UTC), true
}

// photoCollectWindow: requests for the same camera arriving within this
// window are merged into ONE command (the panel asks for front and cabin at
// once -> "Picture,inout#"). Measured on a real JC261: requesting them
// separately made the second one get "busy" (still processing the first) and
// fall back to the expensive video path.
var photoCollectWindow = 350 * time.Millisecond

// Retries on "busy" (the camera is still processing something): short wait
// and retry, instead of waiting for a photo that will never arrive. Observed
// on real hardware: after a photo the JC261 stayed "busy" for ~10 s, so the
// last attempt covers up to ~17 s after the first.
var photoBusyBackoff = []time.Duration{1500 * time.Millisecond, 3 * time.Second, 5 * time.Second, 8 * time.Second}

// maxPhotoBytes caps a photo (a real JC261 photo is ~25 KB).
const maxPhotoBytes = 5 << 20

// ErrPhotoNotDelivered: the photo never arrived -- the caller may fall back
// to video capture.
var ErrPhotoNotDelivered = errors.New("alarmclip: the camera did not upload the photo in time")

type photoResult struct {
	data []byte
	err  error
}

// photoSession: ONE Picture command for one or both cameras of a device.
type photoSession struct {
	sent     bool
	channels map[uint8]bool
	waiters  map[uint8][]chan photoResult
	pending  map[uint8]bool // channels whose photo has not arrived yet
	complete chan struct{}  // closed when all have arrived
	done     chan struct{}  // closed when the session ends
	// Timings copied at session creation (the session goroutine never reads
	// package variables).
	collect       time.Duration
	uploadTimeout time.Duration
	backoff       []time.Duration
}

type photoRequests struct {
	mu       sync.Mutex
	sessions map[string]*photoSession
	lastSent map[string]time.Time // last "Picture" accepted per device
	// lastAttempt: last "Picture" SENT (even if unanswered or "busy": the
	// camera sometimes acts anyway).
	lastAttempt map[string]time.Time
	// tzOffset: difference between the camera's local time (from the photo
	// name) and real time, learned from photos delivered on time. Used to
	// tell how fresh a late photo is.
	tzOffset      map[string]time.Duration
	cooldownUntil map[string]time.Time
}

func newPhotoRequests() *photoRequests {
	return &photoRequests{
		sessions:      make(map[string]*photoSession),
		lastSent:      make(map[string]time.Time),
		lastAttempt:   make(map[string]time.Time),
		tzOffset:      make(map[string]time.Duration),
		cooldownUntil: make(map[string]time.Time),
	}
}

// lateTarget reports whether a photo without an open request is the late
// reply to a recent "Picture", and which channel it belongs to.
func (p *photoRequests) lateTarget(imei, fileName string) (channel uint8, ok bool) {
	ch := photoChannelFromFileName(fileName)
	if ch < 0 {
		return 0, false
	}
	now := time.Now()
	p.mu.Lock()
	offset, offsetKnown := p.tzOffset[imei]
	attempt, attempted := p.lastAttempt[imei]
	p.mu.Unlock()
	// With the camera's time zone learned, the photo's REAL age is measured:
	// a fresh one (< photoLateWindow) is used even if the request already
	// expired; an old one (the camera retrying an old photo, observed in the
	// field) is discarded.
	if local, parsed := photoLocalTime(fileName); parsed && offsetKnown {
		age := now.Sub(local.Add(-offset))
		return uint8(ch), age > -2*time.Minute && age <= photoLateWindow
	}
	// Without a learned zone: only if there was a recent Picture.
	if !attempted || now.Sub(attempt) > photoLateWindow {
		return 0, false
	}
	return uint8(ch), true
}

// learnOffsetLocked stores the camera's time zone from a photo known to be
// fresh (delivered to an open request), rounded to 15 min. Call with p.mu
// held.
func (p *photoRequests) learnOffsetLocked(imei, fileName string) {
	local, ok := photoLocalTime(fileName)
	if !ok {
		return
	}
	p.tzOffset[imei] = local.Sub(time.Now()).Round(15 * time.Minute)
}

// pictureCommand builds the native command for the set of platform channels
// (0 = front/"out", 1 = cabin/"in", the same convention as live video).
func pictureCommand(channels map[uint8]bool) (string, error) {
	switch {
	case channels[0] && channels[1]:
		return "Picture,inout#", nil
	case channels[0]:
		return "Picture,out#", nil
	case channels[1]:
		return "Picture,in#", nil
	default:
		return "", errors.New("alarmclip: no known camera for the photo")
	}
}

// photoChannelFromFileName: a real JC261 names the photo
// CMD_<imei>_<code>_<YYYY_MM_DD_HH_MM_SS>_<I|F>_<n>.jpg (I = cabin,
// F = front, confirmed on the physical device). -1 if not recognized.
func photoChannelFromFileName(name string) int {
	parts := strings.Split(strings.TrimSuffix(strings.ToUpper(name), ".JPG"), "_")
	for i := len(parts) - 1; i >= 0; i-- {
		switch parts[i] {
		case "F":
			return 0
		case "I":
			return 1
		}
	}
	return -1
}

// deliver hands an uploaded photo to that camera's open session: to the
// channel named in the file name, or to the first pending one if the name
// does not say.
func (p *photoRequests) deliver(imei, fileName string, data []byte) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	s := p.sessions[imei]
	if s == nil || !s.sent || len(s.pending) == 0 {
		return false
	}
	ch := photoChannelFromFileName(fileName)
	var target uint8
	switch {
	case ch >= 0 && s.pending[uint8(ch)]:
		target = uint8(ch)
	case ch < 0:
		if s.pending[0] {
			target = 0
		} else {
			target = 1
		}
	default:
		return false // photo for a channel this capture session did not request (or already arrived)
	}
	p.learnOffsetLocked(imei, fileName)
	for _, w := range s.waiters[target] {
		w <- photoResult{data: data}
	}
	delete(s.waiters, target)
	delete(s.pending, target)
	if len(s.pending) == 0 {
		close(s.complete)
	}
	return true
}

// CapturePhoto requests ONE native photo for a channel and waits for it.
// Near-simultaneous requests for the same device share a single command.
func (b *Bridge) CapturePhoto(ctx context.Context, imei string, channel uint8) ([]byte, error) {
	if channel > 1 {
		return nil, fmt.Errorf("alarmclip: channel %d has no known camera", channel)
	}
	if b.gt06 == nil || b.photos == nil {
		return nil, errors.New("alarmclip: native photo not configured")
	}
	p := b.photos
	for {
		p.mu.Lock()
		s := p.sessions[imei]
		if s == nil && time.Now().Before(p.cooldownUntil[imei]) {
			p.mu.Unlock()
			return nil, ErrPhotoNotDelivered
		}
		if s != nil && s.sent && !s.channels[channel] {
			// A command is in flight that does NOT include this channel:
			// wait for it to finish and request in the next session.
			done := s.done
			p.mu.Unlock()
			select {
			case <-done:
				continue
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		if s == nil {
			s = &photoSession{
				channels:      map[uint8]bool{},
				waiters:       map[uint8][]chan photoResult{},
				pending:       map[uint8]bool{},
				complete:      make(chan struct{}),
				done:          make(chan struct{}),
				collect:       photoCollectWindow,
				uploadTimeout: photoUploadTimeout,
				backoff:       append([]time.Duration(nil), photoBusyBackoff...),
			}
			p.sessions[imei] = s
			go b.runPhotoSession(imei, s)
		}
		if !s.sent {
			s.channels[channel] = true
			s.pending[channel] = true
		}
		w := make(chan photoResult, 1)
		s.waiters[channel] = append(s.waiters[channel], w)
		p.mu.Unlock()

		select {
		case r := <-w:
			return r.data, r.err
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
}

func (b *Bridge) runPhotoSession(imei string, s *photoSession) {
	p := b.photos
	time.Sleep(s.collect)

	p.mu.Lock()
	s.sent = true
	cmd, cmdErr := pictureCommand(s.channels)
	p.mu.Unlock()

	fail := func(err error) {
		p.mu.Lock()
		p.cooldownUntil[imei] = time.Now().Add(photoFailCooldown)
		for ch, ws := range s.waiters {
			for _, w := range ws {
				w <- photoResult{err: err}
			}
			delete(s.waiters, ch)
		}
		p.mu.Unlock()
	}
	defer func() {
		p.mu.Lock()
		if p.sessions[imei] == s {
			delete(p.sessions, imei)
		}
		p.mu.Unlock()
		close(s.done)
	}()

	if cmdErr != nil {
		fail(cmdErr)
		return
	}

	started := time.Now()
	var reply string
	var err error
	for attempt := 0; ; attempt++ {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		p.mu.Lock()
		p.lastAttempt[imei] = time.Now()
		p.mu.Unlock()
		reply, err = b.gt06.SendRawCommand(ctx, imei, cmd, 10*time.Second)
		cancel()
		busy := err == nil && strings.Contains(strings.ToLower(reply), "busy")
		if !busy || attempt >= len(s.backoff) {
			break
		}
		log.Printf("alarmclip: %s replied %q to %s, retrying in %s", imei, reply, cmd, s.backoff[attempt])
		time.Sleep(s.backoff[attempt])
	}
	if err != nil {
		fail(fmt.Errorf("alarmclip: sending %s to %s: %w", cmd, imei, err))
		return
	}
	if !strings.Contains(strings.ToUpper(reply), "OK") && !strings.EqualFold(strings.TrimSpace(reply), "PICTURE") {
		// Persistent "busy" or another refusal: the photo will not arrive,
		// no point waiting -- the caller falls back to video.
		log.Printf("alarmclip: %s rejected %s (reply=%q)", imei, cmd, reply)
		fail(fmt.Errorf("alarmclip: the camera rejected %s: %q", cmd, reply))
		return
	}
	log.Printf("alarmclip: %s sent to %s (reply=%q), waiting for the photo upload", cmd, imei, reply)
	p.mu.Lock()
	p.lastSent[imei] = time.Now()
	p.mu.Unlock()

	select {
	case <-s.complete:
		log.Printf("alarmclip: native photo(s) from %s received in %s (%s)", imei, time.Since(started).Round(100*time.Millisecond), cmd)
	case <-time.After(s.uploadTimeout):
		log.Printf("alarmclip: %s accepted %s but did not upload all photos within %s", imei, cmd, s.uploadTimeout)
		fail(ErrPhotoNotDelivered)
	}
}

// looksLikePhoto: is the uploaded file an image? By the name the device
// reports or, if it has no useful one, by magic bytes (JPEG FF D8 FF, PNG
// 89 50 4E 47) -- never by size.
func looksLikePhoto(fileName string, head []byte) bool {
	n := strings.ToLower(fileName)
	if strings.HasSuffix(n, ".jpg") || strings.HasSuffix(n, ".jpeg") || strings.HasSuffix(n, ".png") {
		return true
	}
	return bytes.HasPrefix(head, []byte{0xFF, 0xD8, 0xFF}) || bytes.HasPrefix(head, []byte{0x89, 0x50, 0x4E, 0x47})
}

func (b *Bridge) lateTarget(imei, fileName string) (uint8, bool) {
	if b.photos == nil {
		return 0, false
	}
	return b.photos.lateTarget(imei, fileName)
}
