package videobridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// ZLMClient talks to the ZLMediaKit HTTP API. It is not a generic client for
// the whole ZLM API, only what this project's video protocols need -- each
// method documents which protocol uses it, because the sets do not overlap
// (JT1078 opens/closes its own RTP receiver; GT06 never opens anything since
// the device pushes RTMP directly, it only needs to close the stream ZLM
// already has).
type ZLMClient struct {
	baseURL string // e.g. http://zlmediakit:80
	secret  string
	http    *http.Client
}

func NewZLMClient(baseURL, secret string) *ZLMClient {
	return &ZLMClient{
		baseURL: baseURL,
		secret:  secret,
		http:    &http.Client{Timeout: 5 * time.Second},
	}
}

// OpenRTPServerResult carries the real port ZLMediaKit assigned. When
// port=0 is requested, ZLM picks a free one from its configured port_range
// and returns it here; we never guess it or manage a port pool ourselves.
type OpenRTPServerResult struct {
	Port int
}

// OpenRTPServer opens (or reopens) ZLMediaKit's RTP receiver for streamID in
// passive TCP mode (ZLM listens, we connect as client -- the same mode as
// the reference pattern documented by go-jt808, see the package comment in
// jt1078bridge/rtp.go). port=0 lets ZLM pick one from its port_range. Used by
// jt1078bridge; GT06/RTMP never needs it since the device pushes directly.
func (c *ZLMClient) OpenRTPServer(ctx context.Context, streamID string, port int) (OpenRTPServerResult, error) {
	var res struct {
		Code int    `json:"code"`
		Msg  string `json:"msg"`
		Port int    `json:"port"`
	}
	params := url.Values{
		"secret":    {c.secret},
		"stream_id": {streamID},
		"port":      {strconv.Itoa(port)},
		"tcp_mode":  {"1"}, // 0=udp 1=passive tcp (ZLM listens) 2=active tcp
	}
	if err := c.getJSON(ctx, "/index/api/openRtpServer", params, &res); err != nil {
		return OpenRTPServerResult{}, err
	}
	if res.Code != 0 {
		return OpenRTPServerResult{}, fmt.Errorf("videobridge: openRtpServer code=%d msg=%q", res.Code, res.Msg)
	}
	return OpenRTPServerResult{Port: res.Port}, nil
}

// CloseRTPServer closes the RTP receiver for streamID. Always called before
// opening a new one with the same streamID (a device reconnect must not leave
// an orphan receiver in ZLM). Nothing to close is not an error -- ZLM just
// reports hit=0 -- so this method does not fail in that case. Used by
// jt1078bridge.
func (c *ZLMClient) CloseRTPServer(ctx context.Context, streamID string) error {
	var res struct {
		Code int    `json:"code"`
		Msg  string `json:"msg"`
		Hit  int    `json:"hit"`
	}
	params := url.Values{
		"secret":    {c.secret},
		"stream_id": {streamID},
	}
	if err := c.getJSON(ctx, "/index/api/closeRtpServer", params, &res); err != nil {
		return err
	}
	if res.Code != 0 {
		return fmt.Errorf("videobridge: closeRtpServer code=%d msg=%q", res.Code, res.Msg)
	}
	return nil
}

// CloseMediaStream closes a stream by app/stream via ZLMediaKit's general
// close_streams API. Unlike CloseRTPServer (specific to the RTP receiver
// jt1078bridge opens for JT1078), this closes any active stream regardless
// of origin, so it works for the RTMP video a gt06_video device (Jimi IoT
// JC261/JC400) pushes directly -- there is no receiver of our own to close
// on that side, only ZLM's publish session. Used as a backstop to the real
// cut (the "stop_video" command to the device): defense in depth, never a
// single layer. Used by gt06videobridge.
func (c *ZLMClient) CloseMediaStream(ctx context.Context, app, stream string) error {
	var res struct {
		Code  int `json:"code"`
		Count int `json:"count_hit"`
	}
	params := url.Values{
		"secret": {c.secret},
		"app":    {app},
		"stream": {stream},
		"force":  {"1"},
	}
	if err := c.getJSON(ctx, "/index/api/close_streams", params, &res); err != nil {
		return err
	}
	if res.Code != 0 {
		return fmt.Errorf("videobridge: close_streams code=%d", res.Code)
	}
	return nil
}

