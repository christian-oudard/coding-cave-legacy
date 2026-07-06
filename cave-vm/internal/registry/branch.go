package registry

import "strings"

// The git ferry keeps each agent's work on a dev branch named
// agent-<id>/<base>, mirrored to the shared repo, with a high-water ref at
// refs/cav/agent-tip/<branch>. These helpers pin that naming so the Go git
// bridge and the pre-receive hook agree on it.

// DevBranch is the branch an agent pushes to for a given base branch.
func DevBranch(base, id string) string {
	return "agent-" + id + "/" + base
}

// AgentTipRef is the high-water ref the post-receive hook maintains per dev
// branch; the force-push guard compares an incoming update against it.
func AgentTipRef(branch string) string {
	return "refs/cav/agent-tip/" + branch
}

// ParseDevBranch splits agent-<id>/<base> into its id and base. The id must
// be all digits and the base non-empty; otherwise ok is false.
func ParseDevBranch(branch string) (id, base string, ok bool) {
	rest, found := strings.CutPrefix(branch, "agent-")
	if !found {
		return "", "", false
	}
	i := strings.IndexByte(rest, '/')
	if i <= 0 || i >= len(rest)-1 {
		return "", "", false
	}
	id, base = rest[:i], rest[i+1:]
	for _, c := range id {
		if c < '0' || c > '9' {
			return "", "", false
		}
	}
	return id, base, true
}

// ResolveBase unwraps nested agent-<id>/ prefixes to the underlying base
// branch, so a doubly-wrapped agent-1/agent-2/main resolves to main.
func ResolveBase(branch string) string {
	b := branch
	for {
		_, base, ok := ParseDevBranch(b)
		if !ok {
			return b
		}
		b = base
	}
}
