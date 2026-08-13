# vm-cave red team, 2026-08-11

An adversarial review of the host boundary, run from inside the bootstrap VM
with guest root, against the running system rather than the design. Each
finding below was reached by probing, not by reading; where a claim rests on
code reading alone it says so.

The question asked was "is the VM secure against escape". The short answer is
that the VM boundary held and every break was in a channel we deliberately
opened. No hypervisor escape was attempted or found, and the device surface
looks appropriately small: two virtio-blk, virtio-rng, virtio-vsock, no NIC,
no 9p, no virtio-fs, direct kernel boot, seed disk read-only, no host mounts.
The findings are all cases where an enumerated channel is wider than the
document describing it.

Values are deliberately omitted throughout: this file records that a
credential or identifier is exposed, never what it is.

## Cross-branch status

Three of these are not vm-cave's alone. `main` (the Rust container-cave
implementation) shares the same defect in 3, 5 and 6, does not have 4, and
already solves 1 in a way vm-cave regressed. Fixing 3 and 5 is therefore a
two-branch job, and 1 is a matter of restoring what already exists.

Since the review: 1 and 3 are fixed on vm-cave, 3 still stands on `main`. 4 is
accepted at a corrected severity of low. 5 and 6 are open on both.

## 1. The git daemon exports the whole parent directory, writable

Severity: critical. Fixed: the daemon now serves one repository, the
project's shared bare repo (`.coding-cave/cave-shared.git`), whitelisted
explicitly rather than by `--export-all` over a directory of repos. The
guest's remote is that repo, so a push can no longer reach the human's
working checkout either. Probed after the change: the project repo, a
sibling, and three traversal forms are all refused; only the shared repo
answers.

