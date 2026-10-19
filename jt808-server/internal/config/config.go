// Package config reads the device server configuration from environment
// variables -- never from a versioned file; in production they are injected
// by the deployment platform.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
)

// MediaNode describes one ZLMediaKit media node. ID must equal the
// `mediaServerId` in that node's config.ini (how every ZLM hook tells the
// process which node fired it). A single-node deployment has exactly one of
// these, synthesized from the legacy ZLM_* scalars when ZLM_NODES is unset --
// so nothing changes for existing installs.
type MediaNode struct {
	ID                string `json:"id"`
	BaseURL           string `json:"base_url"`
	Secret            string `json:"secret"`
	RTPPushHost       string `json:"rtp_push_host"`
	FLVPlayURLFormat  string `json:"flv_play_url_format"`
	GT06PlayBaseURL   string `json:"gt06_play_base_url"`
	WebRTCPlayBaseURL string `json:"webrtc_play_base_url"`
	RTMPIngestHost    string `json:"rtmp_ingest_host"`
	ExternIP          string `json:"extern_ip"`
}

type Config struct {
	// ListenAddr is the TCP address where the server accepts JT808 device
	// connections, e.g. ":8808".
	ListenAddr string

	// MaxConnections caps concurrent TCP connections. The port is
	// necessarily public; without this limit an attacker could exhaust the
	// whole process's memory by opening unbounded connections (security
	// review finding). 5000 leaves ample headroom without being unlimited.
	MaxConnections int

	// Postgres connection settings. The server ALWAYS connects as app_user
	// (the same restricted role the API uses) -- never as a superuser. Every
	// transaction runs with app.bypass_rls='true' (see internal/db) because
	// the device server is a trusted service that resolves the tenant from
	// the device identifier, not from a user session with a JWT.
	PGHost     string
	PGPort     string
	PGDatabase string
	PGUser     string
	PGPassword string
	// PGMaxConns is the size of the pgx pool shared by JT808 + GT06 ingestion
	// and the video bridge for ALL tenants. pgx defaults to max(4, NumCPU) --
	// only 4 connections on a 2 vCPU host, a real bottleneck: one tenant's
	// slow query made every other tenant's ingestion wait. Postgres allows
	// 100; the API uses 10.
	PGMaxConns int

	// --- JT1078 -> ZLMediaKit video bridge ---

	// JT1078ListenAddr is where this process accepts the devices' VIDEO
	// connection (a different port from JT808 signaling).
	JT1078ListenAddr string
	// JT1078ListenPort is the same port as JT1078ListenAddr, as a number --
	// it is sent to the device in the 0x9101 command (the TcpPort field
	// takes only the number, not ":8081").
	JT1078ListenPort uint16
	// JT1078MaxConnections: same rule as MaxConnections for JT808.
	JT1078MaxConnections int
	// PublicIP is the IP the device is told (in the 0x9101) to connect its
	// video stream to -- it must be reachable from the device's cellular
	// network. In production, the server's public IP; in local dev with the
	// simulator, 127.0.0.1.
	PublicIP string

	// HTTPListenAddr exposes the internal bridge control API (POST
	// /api/v1/9101, ZLMediaKit hooks, etc.).
	HTTPListenAddr string

	ZLMBaseURL       string
	ZLMSecret        string
	ZLMPlayURLFormat string
	// ZLMWebrtcPlayBaseURL is the public HTTP base (scheme+host, no trailing
	// slash) used to build the WHEP signaling URL for live video -- shared by
	// jt1078bridge and gt06videobridge.
	ZLMWebrtcPlayBaseURL string

	// MediaNodes is the media plane: one or more ZLMediaKit nodes. Built in
	// Load() either from ZLM_NODES (JSON array) or, when that is unset, as a
	// single node synthesized from the ZLM_* scalars above (default
	// single-node mode, identical behavior to before this field existed). The
	// first element is the default node.
	MediaNodes []MediaNode

	// --- GT06 integration ---

	// GT06ListenAddr is where this process accepts GT06 device connections
	// -- a port independent from JT808 signaling (a completely different
	// binary protocol). :5023 is the conventional port across much of the
	// GT06 ecosystem, but it is arbitrary -- each device is configured by
	// SMS/app with the server's real host:port.
	GT06ListenAddr string
	// GT06MaxConnections: same rule as MaxConnections (JT808) -- the port is
	// necessarily public.
	GT06MaxConnections int

	// --- GT06 video (Jimi IoT JC261/JC400: GT06 telemetry + RTMP push
	// video) ---

	// GT06VideoApp is the RTMP "app" name these devices publish under
	// (configured on the device with the RSERVICE command,
	// "<host>/<GT06VideoApp>") -- deliberately different from "rtp" (the app
	// used by the JT1078 path) so both video pipelines never share a stream
	// namespace in ZLMediaKit.
	GT06VideoApp string
	// ZLMGT06PlayBaseURL is the public HTTP base (scheme+host, no trailing
	// slash) used to build a gt06_video stream's playback URL once on_publish
	// confirms it -- see gt06videobridge/bridge.go. Necessarily public (the
	// browser consumes it directly, like ZLMPlayURLFormat).
	ZLMGT06PlayBaseURL string

	// --- Alarm video clip retrieval ---

	// AlarmClipListenAddr is where this process serves POST /upload/{imei}
	// and POST /filelist/{imei} -- the device uploads files there directly,
	// so it is necessarily public (like JT1078ListenAddr), on its own port.
	AlarmClipListenAddr string
	// AlarmClipPublicBaseURL is the public HTTP base (scheme+host, no
	// trailing slash) sent to the device in the UPLOAD/FILELIST commands --
	// it must be reachable from the device's cellular network, like
	// PublicIP.
	AlarmClipPublicBaseURL string
	// AlarmClipMaxConnections: same rule as MaxConnections/
	// GT06MaxConnections -- this port is public too, and each connection may
	// buffer up to 100MB in RAM (maxClipUploadBytes), so the default cap is
	// lower than the device TCP servers'. Security review finding: this was
	// the only public server without a concurrent-connection cap or
	// timeouts.
	AlarmClipMaxConnections int

	// Credentials for the S3-compatible bucket (Cloudflare R2) where uploaded
	// clips are stored -- all optional: a deployment without this
	// integration simply cannot receive clips (internal/storage.Client fails
	// with a clear error, never a nil pointer panic) without breaking the
	// rest of the process.
	R2Endpoint  string
	R2AccessKey string
	R2SecretKey string
	R2Bucket    string
}

