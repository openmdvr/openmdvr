package alarmclip

import (
	"bytes"
	"context"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// TestExtractUploadedFile_MultipartFormData is a regression test: the first
// clip uploaded by a real JC261 arrived as multipart/form-data
// ("filename"/"timestamp"/"sign" fields + the real file in a "file" field)
// instead of a raw body -- without this, the whole multipart envelope was
// stored as the .ts. The test reproduces the SAME real shape (field order,
// "file" field with its own filename/Content-Type) captured from hardware.
func TestExtractUploadedFile_MultipartFormData(t *testing.T) {
	const fileContent = "fake binary content of a .ts clip"

	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	must(t, mw.WriteField("filename", "EVENT_490154203237518_00000000_2026_09_17_06_40_08_F_23.ts"))
	must(t, mw.WriteField("timestamp", "1789652420763"))
	must(t, mw.WriteField("sign", "MDQ4ODAwMjRhNDkyMDM4NzZkZDNkOGRmNDBlMDAxZGM="))
	fw, err := mw.CreateFormFile("file", "2026_09_17_06_40_08_F_23.ts")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := fw.Write([]byte(fileContent)); err != nil {
		t.Fatal(err)
	}
	must(t, mw.Close())

	req := httptest.NewRequest(http.MethodPost, "/upload/490154203237518", &buf)
	req.Header.Set("Content-Type", mw.FormDataContentType())

	r, fileName, err := extractUploadedFile(req, "490154203237518")
	if err != nil {
		t.Fatalf("extractUploadedFile() error = %v", err)
	}
	got, err := io.ReadAll(r)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != fileContent {
		t.Errorf("extracted content = %q, want %q -- must not include boundaries/metadata fields", string(got), fileContent)
	}
	wantFileName := "EVENT_490154203237518_00000000_2026_09_17_06_40_08_F_23.ts"
	if fileName != wantFileName {
		t.Errorf("fileName = %q, want %q -- needed to correlate the upload by exact name (migration 0044)", fileName, wantFileName)
	}
}

// TestExtractUploadedFile_NonMultipart_FallsBackToRawBody: defensive
// fallback -- if the Content-Type is not multipart/form-data, the body is
// assumed to be the file itself.
func TestExtractUploadedFile_NonMultipart_FallsBackToRawBody(t *testing.T) {
	const raw = "raw bytes without any envelope"
	req := httptest.NewRequest(http.MethodPost, "/upload/490154203237518", strings.NewReader(raw))
	req.Header.Set("Content-Type", "application/octet-stream")

	r, fileName, err := extractUploadedFile(req, "490154203237518")
	if err != nil {
		t.Fatalf("extractUploadedFile() error = %v", err)
	}
	got, err := io.ReadAll(r)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != raw {
		t.Errorf("content = %q, want %q", string(got), raw)
	}
	if fileName != "" {
		t.Errorf("fileName = %q, want \"\" -- a raw body carries no filename metadata", fileName)
	}
}

// TestExtractUploadedFile_MultipartWithoutFileField_Errors: a valid
// multipart WITHOUT any "file" field must not be treated as if the raw body
// were the file (that would store only metadata as if it were real video).
func TestExtractUploadedFile_MultipartWithoutFileField_Errors(t *testing.T) {
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	must(t, mw.WriteField("filename", "EVENT_x.ts"))
	must(t, mw.Close())

	req := httptest.NewRequest(http.MethodPost, "/upload/490154203237518", &buf)
	req.Header.Set("Content-Type", mw.FormDataContentType())

	if _, _, err := extractUploadedFile(req, "490154203237518"); err == nil {
		t.Error("extractUploadedFile() error = nil, want error -- a multipart without a \"file\" field must never fall back to the raw body")
	}
}

// TestParseEventFilenameTime is a regression test: the device mixed a
// day-old file into today's alarm. This function is the basis of the defense
// (see maxClipTimestampDrift in alarmclip.go); it is tested separately from
// handleUpload (which touches Postgres and is excluded from unit tests)
// because it is pure.
func TestParseEventFilenameTime(t *testing.T) {
	cases := []struct {
		name     string
		filename string
		wantOK   bool
		wantTime time.Time
	}{
		{
			name:     "real name captured from hardware (F)",
			filename: "EVENT_490154203237518_00000000_2026_09_17_07_34_19_F_23.ts",
			wantOK:   true,
			wantTime: time.Date(2026, 9, 17, 7, 34, 19, 0, time.UTC),
		},
		{
			name:     "real name captured from hardware (I, cabin)",
			filename: "EVENT_490154203237518_00000000_2026_09_17_07_34_19_I_24.ts",
			wantOK:   true,
			wantTime: time.Date(2026, 9, 17, 7, 34, 19, 0, time.UTC),
		},
		{
			name:     "raw body without filename metadata",
			filename: "",
			wantOK:   false,
		},
		{
			name:     "name without the known EVENT_ pattern",
			filename: "clip.ts",
			wantOK:   false,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, ok := parseEventFilenameTime(c.filename)
			if ok != c.wantOK {
				t.Fatalf("parseEventFilenameTime(%q) ok = %v, want %v", c.filename, ok, c.wantOK)
			}
			if ok && !got.Equal(c.wantTime) {
				t.Errorf("parseEventFilenameTime(%q) = %v, want %v", c.filename, got, c.wantTime)
			}
		})
	}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

// fakeGT06Sender implements gt06Sender without touching Postgres or the
// network, to test the HasActiveSession gate in handleUpload and native
// photos (the rest of handleUpload touches Postgres via db.WithBypass and is
// excluded from unit tests, as in gt06server/handlers_test.go).
type fakeGT06Sender struct {
	hasActiveSession bool
	// onRaw, if set, is called on every SendRawCommand (native photo tests
	// use it to simulate the device uploading the photo).
	onRaw func(imei, text string)
	mu    sync.Mutex
	raw   []string
}

func (f *fakeGT06Sender) SendRawCommand(ctx context.Context, imei, text string, timeout time.Duration) (string, error) {
	f.mu.Lock()
	f.raw = append(f.raw, text)
	f.mu.Unlock()
	if f.onRaw != nil {
		go f.onRaw(imei, text)
	}
	return "PICTURE", nil
}

func (f *fakeGT06Sender) SendCommand(ctx context.Context, imei, commandType string, timeout time.Duration) (string, error) {
	return "", nil
}
func (f *fakeGT06Sender) SendRawCommandFireAndForget(imei, text string) error { return nil }
func (f *fakeGT06Sender) HasActiveSession(imei string) bool                   { return f.hasActiveSession }

// TestHandleUpload_RejectsWithoutActiveGT06Session is a regression test for
// a CRITICAL security review finding: before this check, an anonymous POST to
// /upload/{imei} with NO live authenticated GT06 session for that IMEI went as
// far as resolving/overwriting that device's video clip (confirmed live).
// b.pool is nil on purpose: if the gate does not reject BEFORE touching the
// database, this test panics instead of merely failing the status assertion.
func TestHandleUpload_RejectsWithoutActiveGT06Session(t *testing.T) {
	b := &Bridge{gt06: &fakeGT06Sender{hasActiveSession: false}}

	req := httptest.NewRequest(http.MethodPost, "/upload/490154203237518", strings.NewReader("bytes"))
	req.SetPathValue("imei", "490154203237518")
	rec := httptest.NewRecorder()

	b.handleUpload(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want %d -- without a live GT06 session the upload must be rejected before touching storage/DB", rec.Code, http.StatusUnauthorized)
	}
}