// ReaderCount returns how many viewers app/stream has right now
// (getMediaList, totalReaderCount -- same value for every output protocol of
// the stream, so the max is taken). A nonexistent stream returns 0 without
// error. The snapshot flow uses it to never cut a stream someone is
// watching live.
func (c *ZLMClient) ReaderCount(ctx context.Context, app, stream string) (int, error) {
	var res struct {
		Code int `json:"code"`
		Data []struct {
			TotalReaderCount int `json:"totalReaderCount"`
		} `json:"data"`
	}
	params := url.Values{
		"secret": {c.secret},
		"app":    {app},
		"stream": {stream},
	}
	if err := c.getJSON(ctx, "/index/api/getMediaList", params, &res); err != nil {
		return 0, err
	}
	if res.Code != 0 {
		return 0, fmt.Errorf("videobridge: getMediaList code=%d", res.Code)
	}
	n := 0
	for _, d := range res.Data {
		if d.TotalReaderCount > n {
			n = d.TotalReaderCount
		}
	}
	return n, nil
}

// ErrSnapFallbackImage is returned when ZLMediaKit could not capture a real
// frame and served its configured placeholder image instead. getSnap NEVER
// returns an HTTP error on a capture failure (stream without a valid ticket,
// no keyframe yet, or nonexistent): it silently falls back to `defaultSnap`
// (config.ini) with HTTP 200. The only reliable signal, confirmed
// empirically, is the response Content-Type: the placeholder is served as
// the configured file (PNG, `image/png`) while a real capture is always the
// JPEG produced by its own ffmpeg command (`-f mjpeg`, `image/jpeg`) --
// never the response size (varies with each frame) nor the HTTP status
// (always 200).
var ErrSnapFallbackImage = errors.New("videobridge: getSnap returned the placeholder image, not a real capture")

// GetSnap asks ZLMediaKit for a JPEG snapshot of ONE frame of app/stream.
// Used by the photo capture flow (see snapshot.go), never exposed to the
// browser (server-to-server call with the same secret as the rest of this
// client). playToken is embedded as "?token=..." in the URL passed to
// ZLMediaKit: getSnap opens a real INTERNAL player to read the stream, and
// that player fires the same on_play hook as any external player (a getSnap
// without a valid ticket is rejected as "unauthorized" in the ZLMediaKit
// logs just like a real player, and returns the placeholder -- see
// ErrSnapFallbackImage). The "127.0.0.1" in the internal URL is ALWAYS
// ZLMediaKit connecting to itself; it never depends on any public host,
// unlike the playback URLs exposed to the browser.
func (c *ZLMClient) GetSnap(ctx context.Context, app, stream, playToken string, timeoutSec int) ([]byte, error) {
	playURL := fmt.Sprintf("rtmp://127.0.0.1/%s/%s?token=%s", app, stream, playToken)
	params := url.Values{
		"secret":      {c.secret},
		"url":         {playURL},
		"timeout_sec": {strconv.Itoa(timeoutSec)},
		// expire_sec=0: never serve a snapshot ZLM cached from a previous
		// call -- every capture must be a real, fresh frame, not a file
		// reused from ZLM's disk.
		"expire_sec": {"0"},
	}
	u := c.baseURL + "/index/api/getSnap?" + params.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, fmt.Errorf("videobridge: building getSnap request: %w", err)
	}
	// Own timeout, generous relative to timeoutSec (the margin ZLM/ffmpeg
	// gets to connect and capture), leaving room for the HTTP response
	// itself.
	client := &http.Client{Timeout: time.Duration(timeoutSec+5) * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("videobridge: calling getSnap: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxSnapBytes))
	if err != nil {
		return nil, fmt.Errorf("videobridge: reading getSnap response: %w", err)
	}
	contentType := resp.Header.Get("Content-Type")
	if !strings.HasPrefix(contentType, "image/jpeg") {
		return nil, ErrSnapFallbackImage
	}
	return body, nil
}

// maxSnapBytes bounds the getSnap response read. A single-frame JPEG should
// never come close; this is a backstop against an abnormal response.
const maxSnapBytes = 5 << 20 // 5 MiB

func (c *ZLMClient) getJSON(ctx context.Context, path string, params url.Values, out any) error {
	u := c.baseURL + path + "?" + params.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return fmt.Errorf("videobridge: building request to %s: %w", path, err)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("videobridge: calling %s: %w", path, err)
	}
	defer resp.Body.Close()
	if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
		return fmt.Errorf("videobridge: decoding response from %s: %w", path, err)
	}
	return nil
}
