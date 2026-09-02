# vm-cave red team, 2026-08-11

An adversarial review of the host boundary, run from inside the bootstrap VM
with guest root, against the running system rather than the design. Each
finding below was reached by probing, not by reading; where a claim rests on
code reading alone it says so.

A second pass, run the same way on 2026-08-27, is recorded at the end of this
file and continues the numbering at 7.

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

# vm-cave red team, 2026-08-27

A second pass, run the same way: from inside the guest as the root agent,
against the running system. Sixteen days and a good deal of new surface later
-- per-agent uids and worktrees, the cave environment, swap, and the session
archive -- so the question this time was less "is the VM secure against
escape" and more "did the new channels stay as narrow as the old ones".

They mostly did. Nothing here is a hypervisor escape and nothing here gets a
guest onto the host as a process. The two new findings are both cases of a
guard that is correct about the thing it was written for and silent about a
neighbouring case: a service that listens wider than the channel it was built
for, and a rescue path that mistakes a malformed command for an empty
directory.

Values are omitted here as in the first pass.

## Where the first pass stands

Re-probed rather than assumed, except where noted.

- **1 (git daemon exported everything)** holds fixed. Only the shared bare repo
  answers, under both the names it is served by; `/dotfiles.git`,
  `/crinj.git` and three traversal forms are all refused. But see 7: what was
  fixed was *which* repositories are served, not *to whom*.
- **2 (unrestricted egress; premature `200`)** still true, and the premature
  `200` has stopped being merely a diagnosability wart. It is now what makes 8
  hard to reason about from either side, and it cost this pass the same false
  positive it cost the last one.
- **3 (`~/.claude.json`)** fixed on vm-cave; not re-checked on `main`.
- **4 (reformat does not reset the guest)** accepted, not re-probed.
- **5 (the broker signs bytes the guest chose)** open, unchanged. Re-probed
  directly: `cave-gpg` signed 39 bytes of text that is not a git object and
  returned a valid detached signature under the user's key. The allowlist
  still does what it does well; the payload is still never examined.
- **6 (host-side sockets reachable by any local uid)** not checkable from
  inside the guest, which is where this review runs. Unknown, not cleared --
  and 7 is a second instance of the same underlying habit.

## 7. The host's git daemon listens on every interface

Severity: high, and the highest-value finding of this pass. Demonstrated, not
read.

`start_git_daemon` runs `git daemon ... --port="$GIT_PORT"` with no
`--listen`, so it binds every address the host has rather than loopback. The
boundary document says the repo reaches the guest over vsock 7300, and that is
true; what it does not say is that the same daemon is simultaneously reachable
by every other route into the host.

Two consequences, and the second is the one that matters.

The first is that it defeats crinj's loopback guard. crinj refuses `CONNECT`
to `127.0.0.1` and friends, which is a deliberate guard against the guest
reaching back into the host. But the host owns non-loopback addresses too, and
the guard never considers them. `CONNECT 172.17.0.1:9418` -- the host's docker
bridge address -- returned the host git daemon's real ref advertisement,
byte-identical to what the sanctioned vsock 7300 path returns. That is the
guest reaching a host service by a route the boundary document does not
enumerate, through the one channel whose job is to prevent exactly that.

The second is that the exposure is not the guest's alone. `receive-pack` is
enabled, so every container on the host's bridge and every host on its LANs
can both read the project and push into the shared bare repo. Such a client
inherits `CODING_CAVE_AGENT_ID` from the daemon's environment exactly as the
guest does, so its pushes are stamped as the root agent and land in
`agent-0/*`. The `pre-receive` hook still refuses base branches, so this does
not move `main`; what it does is put attacker-chosen commits into the precise
namespace a human reviews and runs `vmcav approve` against. The hook's comment
says the stamp "is what the daemon asserts about whoever connected", and is
careful to note this only works while there is exactly one client. It is
worth being blunt that the set of possible clients is currently much larger
than one VM.

Fix: `--listen=127.0.0.1`, which is one word and closes the LAN half
completely. Closing the first half properly means not serving this on TCP at
all -- the guest's only sanctioned route is vsock, and a unix socket with the
bridge in front of it would make the daemon unreachable by address from
anywhere, rather than merely unreachable by the addresses crinj happens to
deny-list.

**Fixed, for the LAN half.** `start_git_daemon` passes `--listen=127.0.0.1`.
Measured rather than assumed: without it the daemon binds `0.0.0.0:9418` and
`[::]:9418`, every interface in both families; with it, `127.0.0.1:9418` and
nothing else. The sanctioned route is untouched, since the relay behind vsock
7300 is started as `--git 127.0.0.1:9418` and dials loopback.

Still open, and it is the half this does not reach: the guest can still ask
for the daemon by address, and is refused only because crinj deny-lists
loopback. Taking it off TCP entirely -- a unix socket with the bridge in front
-- would make it unreachable by any address rather than by the ones the guard
happens to name. That is a change to the service plane and wants doing on its
own.

