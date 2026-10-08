package alarmclip

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

const testIMEI = "490154203237518"

func fastPhotoTimers(t *testing.T) {
	t.Helper()
	oc, ob, ou := photoCollectWindow, photoBusyBackoff, photoUploadTimeout
	photoCollectWindow = 20 * time.Millisecond
	photoBusyBackoff = []time.Duration{10 * time.Millisecond, 10 * time.Millisecond}
	t.Cleanup(func() { photoCollectWindow, photoBusyBackoff, photoUploadTimeout = oc, ob, ou })
}

var fakeJPEG = append([]byte{0xFF, 0xD8, 0xFF, 0xE0}, []byte("test photo")...)

// uploadPhoto simulates the device uploading a photo via multipart, the same
// way it uploads clips (filename/timestamp/sign + file fields).
func uploadPhoto(t *testing.T, b *Bridge, fileName string, data []byte) int {
	t.Helper()
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	_ = mw.WriteField("filename", fileName)
	_ = mw.WriteField("timestamp", "1790284350531")
	_ = mw.WriteField("sign", "x")
	fw, _ := mw.CreateFormFile("file", fileName)
	_, _ = fw.Write(data)
	_ = mw.Close()
	req := httptest.NewRequest(http.MethodPost, "/upload/"+testIMEI, &buf)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	req.SetPathValue("imei", testIMEI)
	rec := httptest.NewRecorder()
	b.handleUpload(rec, req)
	return rec.Code
}

func TestCapturePhoto_SendsNativeCommandAndReturnsUploadedPhoto(t *testing.T) {
	fastPhotoTimers(t)
	sender := &fakeGT06Sender{hasActiveSession: true}
	// pool is nil on purpose: if the photo fell into the clip logic (which
	// queries the database), the test would panic.
	b := &Bridge{gt06: sender, photos: newPhotoRequests()}
	sender.onRaw = func(imei, text string) {
		time.Sleep(20 * time.Millisecond)
		uploadPhoto(t, b, "PHOTO_490154203237518_F.jpg", fakeJPEG)
	}
	data, err := b.CapturePhoto(context.Background(), testIMEI, 0)
	if err != nil {
		t.Fatalf("CapturePhoto: %v", err)
	}
	if !bytes.Equal(data, fakeJPEG) {
		t.Fatalf("the delivered photo is not the one the device uploaded")
	}
	if len(sender.raw) != 1 || sender.raw[0] != "Picture,out#" {
		t.Fatalf("command sent = %v, expected [Picture,out#]", sender.raw)
	}
}

func TestCapturePhoto_CabinUsesInCommand(t *testing.T) {
	fastPhotoTimers(t)
	sender := &fakeGT06Sender{hasActiveSession: true}
	b := &Bridge{gt06: sender, photos: newPhotoRequests()}
	sender.onRaw = func(imei, text string) { uploadPhoto(t, b, "", fakeJPEG) } // no name: recognized by the JPEG bytes
	if _, err := b.CapturePhoto(context.Background(), testIMEI, 1); err != nil {
		t.Fatalf("CapturePhoto: %v", err)
	}
	if sender.raw[0] != "Picture,in#" {
		t.Fatalf("channel 1 (cabin) should send Picture,in#, sent %q", sender.raw[0])
	}
}

func TestCapturePhoto_TimesOutWhenDeviceNeverUploads(t *testing.T) {
	fastPhotoTimers(t)
	orig := photoUploadTimeout
	photoUploadTimeout = 50 * time.Millisecond
	t.Cleanup(func() { photoUploadTimeout = orig })
	b := &Bridge{gt06: &fakeGT06Sender{hasActiveSession: true}, photos: newPhotoRequests()}
	if _, err := b.CapturePhoto(context.Background(), testIMEI, 0); !errors.Is(err, ErrPhotoNotDelivered) {
		t.Fatalf("without an upload it should return ErrPhotoNotDelivered, returned %v", err)
	}
}

