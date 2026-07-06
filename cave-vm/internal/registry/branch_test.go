package registry

import "testing"

func TestDevBranchAndTip(t *testing.T) {
	if got := DevBranch("main", "4"); got != "agent-4/main" {
		t.Fatalf("DevBranch = %q", got)
	}
	if got := AgentTipRef("agent-4/main"); got != "refs/cav/agent-tip/agent-4/main" {
		t.Fatalf("AgentTipRef = %q", got)
	}
}

func TestParseDevBranch(t *testing.T) {
	cases := []struct {
		branch   string
		id, base string
		ok       bool
	}{
		{"agent-4/main", "4", "main", true},
		{"agent-1/feature/foo", "1", "feature/foo", true},
		{"agent-42/develop", "42", "develop", true},
		{"main", "", "", false},
		{"agent-x/main", "", "", false}, // non-digit id
		{"agent-4/", "", "", false},     // empty base
		{"agent-/main", "", "", false},  // empty id
		{"agent-4", "", "", false},      // no slash
	}
	for _, c := range cases {
		id, base, ok := ParseDevBranch(c.branch)
		if ok != c.ok || id != c.id || base != c.base {
			t.Errorf("ParseDevBranch(%q) = (%q,%q,%v), want (%q,%q,%v)",
				c.branch, id, base, ok, c.id, c.base, c.ok)
		}
	}
}

func TestResolveBase(t *testing.T) {
	cases := map[string]string{
		"main":                 "main",
		"agent-4/main":         "main",
		"agent-1/agent-2/main": "main",
		"agent-3/feature/foo":  "feature/foo",
	}
	for branch, want := range cases {
		if got := ResolveBase(branch); got != want {
			t.Errorf("ResolveBase(%q) = %q, want %q", branch, got, want)
		}
	}
}