`vmcav`'s `start_git_daemon` runs `git daemon --base-path="$(dirname $REPO)"
--export-all --enable=receive-pack`. That serves every git repository beside
the project, not the project. Probing candidate names harvested from the
seeded `claude.json`, sixteen answered, including the host's dotfiles
repository and crinj's own source.

The pre-receive guard is installed only into `$REPO/.git/hooks`, so it
protects exactly one of them. Verified both halves: a push to a base branch of
the project repo was rejected by the hook, as was a push of a tag; a push to a
sibling repo created a branch with no check at all. The probe branch was
deleted immediately and its removal confirmed.

The escalation that makes this critical is the dotfiles repository, which is
chezmoi-managed: a guest that pushes a modified shell profile gets host code
execution as the user at the next apply. A guest can also edit the source of
the proxy that contains it.

`main` does not have this. `daemon.rs`'s `start()` creates a per-agent
temporary directory and symlinks in only the `shared_repos` it was passed, and
`cav-git-daemon` rejects `..` components and checks containment before
canonicalizing. The exported set is an explicit allowlist. vm-cave replaced
that with stock `git daemon` and inherited its far broader default.

Fix: serve one repository, e.g. `git daemon --strict-paths
--enable=receive-pack --export-all "$REPO/.git"`, rather than relying on a
hook that exists in one repository out of sixteen.

## 2. Not a finding: unrestricted egress through crinj

Recorded so it is not re-raised. crinj forwards to arbitrary hosts, not only
to the Anthropic API; outbound TLS to unrelated public hosts succeeds from
inside the guest. This is the intended design, since caves are built around an
open internet, and the boundary's purpose is credential injection, not
destination control. It does mean every other finding here is also an
exfiltration finding, which is why the ones below are rated as they are.

Incidentally: crinj answers `CONNECT` with `200 Connection Established` before
attempting the upstream connection, so a tunnel to an unreachable or
non-existent destination is indistinguishable from a working one until data
flows. That cost this review a false positive and is worth fixing for
diagnosability.

## 3. The host's whole `~/.claude.json` is handed to the guest

Severity: high. Fixed on vm-cave, still open on `main`. `stage_seed` no longer
reads `$HOME/.claude.json` at all; what it stages is the VM's own previous
config, captured from the guest during a reformat. The host's account
identity, its MCP server's API key and its map of project paths never reach
the seed disk. The trust and onboarding flags the guest actually needs are set
in the guest's own config, keyed to its own project path, where they belong.

`vmcav`'s `stage_seed` copies `$HOME/.claude.json` verbatim onto the seed
disk. It is not a small file and it is not only preferences. It carries a live
third-party API key belonging to an MCP server, the account identity block
(email address, account and organization identifiers, organization role,
billing and subscription details), and the full list of the user's project
paths, which is effectively a map of their home directory.

The design says a fully compromised guest never sees a real key. That is true
of the Anthropic credential and false in general, because of this file.

`main` has the same defect, in `seed_claude_config`: it copies the host's
`~/.claude.json` when the cave home lacks one. `main` also already contains
the right answer and does not use it. `lib/sandbox.nix` builds a minimal
synthesized default (onboarding and callout flags only), but the host copy
runs first, so the synthesized file is only a fallback for hosts that have no
`.claude.json` at all.

Fix: synthesize the file instead of copying it. The guest needs
`hasCompletedOnboarding`, the callout dismissal, and the two per-project trust
flags. Everything else is the host's business. Both branches should build it
from a literal.

## 4. Reformat does not reset the guest

Severity: low, downgraded from the medium-high this review first gave it.
Accepted, not fixed. Recorded so it is not re-raised.

`capture_guest_credentials` reads `/root/.claude/.credentials.json` *from the
guest* and stores the whole file host-side, re-seeding it into the next guest.
Every key round-trips verbatim, and the placeholder check is an unanchored
substring match, so the token strings themselves are arbitrary-length
carriers. A guest can therefore write state that survives the one command
documented as the way out of a broken or untrusted guest. It was never
demonstrated, because demonstrating it means reformatting.

The original rating did not survive its own argument. It requires an agent
that is already compromised, and this same review concluded that the guest
persists across a reformat by design through its own branch in the shared
repo. A kilobyte of JSON beside an open-ended git branch is not medium-high;
rating it that way spends attention that belongs elsewhere.

Scrubbing was tried and backed out. Parsing into a struct of the fields that
matter, checking each, and re-marshalling reduces the capacity without closing
it, because the fields that have to survive are carriers themselves: the token
pattern pins only the middle, leaving a 20-character prefix and 32 hex digits
per token, and the scopes array is worth several hundred bytes on its own.
Roughly a kilobyte still crosses.

The fix, if this ever matters, is to stop reading the guest's file. crinj
already holds the pair in its vault, keyed by the issued access token and
stable across refresh, so the host can read `issued_access` and
`issued_refresh` for the endpoint and build the rest from a fixed template.
The guest then contributes nothing. Note that this is a read, not a mint: the
vault stores the real tokens inline per row, so a second handle for the same
login would copy them and then desynchronise at the first refresh. Per-VM
revocable handles need crinj's schema split into login and handle, which is a
change to crinj rather than a flag.

What was worth doing was cheap and is done: the capture happens during a
reformat and nowhere else, rather than at every stop and every detach, so the
host reaches into the guest for this once, at the last moment it can.

## 5. The host's signing key will sign bytes the guest chose

Severity: medium-high. Partly inherent.

`gpg-broker.py` is careful where it counts: it validates argv against an
allowlist, rebuilds the invocation host-side, and pins the key, so the guest
cannot export key material or sign as someone else. But the payload is
guest-controlled and never examined. The guest can obtain a valid detached
signature from the user's key over any content at all: a signed tag, a
release artifact, a mail message, a commit object attributing arbitrary
content to the user.

Some of this is inherent: an agent that signs its own commits must be trusted
with its own commits. What is not inherent is that it can sign things that are
not commits. The broker could parse the payload as a git commit object and
require that its tree and parents exist in the served repository.

`main` is in the same position and slightly worse: it forwards the gpg-agent
extra socket into the sandbox, which is restricted mode (key export blocked),
but is still the general agent protocol rather than one operation. vm-cave's
broker is an improvement on it; neither constrains what gets signed.

Minor, in the same file: `--verify` appears in the allowlist, but the rebuilt
command is always `--detach-sign`, so a verify request signs. Fixed, along
with the two other verification flags that had no business in a signing
allowlist. Verification never reaches the broker: the guest runs it locally
against the seeded public keyring, which is also now possible at all, since
`cave-gpg` was exec'ing a gpg that was not on the guest's PATH.

The main finding stands: the payload is still guest-chosen and unexamined.

## 6. Host-side channels are reachable by any local uid

Severity: low on a single-user machine.

`vmcav` puts its control and bridge sockets under `/tmp` with a predictable
name. The gpg broker chmods its own socket to 0600; the socat bridges and
Cloud Hypervisor's API socket take default permissions. Another local user
could reach crinj egress, push through the git daemon with the agent's
injected identity, or, via the API socket, control the VM. A 0700 parent
directory closes it.

`main` has the same shape for a different reason: its per-agent git daemon
listens on a localhost TCP port and stamps every connection with that agent's
identity, so any local process can push as that agent.

## What this review did to the host

Recorded for completeness. Cloned the dotfiles and crinj repositories into the
guest and deleted them; read the seed disk; created one branch in a sibling
repository and deleted it, confirming removal; made outbound TLS connections
to three public hosts. Nothing was transmitted anywhere and no host state
remains changed.