## 8. crinj's destination guard is by spelling, not by address

Severity: medium on its own; it is 7 that turned it into a demonstrated
crossing.

The guard is a deny-list matched against the literal text of the `CONNECT`
target. Probed across spellings of the same address:

- refused: `127.0.0.1`, `127.0.0.2`, `::1`, `::ffff:127.0.0.1`, `0.0.0.0`,
  and `169.254.169.254`
- accepted: `localhost`, `127.1`, `2130706433`, `0`, and every RFC1918
  address tried (`192.168.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`)

Two separate problems sit in that list. The loopback spellings that get
through do not appear to actually connect -- no data ever came back from
them, including from a host port proven to answer by another route -- so
this is not by itself a loopback SSRF, and it should not be written up as
one. But because of 2's premature `200`, "refused after the handshake" and
"never connected" are indistinguishable from the guest, and from anyone
reading a log. That the deny-list contains `169.254.169.254` shows the intent
is to keep the guest off things that are not the internet. RFC1918 is the same
class and is absent, which means the guard's coverage does not match its own
evident purpose.

The deeper point is the one 7 demonstrates: a deny-list of addresses cannot
express "not the host", because the host has as many addresses as it has
interfaces, and one of them was enough. The guard belongs on what the
destination resolves to, evaluated after resolution, or the reachable set
belongs somewhere other than a list of strings.

## 9. A guest can make its session transcript invisible to the rescue path

Severity: medium. Demonstrated end to end against the host's own code.

`internal/sessions.Disk` reads transcripts out of a stopped image with
`debugfs -R "ls -p <dir>"`. `exec.Command` means there is no shell and no
shell injection, and the code is careful that "nothing the guest says becomes
a path" -- session ids are validated to a strict uuid shape, and a `ls -p`
line whose name would contain a separator is dropped. Directory names,
though, reach the request string unexamined, and `debugfs` re-tokenizes that
string itself. A filename may not contain `/`, but it may contain a space.

So a project directory named with a space turns `ls -p <dir>` into a `ls`
with two arguments, which is a usage error. `debugfs` prints its usage and
**exits 0**. `cmd.Output()` therefore succeeds with empty output, and the
caller reads an empty directory listing as an empty directory.

Run against a crafted image holding two transcripts, one under a normal
project name and one under a name with a space, the host's own `sessions`
package reported:

    List: err=<nil>
      record id=1111...  size=18  project="-projects-coding-cave"
    Sync: err=<nil>
      change id=1111...  outcome=added  err=<nil>

The second transcript is simply absent. No error, no warning, and `Sync`
reports success. The record then does not exist as far as the host is
concerned, and `reformat` deletes the disk it was on.

This matters more than a parsing bug because of which path it is. The disk
reader is documented as the more trustworthy of the two precisely because no
guest code runs, and it is the one reached for when the guest is too broken or
too untrusted to answer -- immediately before the disk is destroyed. An agent
that would rather not be read renames one directory. The host reports that it
rescued everything.

It is also a plain bug with no attacker at all. `EncodeProject` builds the
directory name from the working directory by replacing `/` with `-`, so any
project whose path contains a space silently loses its history down this path.

Ruled out by probing, so the finding is not overstated: this does not reach an
arbitrary host write. The extra token makes `dump` fail its own usage check
rather than redirect its output, a filename cannot contain `/` so no absolute
destination can be spelled, and `debugfs -R` executes only the first line of
its request, so a newline in a name does not run a second command.

Fix: stop passing paths through a string the callee re-parses -- `debugfs`
takes a request file, and `-f` avoids the tokenizer entirely. Failing that,
treat a usage error as an error: `debugfs` exiting 0 on a malformed request
means every caller here has to check output plausibility rather than the exit
status, and an empty listing for a directory known to exist should be loud.

## Not findings

Recorded so they are not re-raised.

- The cave environment spec (`/var/lib/coding-cave/env/`) is root-owned and
  not group- or world-writable, so an unprivileged agent cannot reach the
  other agents, or the root agent, by editing the module they all build from.
- The guest-side bridges (7100, 7400, 9418, 10255) are on the guest's loopback
  and reachable by every agent uid in the guest. That is the documented blast
  radius rather than a wall, and the separation that is supposed to hold is
  the VM's.

## What this review did to the host

Read the host git daemon's ref advertisement by two routes, vsock 7300 and
`172.17.0.1:9418` through crinj, both read-only. Made outbound TLS and HTTP
connections to one public host. Obtained one detached signature over a benign
39-byte string. Substituted store paths from the host's cache in the ordinary
way. No pushes, no branches created or deleted, nothing written to the host,
and no host state changed. The crafted disk images and the harness that read
them were built inside the guest and stay there.