func Load() (Config, error) {
	cfg := Config{
		ListenAddr:     getEnv("JT808_LISTEN_ADDR", ":8808"),
		MaxConnections: getEnvInt("JT808_MAX_CONNECTIONS", 5000),
		PGHost:         getEnv("PGHOST", "127.0.0.1"),
		PGPort:         getEnv("PGPORT", "55432"),
		PGDatabase:     getEnv("PGDATABASE", "openmdvr"),
		PGUser:         getEnv("PGUSER", "app_user"),
		PGPassword:     os.Getenv("APP_USER_PASSWORD"),
		PGMaxConns:     getEnvInt("PG_MAX_CONNS", 16),

		JT1078ListenAddr:     getEnv("JT1078_LISTEN_ADDR", ":8081"),
		JT1078ListenPort:     uint16(getEnvInt("JT1078_LISTEN_PORT", 8081)),
		JT1078MaxConnections: getEnvInt("JT1078_MAX_CONNECTIONS", 5000),
		PublicIP:             getEnv("PUBLIC_IP", "127.0.0.1"),

		HTTPListenAddr: getEnv("HTTP_LISTEN_ADDR", ":8082"),

		ZLMBaseURL:           getEnv("ZLM_BASE_URL", "http://127.0.0.1:80"),
		ZLMSecret:            os.Getenv("ZLM_API_SECRET"),
		ZLMPlayURLFormat:     getEnv("ZLM_PLAY_URL_FORMAT", "http://127.0.0.1/rtp/%s.live.flv"),
		ZLMWebrtcPlayBaseURL: getEnv("ZLM_WEBRTC_PLAY_BASE_URL", "http://127.0.0.1"),

		GT06ListenAddr:     getEnv("GT06_LISTEN_ADDR", ":5023"),
		GT06MaxConnections: getEnvInt("GT06_MAX_CONNECTIONS", 5000),

		GT06VideoApp:       getEnv("GT06_VIDEO_APP", "live"),
		ZLMGT06PlayBaseURL: getEnv("ZLM_GT06_PLAY_BASE_URL", "http://127.0.0.1"),

		AlarmClipListenAddr:     getEnv("ALARM_CLIP_LISTEN_ADDR", ":8083"),
		AlarmClipPublicBaseURL:  getEnv("ALARM_CLIP_PUBLIC_BASE_URL", "http://127.0.0.1:8083"),
		AlarmClipMaxConnections: getEnvInt("ALARM_CLIP_MAX_CONNECTIONS", 200),

		R2Endpoint:  os.Getenv("R2_ENDPOINT"),
		R2AccessKey: os.Getenv("R2_ACCESS_KEY"),
		R2SecretKey: os.Getenv("R2_SECRET_KEY"),
		R2Bucket:    os.Getenv("R2_BUCKET"),
	}
	if cfg.PGPassword == "" {
		return Config{}, fmt.Errorf("config: APP_USER_PASSWORD is not set")
	}

	nodes, err := loadMediaNodes(cfg)
	if err != nil {
		return Config{}, err
	}
	cfg.MediaNodes = nodes

	return cfg, nil
}

