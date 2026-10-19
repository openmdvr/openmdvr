package videobridge

import (
	"fmt"
	"net/url"
	"sync"
)

// NodeConfig describes one ZLMediaKit media node. It is the protocol-agnostic
// descriptor videobridge needs; the config package (cmd/server) maps its own
// environment into this, so videobridge never depends on config.
//
// ID MUST equal the `mediaServerId` set in that node's config.ini -- it is how
// every ZLMediaKit hook (on_publish, on_play, ...) tells this process which
// node fired it, so the bridge can act on the SAME node that holds the stream.
type NodeConfig struct {
	ID      string
	BaseURL string // internal ZLM HTTP API base, e.g. http://zlmediakit:80
	Secret  string

	// RTPPushHost is the host (no scheme) the JT1078 relay dials to push RTP
	// into THIS node's openRtpServer. Empty -> derived from BaseURL's host.
	RTPPushHost string

	// FLVPlayURLFormat is the browser-facing FLV playback template for JT1078
	// on this node (one %s for the stream id), e.g.
	// https://portal.example.com/media/zlm-1/rtp/%s.live.flv
	FLVPlayURLFormat string
	// GT06PlayBaseURL is the browser-facing FLV base for gt06_video on this
	// node (scheme+host[+path], no trailing slash).
	GT06PlayBaseURL string
	// WebRTCPlayBaseURL is the browser-facing WHEP base on this node
	// (scheme+host[+path], no trailing slash), shared by both protocols.
	WebRTCPlayBaseURL string

	// RTMPIngestHost is where a push device (GT06/RTMP) is told to publish via
	// RSERVICE, e.g. node1.media.example.com:1935. Informational for the API's
	// config-command builder; the node a stream really landed on is always
	// confirmed by on_publish's mediaServerId.
	RTMPIngestHost string
	// ExternIP is this node's externally reachable IP for the un-proxied
	// WebRTC media ports. Informational (infra/config-command use).
	ExternIP string
}

// MediaNode is a live media node: its resolved ZLMediaKit client plus the
// browser/device-facing endpoints that are specific to it.
type MediaNode struct {
	ID                string
	Client            *ZLMClient
	RTPPushHost       string
	FLVPlayURLFormat  string
	GT06PlayBaseURL   string
	WebRTCPlayBaseURL string
	RTMPIngestHost    string
	ExternIP          string
}

// MediaPool is the protocol-agnostic set of media nodes. One pool is built
// once in cmd/server/main.go and shared by reference to the Dispatcher and
// every Protocol, exactly as the single *ZLMClient was before. A single-node
// deployment is just a pool of length one, so no caller needs a special case.
//
// The hard part of multi-node is stream<->node affinity: which node holds a
// given stream. This is NOT stored here (the pool only knows the set of
// nodes); affinity lives on each ActiveEntry.NodeID, set when the node is
// chosen (pull) or learned from on_publish's mediaServerId (push).
type MediaPool struct {
	nodes map[string]*MediaNode
	order []string // stable; order[0] is the default node

	mu        sync.Mutex
	rr        int             // round-robin cursor for Pick
	unhealthy map[string]bool // nodes that recently failed an API call
}

// NewMediaPool builds the pool from node descriptors. The first node is the
// default (used for single-node mode and whenever a mediaServerId does not
// resolve). Fails fast on an empty list, a missing id/secret, or a duplicate
// id -- a misconfigured media plane must stop the process at startup, never
// silently serve video from the wrong node.
func NewMediaPool(nodes []NodeConfig) (*MediaPool, error) {
	if len(nodes) == 0 {
		return nil, fmt.Errorf("videobridge: media pool needs at least one node")
	}
	p := &MediaPool{
		nodes:     make(map[string]*MediaNode, len(nodes)),
		unhealthy: make(map[string]bool, len(nodes)),
	}
	for _, n := range nodes {
		if n.ID == "" {
			return nil, fmt.Errorf("videobridge: media node with empty id")
		}
		if n.Secret == "" {
			return nil, fmt.Errorf("videobridge: media node %q has no ZLM API secret", n.ID)
		}
		if _, dup := p.nodes[n.ID]; dup {
			return nil, fmt.Errorf("videobridge: duplicate media node id %q", n.ID)
		}
		rtpHost := n.RTPPushHost
		if rtpHost == "" {
			h, err := hostFromURL(n.BaseURL)
			if err != nil {
				return nil, fmt.Errorf("videobridge: media node %q: %w", n.ID, err)
			}
			rtpHost = h
		}
		p.nodes[n.ID] = &MediaNode{
			ID:                n.ID,
			Client:            NewZLMClient(n.BaseURL, n.Secret),
			RTPPushHost:       rtpHost,
			FLVPlayURLFormat:  n.FLVPlayURLFormat,
			GT06PlayBaseURL:   n.GT06PlayBaseURL,
			WebRTCPlayBaseURL: n.WebRTCPlayBaseURL,
			RTMPIngestHost:    n.RTMPIngestHost,
			ExternIP:          n.ExternIP,
		}
		p.order = append(p.order, n.ID)
	}
	return p, nil
}

// Node resolves a node by its id (a hook's mediaServerId). ok=false for an
// unknown/empty id so the caller can fall back to Default.
func (p *MediaPool) Node(id string) (*MediaNode, bool) {
	n, ok := p.nodes[id]
	return n, ok
}

// NodeOrDefault returns the node for id, or the default node when id is empty
// or unknown (single-node mode, or a hook from a node not in config). This is
// the safe resolver every hook handler uses: it never returns nil.
func (p *MediaPool) NodeOrDefault(id string) *MediaNode {
	if n, ok := p.nodes[id]; ok {
		return n
	}
	return p.Default()
}

// Default is the first configured node. Used for single-node mode and as the
// fallback whenever a node cannot be resolved.
func (p *MediaPool) Default() *MediaNode {
	return p.nodes[p.order[0]]
}

// Pick chooses a node for a PULL protocol that must place a new stream
// (JT1078): round-robin over healthy nodes, falling back to the default if
// every node is currently marked unhealthy. deviceKey is accepted for a future
// sticky/consistent-hash policy; today the choice is round-robin so load
// spreads evenly. Agnostic: any protocol that needs to place a stream uses it.
func (p *MediaPool) Pick(deviceKey string) *MediaNode {
	p.mu.Lock()
	defer p.mu.Unlock()
	n := len(p.order)
	for i := 0; i < n; i++ {
		id := p.order[(p.rr+i)%n]
		if !p.unhealthy[id] {
			p.rr = (p.rr + i + 1) % n
			return p.nodes[id]
		}
	}
	// Every node is marked unhealthy -- better to try the default than to
	// refuse service; the next successful call clears the flag.
	return p.nodes[p.order[0]]
}

// SetHealthy records the last observed health of a node from an API call.
// Bridges call it after talking to a node's client so Pick can route around a
// node that is down for new streams (existing streams on a dead node are lost;
// the device reconnects/re-requests and gets routed elsewhere).
func (p *MediaPool) SetHealthy(id string, healthy bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if healthy {
		delete(p.unhealthy, id)
	} else {
		p.unhealthy[id] = true
	}
}

// Len is the number of configured nodes (1 == single-node mode).
func (p *MediaPool) Len() int { return len(p.order) }

func hostFromURL(raw string) (string, error) {
	u, err := url.Parse(raw)
	if err != nil {
		return "", fmt.Errorf("invalid base url %q: %w", raw, err)
	}
	if u.Host == "" {
		return "", fmt.Errorf("base url %q has no host", raw)
	}
	return u.Hostname(), nil
}