// Both cameras requested at once (the panel shows front and cabin): ONE
// "Picture,inout#" command (requesting them separately made a real JC261
// answer "busy" to the second), and each photo goes to its channel by the
// I/F letter in the real file name.
func TestCapturePhoto_BothChannelsOneCommandEachGetsItsPhoto(t *testing.T) {
	fastPhotoTimers(t)
	sender := &fakeGT06Sender{hasActiveSession: true}
	b := &Bridge{gt06: sender, photos: newPhotoRequests()}
	front := append([]byte{0xFF, 0xD8, 0xFF}, []byte("front")...)
	cabin := append([]byte{0xFF, 0xD8, 0xFF}, []byte("cabin")...)
	sender.onRaw = func(imei, text string) {
		time.Sleep(10 * time.Millisecond)
		// The cabin photo arrives first, as with the real device.
		uploadPhoto(t, b, "CMD_490154203237518_00000000_2026_09_25_09_18_59_I_10.jpg", cabin)
		uploadPhoto(t, b, "CMD_490154203237518_00000000_2026_09_25_09_18_59_F_11.jpg", front)
	}
	var wg sync.WaitGroup
	got := make([][]byte, 2)
	for ch := 0; ch < 2; ch++ {
		wg.Add(1)
		go func(ch int) {
			defer wg.Done()
			d, err := b.CapturePhoto(context.Background(), testIMEI, uint8(ch))
			if err != nil {
				t.Errorf("channel %d: %v", ch, err)
			}
			got[ch] = d
		}(ch)
	}
	wg.Wait()
	if !bytes.Equal(got[0], front) || !bytes.Equal(got[1], cabin) {
		t.Fatalf("each channel should receive its own photo: %q / %q", got[0], got[1])
	}
	if len(sender.raw) != 1 || sender.raw[0] != "Picture,inout#" {
		t.Fatalf("expected ONE Picture,inout# command, sent %v", sender.raw)
	}
}

// "busy" (the camera is still busy): short retry, never wait 20 s for a
// photo that will not arrive.
func TestCapturePhoto_RetriesOnBusy(t *testing.T) {
	fastPhotoTimers(t)
	sender := &busySender{fakeGT06Sender: fakeGT06Sender{hasActiveSession: true}, busyReplies: 2}
	b := &Bridge{gt06: sender, photos: newPhotoRequests()}
	sender.b, sender.t = b, t
	d, err := b.CapturePhoto(context.Background(), testIMEI, 0)
	if err != nil || !bytes.Equal(d, fakeJPEG) {
		t.Fatalf("after 2 busy replies the photo should arrive: %v", err)
	}
	if sender.calls != 3 {
		t.Fatalf("expected 3 sends (2 busy + 1 ok), got %d", sender.calls)
	}
}

func TestCapturePhoto_RejectionFailsFast(t *testing.T) {
	fastPhotoTimers(t)
	photoUploadTimeout = time.Hour // if it waited for the photo, the test would hang
	sender := &busySender{fakeGT06Sender: fakeGT06Sender{hasActiveSession: true}, reply: "ERROR"}
	b := &Bridge{gt06: sender, photos: newPhotoRequests()}
	start := time.Now()
	if _, err := b.CapturePhoto(context.Background(), testIMEI, 1); err == nil {
		t.Fatal("a non-OK reply should fail")
	}
	if time.Since(start) > 2*time.Second {
		t.Fatalf("a rejection should fail immediately, took %s", time.Since(start))
	}
}

type busySender struct {
	fakeGT06Sender
	busyReplies int
	reply       string
	calls       int
	b           *Bridge
	t           *testing.T
}

func (s *busySender) SendRawCommand(ctx context.Context, imei, text string, timeout time.Duration) (string, error) {
	s.calls++
	if s.reply != "" {
		return s.reply, nil
	}
	if s.calls <= s.busyReplies {
		return "busy", nil
	}
	go func() {
		time.Sleep(10 * time.Millisecond)
		uploadPhoto(s.t, s.b, "CMD_490154203237518_00000000_2026_09_25_09_18_59_F_10.jpg", fakeJPEG)
	}()
	return "PICTURE:OK!", nil
}

func TestPhotoChannelFromFileName(t *testing.T) {
	cases := map[string]int{
		"CMD_490154203237518_00000000_2026_09_25_09_18_59_I_10.jpg": 1,
		"CMD_490154203237518_00000000_2026_09_25_09_18_59_F_10.jpg": 0,
		"":          -1,
		"photo.jpg": -1,
	}
	for name, want := range cases {
		if got := photoChannelFromFileName(name); got != want {
			t.Errorf("photoChannelFromFileName(%q) = %d, want %d", name, got, want)
		}
	}
}