// loadMediaNodes builds the media plane. ZLM_NODES (JSON array) takes
// precedence for multi-node; when it is unset or empty, a single node is
// synthesized from the legacy ZLM_* scalars so a single-server install needs
// no new configuration. Fails fast on invalid JSON or a node missing its id or
// secret -- a misconfigured media plane must stop the process at startup.
func loadMediaNodes(cfg Config) ([]MediaNode, error) {
	raw := os.Getenv("ZLM_NODES")
	if raw == "" {
		// Single-node synthesis from the legacy scalars.
		if cfg.ZLMSecret == "" {
			return nil, fmt.Errorf("config: ZLM_API_SECRET is not set")
		}
		return []MediaNode{{
			ID:                getEnv("ZLM_NODE_ID", "zlm-1"),
			BaseURL:           cfg.ZLMBaseURL,
			Secret:            cfg.ZLMSecret,
			FLVPlayURLFormat:  cfg.ZLMPlayURLFormat,
			GT06PlayBaseURL:   cfg.ZLMGT06PlayBaseURL,
			WebRTCPlayBaseURL: cfg.ZLMWebrtcPlayBaseURL,
			ExternIP:          getEnv("ZLM_EXTERN_IP", ""),
		}}, nil
	}

	var nodes []MediaNode
	if err := json.Unmarshal([]byte(raw), &nodes); err != nil {
		return nil, fmt.Errorf("config: ZLM_NODES is not valid JSON: %w", err)
	}
	if len(nodes) == 0 {
		return nil, fmt.Errorf("config: ZLM_NODES is an empty array")
	}
	seen := make(map[string]bool, len(nodes))
	for i := range nodes {
		n := &nodes[i]
		if n.ID == "" {
			return nil, fmt.Errorf("config: ZLM_NODES[%d] has no id", i)
		}
		if seen[n.ID] {
			return nil, fmt.Errorf("config: ZLM_NODES has duplicate id %q", n.ID)
		}
		seen[n.ID] = true
		if n.Secret == "" {
			return nil, fmt.Errorf("config: ZLM_NODES node %q has no secret", n.ID)
		}
		if n.BaseURL == "" {
			return nil, fmt.Errorf("config: ZLM_NODES node %q has no base_url", n.ID)
		}
		// Fall back to the shared scalars for the browser-facing templates a
		// node omits, so a multi-node config only needs to spell out what
		// differs per node.
		if n.FLVPlayURLFormat == "" {
			n.FLVPlayURLFormat = cfg.ZLMPlayURLFormat
		}
		if n.GT06PlayBaseURL == "" {
			n.GT06PlayBaseURL = cfg.ZLMGT06PlayBaseURL
		}
		if n.WebRTCPlayBaseURL == "" {
			n.WebRTCPlayBaseURL = cfg.ZLMWebrtcPlayBaseURL
		}
	}
	return nodes, nil
}

func getEnv(key, fallback string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return fallback
}

func getEnvInt(key string, fallback int) int {
	v, ok := os.LookupEnv(key)
	if !ok || v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}
