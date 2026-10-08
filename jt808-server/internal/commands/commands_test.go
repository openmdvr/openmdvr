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

// fakeSender is a test-controlled Sender -- it never touches a real
// connection.
type fakeSender struct {
	reply string
	err   error
	// gotDeviceKey/gotCommandType/gotTimeout capture what Handler actually
	// passed to SendCommand, to verify the HTTP request body is propagated
	// unchanged.
	gotDeviceKey   string
	gotCommandType string
	gotTimeout     time.Duration
}

func (f *fakeSender) SendCommand(ctx context.Context, deviceKey, commandType string, timeout time.Duration) (string, error) {
	f.gotDeviceKey = deviceKey
	f.gotCommandType = commandType
	f.gotTimeout = timeout
	return f.reply, f.err
}

func doRequest(t *testing.T, senders map[string]Sender, body string) (*http.Response, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/commands", strings.NewReader(body))
	rec := httptest.NewRecorder()
	Handler(senders)(rec, req)
	resp := rec.Result()

	var parsed map[string]any
	if resp.ContentLength != 0 {
		if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
			t.Fatalf("decoding JSON response: %v", err)
		}
	}
	return resp, parsed
}

func TestHandler_MalformedJSON_Returns400(t *testing.T) {
	resp, body := doRequest(t, map[string]Sender{"gt06": &fakeSender{}}, `{not json`)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", resp.StatusCode)
	}
	if body["code"].(float64) != 400 {
		t.Errorf("code = %v, want 400", body["code"])
	}
}

func TestHandler_MissingFields_Returns400(t *testing.T) {
	cases := []string{
		`{"deviceKey":"868720063843126","commandType":"engine_stop"}`,               // no protocol
		`{"protocol":"gt06","commandType":"engine_stop"}`,                           // no deviceKey
		`{"protocol":"gt06","deviceKey":"868720063843126"}`,                         // no commandType
		`{"protocol":"","deviceKey":"868720063843126","commandType":"engine_stop"}`, // empty protocol
	}
	for _, body := range cases {
		resp, _ := doRequest(t, map[string]Sender{"gt06": &fakeSender{}}, body)
		if resp.StatusCode != http.StatusBadRequest {
			t.Errorf("body=%q: status = %d, want 400", body, resp.StatusCode)
		}
	}
}

func TestHandler_UnknownProtocol_Returns400_NeverCallsAnySender(t *testing.T) {
	sender := &fakeSender{reply: "should not be called"}
	resp, body := doRequest(t, map[string]Sender{"gt06": sender},
		`{"protocol":"jt808","deviceKey":"123456789012345","commandType":"engine_stop"}`)

	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", resp.StatusCode)
	}
	if sender.gotDeviceKey != "" {
		t.Error("the sender of a different protocol was invoked -- Handler must resolve by protocol before calling SendCommand")
	}
	if msg, _ := body["msg"].(string); !strings.Contains(msg, "protocol") {
		t.Errorf("msg = %q, want a mention of the unsupported protocol", msg)
	}
}

func TestHandler_Success_Returns200WithReply(t *testing.T) {
	sender := &fakeSender{reply: "DYD=Success!"}
	resp, body := doRequest(t, map[string]Sender{"gt06": sender},
		`{"protocol":"gt06","deviceKey":"868720063843126","commandType":"engine_stop"}`)

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if body["code"].(float64) != 0 {
		t.Errorf("code = %v, want 0", body["code"])
	}
	if body["reply"] != "DYD=Success!" {
		t.Errorf("reply = %v, want %q", body["reply"], "DYD=Success!")
	}
	if sender.gotDeviceKey != "868720063843126" || sender.gotCommandType != "engine_stop" {
		t.Errorf("sender got deviceKey=%q commandType=%q, want the request body values", sender.gotDeviceKey, sender.gotCommandType)
	}
	if sender.gotTimeout != commandTimeout {
		t.Errorf("sender got timeout=%v, want commandTimeout=%v", sender.gotTimeout, commandTimeout)
	}
}

func TestHandler_ReplyLongerThanMaxReplyLen_Truncated(t *testing.T) {
	longReply := strings.Repeat("A", maxReplyLen+250)
	sender := &fakeSender{reply: longReply}
	resp, body := doRequest(t, map[string]Sender{"gt06": sender},
		`{"protocol":"gt06","deviceKey":"868720063843126","commandType":"engine_stop"}`)

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	reply, _ := body["reply"].(string)
	if len(reply) != maxReplyLen {
		t.Errorf("len(reply) = %d, want %d (truncated)", len(reply), maxReplyLen)
	}
}

// TestHandler_ErrorMapping covers every error sentinel a Sender may wrap --
// Handler must map each to the right HTTP status without coupling to each
// protocol's concrete error type (hence fmt.Errorf("...: %w", sentinel), as
// gt06server.Dispatcher actually does).
func TestHandler_ErrorMapping(t *testing.T) {
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
			sender := &fakeSender{err: tc.err}
			resp, body := doRequest(t, map[string]Sender{"gt06": sender},
				`{"protocol":"gt06","deviceKey":"868720063843126","commandType":"engine_stop"}`)

			if resp.StatusCode != tc.wantStatus {
				t.Fatalf("status = %d, want %d", resp.StatusCode, tc.wantStatus)
			}
			if code, ok := body["code"].(float64); !ok || int(code) != tc.wantStatus {
				t.Errorf("body code = %v, want %d", body["code"], tc.wantStatus)
			}
			if reply, has := body["reply"]; has && reply != "" {
				t.Errorf("reply = %v, want empty/omitted on error", reply)
			}
		})
	}
}

func TestHandler_ErrorMessageFromSender_NeverLeaksBeyondItsOwnText(t *testing.T) {
	// Handler does not sanitize the Sender's error message -- that is each
	// Sender's responsibility (and the API never forwards "msg" to clients).
	// This test documents the contract: what the Sender returns is exactly
	// what Handler exposes in "msg".
	wantMsg := "gt06: writing command: broken pipe"
	sender := &fakeSender{err: errors.New(wantMsg)}
	_, body := doRequest(t, map[string]Sender{"gt06": sender},
		`{"protocol":"gt06","deviceKey":"868720063843126","commandType":"engine_stop"}`)

	if body["msg"] != wantMsg {
		t.Errorf("msg = %q, want %q", body["msg"], wantMsg)
	}
}
