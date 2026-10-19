package commands

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// fakeRawSender is like fakeSender in commands_test.go, but for RawSender
// (raw text instead of commandType).
type fakeRawSender struct {
	reply string
	err   error

	gotDeviceKey string
	gotText      string
	gotTimeout   time.Duration
}

func (f *fakeRawSender) SendRawCommand(ctx context.Context, deviceKey, text string, timeout time.Duration) (string, error) {
	f.gotDeviceKey = deviceKey
	f.gotText = text
	f.gotTimeout = timeout
	return f.reply, f.err
}

func doRawRequest(t *testing.T, senders map[string]RawSender, body string) (*http.Response, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/gt06-raw-command", strings.NewReader(body))
	rec := httptest.NewRecorder()
	RawHandler(senders)(rec, req)
	resp := rec.Result()

	var parsed map[string]any
	if resp.ContentLength != 0 {
		if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
			t.Fatalf("decoding JSON response: %v", err)
		}
	}
	return resp, parsed
}

func TestRawHandler_MalformedJSON_Returns400(t *testing.T) {
	resp, _ := doRawRequest(t, map[string]RawSender{"gt06": &fakeRawSender{}}, `{not json`)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", resp.StatusCode)
	}
}

func TestRawHandler_MissingFields_Returns400(t *testing.T) {
	cases := []string{
		`{"deviceKey":"868720063843126","text":"SERVER,1,1.2.3.4,5023#"}`,
		`{"protocol":"gt06","text":"SERVER,1,1.2.3.4,5023#"}`,
		`{"protocol":"gt06","deviceKey":"868720063843126"}`,
		`{"protocol":"gt06","deviceKey":"868720063843126","text":""}`,
	}
	for _, body := range cases {
		resp, _ := doRawRequest(t, map[string]RawSender{"gt06": &fakeRawSender{}}, body)
		if resp.StatusCode != http.StatusBadRequest {
			t.Errorf("body=%q: status = %d, want 400", body, resp.StatusCode)
		}
	}
}

func TestRawHandler_TextTooLong_Returns400_NeverCallsSender(t *testing.T) {
	sender := &fakeRawSender{reply: "should not be called"}
	longText := strings.Repeat("A", maxRawTextLen+1)
	body := fmt.Sprintf(`{"protocol":"gt06","deviceKey":"868720063843126","text":%q}`, longText)
	resp, _ := doRawRequest(t, map[string]RawSender{"gt06": sender}, body)

	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", resp.StatusCode)
	}
	if sender.gotDeviceKey != "" {
		t.Error("text above maxRawTextLen must never reach SendRawCommand")
	}
}

func TestRawHandler_UnknownProtocol_Returns400_NeverCallsAnySender(t *testing.T) {
	sender := &fakeRawSender{reply: "should not be called"}
	resp, body := doRawRequest(t, map[string]RawSender{"gt06": sender},
		`{"protocol":"jt808","deviceKey":"123456789012345","text":"SERVER,1,1.2.3.4,5023#"}`)

	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", resp.StatusCode)
	}
	if sender.gotDeviceKey != "" {
		t.Error("the sender of a different protocol was invoked")
	}
	if msg, _ := body["msg"].(string); !strings.Contains(msg, "protocol") {
		t.Errorf("msg = %q, want a mention of the unsupported protocol", msg)
	}
}

func TestRawHandler_Success_PropagatesExactTextAndReturnsReply(t *testing.T) {
	sender := &fakeRawSender{reply: "SERVER OK"}
	resp, body := doRawRequest(t, map[string]RawSender{"gt06": sender},
		`{"protocol":"gt06","deviceKey":"868720063843126","text":"SERVER,1,203.0.113.10,5023#"}`)

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if body["code"].(float64) != 0 {
		t.Errorf("code = %v, want 0", body["code"])
	}
	if body["reply"] != "SERVER OK" {
		t.Errorf("reply = %v, want %q", body["reply"], "SERVER OK")
	}
	if sender.gotDeviceKey != "868720063843126" || sender.gotText != "SERVER,1,203.0.113.10,5023#" {
		t.Errorf("sender got deviceKey=%q text=%q, want the request body values", sender.gotDeviceKey, sender.gotText)
	}
	if sender.gotTimeout != commandTimeout {
		t.Errorf("sender got timeout=%v, want commandTimeout=%v", sender.gotTimeout, commandTimeout)
	}
}

func TestRawHandler_ReplyLongerThanMaxReplyLen_Truncated(t *testing.T) {
	longReply := strings.Repeat("A", maxReplyLen+250)
	sender := &fakeRawSender{reply: longReply}
	resp, body := doRawRequest(t, map[string]RawSender{"gt06": sender},
		`{"protocol":"gt06","deviceKey":"868720063843126","text":"TIMEZONE,+00:00#"}`)

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	reply, _ := body["reply"].(string)
	if len(reply) != maxReplyLen {
		t.Errorf("len(reply) = %d, want %d (truncated)", len(reply), maxReplyLen)
	}
}

func TestRawHandler_ErrorMapping(t *testing.T) {
	cases := []struct {
		name       string
		err        error
		wantStatus int
	}{
		{"DeviceNotConnected", fmt.Errorf("gt06: %w", ErrDeviceNotConnected), http.StatusNotFound},
		{"CommandTimeout", fmt.Errorf("gt06: %w", ErrCommandTimeout), http.StatusGatewayTimeout},
		{"CommandBusy", fmt.Errorf("gt06: %w", ErrCommandBusy), http.StatusConflict},
		{"UnknownError", errors.New("gt06: something went wrong writing to the socket"), http.StatusInternalServerError},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			sender := &fakeRawSender{err: tc.err}
			resp, body := doRawRequest(t, map[string]RawSender{"gt06": sender},
				`{"protocol":"gt06","deviceKey":"868720063843126","text":"SERVER,1,203.0.113.10,5023#"}`)

			if resp.StatusCode != tc.wantStatus {
				t.Fatalf("status = %d, want %d", resp.StatusCode, tc.wantStatus)
			}
			if code, ok := body["code"].(float64); !ok || int(code) != tc.wantStatus {
				t.Errorf("body code = %v, want %d", body["code"], tc.wantStatus)
			}
		})
	}
}