func TestUnsolicitedPhotoIsDiscardedWithoutTouchingClips(t *testing.T) {
	// nil pool: if an unsolicited photo fell into the clip logic, it would panic.
	b := &Bridge{gt06: &fakeGT06Sender{hasActiveSession: true}, photos: newPhotoRequests()}
	if code := uploadPhoto(t, b, "PHOTO.jpg", fakeJPEG); code != http.StatusOK {
		t.Fatalf("status = %d, want 200", code)
	}
}

func TestLooksLikePhoto(t *testing.T) {
	cases := []struct {
		name string
		head []byte
		want bool
	}{
		{"x.JPG", nil, true},
		{"x.png", nil, true},
		{"EVENT_1_00000000_2026_09_16_08_10_10_F_23.ts", []byte{0x47, 0x40, 0x00, 0x10}, false},
		{"", []byte{0xFF, 0xD8, 0xFF, 0xE0}, true},
		{"", []byte{0x89, 0x50, 0x4E, 0x47}, true},
		{"", []byte{0x47, 0x40, 0x00, 0x10}, false},
	}
	for _, c := range cases {
		if got := looksLikePhoto(c.name, c.head); got != c.want {
			t.Errorf("looksLikePhoto(%q, % x) = %v, want %v", c.name, c.head, got, c.want)
		}
	}
}

// A photo arriving without an open request but shortly after a "Picture" is
// the late reply (the JC261 front camera sometimes takes 30 s+): it is used
// for the preview. An old photo the device retries much later is still
// discarded.
func TestLateTarget(t *testing.T) {
	p := newPhotoRequests()
	name := "CMD_490154203237518_00000000_2026_09_25_10_52_46_F_09.jpg"
	if _, ok := p.lateTarget("490154203237518", name); ok {
		t.Fatal("without any Picture sent it must not be accepted")
	}
	p.lastAttempt["490154203237518"] = time.Now()
	if ch, ok := p.lateTarget("490154203237518", name); !ok || ch != 0 {
		t.Fatalf("late front: ch=%d ok=%v, want 0 true", ch, ok)
	}
	if ch, ok := p.lateTarget("490154203237518", "CMD_490154203237518_00000000_2026_09_25_10_52_46_I_10.jpg"); !ok || ch != 1 {
		t.Fatalf("late cabin: ch=%d ok=%v, want 1 true", ch, ok)
	}
	if _, ok := p.lateTarget("490154203237518", "photo.jpg"); ok {
		t.Fatal("without a channel letter the camera cannot be known")
	}
	p.lastAttempt["490154203237518"] = time.Now().Add(-2 * photoLateWindow)
	if _, ok := p.lateTarget("490154203237518", name); ok {
		t.Fatal("outside the window it must be discarded")
	}
}

// With the camera's time zone learned (real log: local time UTC-7), a freshly
// taken photo arriving late is used even if the last Picture was more than
// 90 s ago, and an old photo the camera retries 16 min later is discarded.
func TestLateTarget_UsesCameraClock(t *testing.T) {
	p := newPhotoRequests()
	imei := "490154203237518"
	now := time.Now().UTC()
	local := func(t time.Time) string {
		l := t.Add(-7 * time.Hour)
		return fmt.Sprintf("CMD_%s_00000000_%s_F_09.jpg", imei, l.Format("2006_01_02_15_04_05"))
	}
	p.mu.Lock()
	p.learnOffsetLocked(imei, local(now.Add(-3*time.Second)))
	p.mu.Unlock()
	if off := p.tzOffset[imei]; off != -7*time.Hour {
		t.Fatalf("learned zone = %s, want -7h", off)
	}
	if _, ok := p.lateTarget(imei, local(now.Add(-4*time.Second))); !ok {
		t.Fatal("a 4 s old photo should be used")
	}
	if _, ok := p.lateTarget(imei, local(now.Add(-16*time.Minute))); ok {
		t.Fatal("a 16 min old photo should be discarded")
	}
}

// After a failed request no other Picture is sent to the camera during the
// cooldown: the request answers immediately without touching the device.
func TestCapturePhoto_CooldownAfterFailure(t *testing.T) {
	b := &Bridge{gt06: &fakeGT06Sender{hasActiveSession: true}, photos: newPhotoRequests()}
	b.photos.cooldownUntil["490154203237518"] = time.Now().Add(time.Hour)
	start := time.Now()
	_, err := b.CapturePhoto(context.Background(), "490154203237518", 0)
	if err != ErrPhotoNotDelivered || time.Since(start) > 100*time.Millisecond {
		t.Fatalf("err=%v after %s, want immediate ErrPhotoNotDelivered", err, time.Since(start))
	}
}
