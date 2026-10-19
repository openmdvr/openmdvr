package videobridge

import "testing"

func TestNewMediaPool_SingleNode(t *testing.T) {
	p, err := NewMediaPool([]NodeConfig{{ID: "zlm-1", BaseURL: "http://zlm:80", Secret: "s"}})
	if err != nil {
		t.Fatalf("NewMediaPool: %v", err)
	}
	if p.Len() != 1 {
		t.Fatalf("Len = %d, want 1", p.Len())
	}
	// Default and NodeOrDefault("") both resolve to the one node; an unknown
	// id also falls back to it (single-node mode must never return nil).
	if p.Default().ID != "zlm-1" {
		t.Errorf("Default id = %q, want zlm-1", p.Default().ID)
	}
	if p.NodeOrDefault("").ID != "zlm-1" || p.NodeOrDefault("nope").ID != "zlm-1" {
		t.Error("NodeOrDefault did not fall back to the single node")
	}
	// RTPPushHost is derived from the base URL host when not set explicitly.
	if got := p.Default().RTPPushHost; got != "zlm" {
		t.Errorf("derived RTPPushHost = %q, want zlm", got)
	}
}

func TestNewMediaPool_Validation(t *testing.T) {
	cases := map[string][]NodeConfig{
		"empty list":     {},
		"empty id":       {{ID: "", BaseURL: "http://z:80", Secret: "s"}},
		"missing secret": {{ID: "a", BaseURL: "http://z:80"}},
		"duplicate id": {
			{ID: "a", BaseURL: "http://z1:80", Secret: "s"},
			{ID: "a", BaseURL: "http://z2:80", Secret: "s"},
		},
	}
	for name, nodes := range cases {
		if _, err := NewMediaPool(nodes); err == nil {
			t.Errorf("%s: expected an error, got nil", name)
		}
	}
}

func TestMediaPool_PickRoundRobinSkipsUnhealthy(t *testing.T) {
	p, err := NewMediaPool([]NodeConfig{
		{ID: "a", BaseURL: "http://a:80", Secret: "s"},
		{ID: "b", BaseURL: "http://b:80", Secret: "s"},
		{ID: "c", BaseURL: "http://c:80", Secret: "s"},
	})
	if err != nil {
		t.Fatalf("NewMediaPool: %v", err)
	}
	// Round-robin spreads across all three before repeating.
	seen := map[string]int{}
	for i := 0; i < 6; i++ {
		seen[p.Pick("dev").ID]++
	}
	for _, id := range []string{"a", "b", "c"} {
		if seen[id] != 2 {
			t.Errorf("node %s picked %d times over 6 rounds, want 2 (round-robin)", id, seen[id])
		}
	}

	// Marking a node unhealthy removes it from the rotation.
	p.SetHealthy("b", false)
	for i := 0; i < 10; i++ {
		if p.Pick("dev").ID == "b" {
			t.Fatal("Pick returned an unhealthy node")
		}
	}
	// Recovery puts it back.
	p.SetHealthy("b", true)
	backIn := false
	for i := 0; i < 10; i++ {
		if p.Pick("dev").ID == "b" {
			backIn = true
			break
		}
	}
	if !backIn {
		t.Error("a recovered node was never picked again")
	}
}

func TestMediaPool_PickAllUnhealthyFallsBackToDefault(t *testing.T) {
	p, _ := NewMediaPool([]NodeConfig{
		{ID: "a", BaseURL: "http://a:80", Secret: "s"},
		{ID: "b", BaseURL: "http://b:80", Secret: "s"},
	})
	p.SetHealthy("a", false)
	p.SetHealthy("b", false)
	// Better to try the default than to refuse service when every node is
	// marked down.
	if p.Pick("dev") == nil {
		t.Fatal("Pick returned nil when all nodes are unhealthy")
	}
}
