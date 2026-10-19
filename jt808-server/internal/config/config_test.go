package config

import "testing"

// baseEnv sets the variables Load() requires regardless of the media plane.
func baseEnv(t *testing.T) {
	t.Helper()
	t.Setenv("APP_USER_PASSWORD", "pw")
}

func TestLoad_SingleNodeSynthesizedFromScalars(t *testing.T) {
	baseEnv(t)
	t.Setenv("ZLM_API_SECRET", "secret-1")
	t.Setenv("ZLM_BASE_URL", "http://zlmediakit:80")
	t.Setenv("ZLM_PLAY_URL_FORMAT", "http://host/rtp/%s.live.flv")
	t.Setenv("ZLM_GT06_PLAY_BASE_URL", "http://host/gt06")
	t.Setenv("ZLM_WEBRTC_PLAY_BASE_URL", "http://host")
	// No ZLM_NODES -> exactly one node synthesized, identical to before.
	t.Setenv("ZLM_NODES", "")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if len(cfg.MediaNodes) != 1 {
		t.Fatalf("MediaNodes len = %d, want 1 (single-node synthesis)", len(cfg.MediaNodes))
	}
	n := cfg.MediaNodes[0]
	if n.ID != "zlm-1" || n.Secret != "secret-1" || n.BaseURL != "http://zlmediakit:80" {
		t.Errorf("synthesized node = %+v, want id/secret/base from scalars", n)
	}
	if n.FLVPlayURLFormat != "http://host/rtp/%s.live.flv" || n.GT06PlayBaseURL != "http://host/gt06" || n.WebRTCPlayBaseURL != "http://host" {
		t.Errorf("synthesized node play URLs = %+v, want them copied from the scalars", n)
	}
}

func TestLoad_SingleNodeRequiresSecret(t *testing.T) {
	baseEnv(t)
	t.Setenv("ZLM_API_SECRET", "")
	t.Setenv("ZLM_NODES", "")
	if _, err := Load(); err == nil {
		t.Error("expected an error when ZLM_API_SECRET is unset in single-node mode")
	}
}

func TestLoad_MultiNodeFromJSON(t *testing.T) {
	baseEnv(t)
	t.Setenv("ZLM_API_SECRET", "unused-when-nodes-set")
	t.Setenv("ZLM_WEBRTC_PLAY_BASE_URL", "http://fallback") // picked up by the node that omits it
	t.Setenv("ZLM_NODES", `[
		{"id":"zlm-1","base_url":"http://z1:80","secret":"s1","webrtc_play_base_url":"http://n1"},
		{"id":"zlm-2","base_url":"http://z2:80","secret":"s2"}
	]`)

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if len(cfg.MediaNodes) != 2 {
		t.Fatalf("MediaNodes len = %d, want 2", len(cfg.MediaNodes))
	}
	if cfg.MediaNodes[0].WebRTCPlayBaseURL != "http://n1" {
		t.Errorf("node 1 webrtc = %q, want its own http://n1", cfg.MediaNodes[0].WebRTCPlayBaseURL)
	}
	// Node 2 omitted webrtc_play_base_url -> falls back to the shared scalar.
	if cfg.MediaNodes[1].WebRTCPlayBaseURL != "http://fallback" {
		t.Errorf("node 2 webrtc = %q, want the shared fallback http://fallback", cfg.MediaNodes[1].WebRTCPlayBaseURL)
	}
}

func TestLoad_MultiNodeValidation(t *testing.T) {
	baseEnv(t)
	cases := map[string]string{
		"invalid json":   `[{"id":`,
		"empty array":    `[]`,
		"duplicate id":   `[{"id":"a","base_url":"http://z:80","secret":"s"},{"id":"a","base_url":"http://z2:80","secret":"s"}]`,
		"missing secret": `[{"id":"a","base_url":"http://z:80"}]`,
		"missing base":   `[{"id":"a","secret":"s"}]`,
	}
	for name, nodes := range cases {
		t.Setenv("ZLM_NODES", nodes)
		if _, err := Load(); err == nil {
			t.Errorf("%s: expected an error, got nil", name)
		}
	}
}
