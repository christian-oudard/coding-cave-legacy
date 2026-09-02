# vm-cave: open work

Things known to be missing or wrong, kept out of `vm-cave-design.md` because
they are not phases of the build-out: they are gaps the bootstrap has
surfaced. Each says what is broken and what is undecided, not what to do.

## The guest clock is never corrected

First in this file because it is low in the stack and everything above it
fails confusingly. The guest has no timesyncd, no NTP, no
`qemu-guest-agent` and no NIC to reach a time server with. It takes the
clock from the host at boot and nothing touches it afterwards.

On x86 under KVM this mostly hides: `kvm-clock` tracks host adjustments
across a suspend. Under HVF on aarch64 the guest is on `arch_sys_counter`,
which does not, so a closed lid leaves the guest behind by however long the
host slept. A stale clock fails TLS certificate validation, which fails
every agent request, and nothing in that failure points at the clock. ssh is
unaffected, since host keys carry no validity dates, so the channel that
could correct it keeps working while everything else is dead.

vfkit ships `--timesync` for exactly this and it is not usable here: it
speaks to `qemu-guest-agent` over vsock, which is a whole guest agent shipped
for one syscall. The host already talks to the guest, and whatever it says
can carry the host's UTC time.

Separately, and not the same problem: the timezone is a host information
leak. `main` pins the cave's `/etc/localtime` to UTC so the host's geographic
region stays outside the sandbox, with `coding-cave.timeZone` and a host `$TZ`
as the only levers. The guest owns its own `/etc/localtime`, so the mechanism
differs and the contract must not.

**Done, by the first of those.** `vmcav status` and `vmcav agent` correct the
clock through the control plane before they do anything else, so staleness is
bounded by when you last asked -- the rule the session sync already follows. A
guest-side timer was rejected because it would need a clock it has no reason to
trust, and a resume hook because it would need the host to notice its own
suspend.

Quiet unless it acts: `cave-vm guest -if-off-by` corrects only past a
threshold, so a couple of milliseconds of round trip is not reported as drift
and this can run on every command. A correction is worth a line, because it
means the guest was wrong about the time and an agent may have been failing
for it.

**The timezone half is done too**, and doing it turned up a larger leak of the
same kind. The guest is pinned to UTC in its own config rather than left on
NixOS's default, so the contract `main` states -- the host's geographic region
stays outside the sandbox -- is something somebody decided rather than
something that happened to be true.

The larger one: the host's `~/.claude.json` is staged on the seed disk and was
copied into every agent's home whole, carrying the host user's entire project
list -- 75 paths under their home directory, naming projects with nothing to
do with this VM. seed-agent-home@ already said that must not happen; it now
does what it said, keeping only project entries under /projects and dropping
history and MCP config with them. Applied to existing homes as well as fresh
seeds, since the homes that already existed are the ones holding the paths.

## `clone-repo` failed once and has not reproduced

Seen once, on a from-scratch `image create` into a never-booted disk: the
guest reached multi-user and answered ssh, `seed-claude-config` finished, and
then `clone-repo` failed, taking `vm-root-agent` with it, since it requires
it. The VM came up with nothing running in it.

    Starting Clone the coding-cave repo for the agent...
    [FAILED] Failed to start Clone the coding-cave repo for the agent.
    [DEPEND] Dependency failed for Bootstrap vm-root-agent

A later `image create` plus `start`, on the same host and the same code path,
succeeded: `clone-repo` exited 0 and the agent's checkout landed on
`agent-1/vm-cave`. So this is intermittent, not a plain break, and the run
that failed took its journal with it.

What makes it worth keeping rather than forgetting: `clone-repo` is a
`oneshot` with no retry, and what it depends on is not in the guest. It
clones through the vsock bridge to a git daemon on the host, so anything
transient at the wrong moment is unrecoverable and stops the agent from
existing at all.

The likeliest mechanism is guest-local, and it is not the one first written
here. `clone-repo` has `after`/`wants` on `vsock-git.service`, which is a
plain `socat TCP-LISTEN:9418 ... VSOCK-CONNECT:2:7300`. systemd calls such a
unit active the instant the process execs, not when it has bound the port, so
`git clone git://127.0.0.1/...` can arrive before anything is listening and
get a connection refused. The host side is a weaker suspect than it looked:
`start_git_daemon` runs before `cave-vm start`, so the host daemon is
listening before the guest begins to boot.

**Fixed, without a retry.** All four outbound bridges are `.socket` units now,
so systemd binds the port itself at `sockets.target`, before anything ordered
after them runs, and hands each connection to its own socat on stdin. The
ordering is a guarantee rather than a hope, and `clone-repo` requires
`vsock-git.socket` rather than a service that only proved a process existed.
It also deleted machinery: no `Restart=always` papering over a host bridge
that was not listening yet, since the kernel now holds the connection until
something answers.

Verified on a cold boot: all four bridges carry traffic (git, crinj to the
internet, the nix cache), `clone-repo` succeeds, and no unit is in a failed
state.

Still open, and cheap: the run that failed took its journal with it, so
whatever the cause, the evidence should survive the next one. One mechanism
for losing it is now closed: journald was being watchdog-killed every time the
host suspended, fourteen times in a single boot, taking whatever it had
buffered with it. See the watchdog note in guest/configuration.nix. Also untouched:
`vsock-ssh` is still a `VSOCK-LISTEN` socat, deliberately left alone because
it is the channel a deploy needs and a broken one is only recoverable by
reformat. It should become a socket unit the same way, and `cave-control`
already proves the shape works for AF_VSOCK.

## The root agent should be agent 0, and there should be a second one

Two agents at once, one root and one not, is the next thing this guest is
being pushed toward, and the id is the first thing in the way. `vmcav agent`
hardcodes `local id=1`, so the agent that builds the guest from inside it --
the root one, the only one there has ever been -- is agent 1, and the first
ordinary agent would collide with it or be handed 2 for no reason anyone
could state.

Reserve 0 for it. The root agent is not one of the numbered agents: it is the
one that redeploys the system the others run in, it exists before any project
is assigned, and it is the only one for which "root in the guest" is the point
rather than a hazard. A reserved id says that in the branch namespace, where
`agent-0/<base>` is visibly not part of the sequence, and it leaves 1..N to
mean what it means in `main` -- one per agent a human created.

Most of the machinery is already indifferent to the number. `pre-receive`
gates by whatever id the daemon injects and `scripts/test-git-hooks.sh`
already exercises agent 0. `clone-repo@` and `cave-session@` are templates
instanced by id. What is not: the hardcoded 1 in `vmcav agent`, and this VM's
own checkout, which is on `agent-1/vm-cave` and would have to be recreated as
`agent-0/vm-cave` or left as a legacy branch until it lands.

The harder half is that the id does not currently belong to an agent at all.
It belongs to the git daemon: `start_git_daemon` exports one
`CODING_CAVE_AGENT_ID` for every connection on one port, so two agents pushing
through it are the same agent as far as `pre-receive` can tell, and the
namespace guard stops separating anyone. Reserving 0 is worth doing on its own
-- it names the root agent correctly -- but it does not make two agents
distinguishable, and nothing else will until the id arrives with the
connection rather than with the daemon.

**Superseded. Agents are separate users with separate views.** What follows in
this section is kept because the reasoning is still worth reading, but two of
its conclusions are now wrong: that the worktree path should carry the agent's
id, and that `User=` cannot be chosen per instance. See *Non-root agents, and
a view each* below for what was built instead.

**Decided at the time: agents are not isolated from each other yet.** Each gets
its own worktree in the guest, kept there and never synced to the host, and
that is the whole of the separation for now. Namespaces or bubblewrap inside
the guest are a later question, deliberately deferred rather than skipped.

What that buys and what it does not: two agents stop overwriting each other's
files, which is what actually prevents them running today. It does not make
one unable to read or write another's tree, and with the root agent being root
it never could without a second mechanism. So the boundary that means anything
stays the VM's, exactly as `docs/vm-cave-boundary.md` has it -- inside the
guest, agents are colleagues rather than strangers, and the host's gate is
what stands between any of them and a base branch.

The honest consequence for the id: within one VM, attribution is a label
rather than a guarantee. The root agent can push as anyone, because it is root
and the stamp is not something the host can tie to a session. The gate that
matters is unaffected, since it refuses base branches by namespace and not by
believing who anybody is.

**Done, and what it left behind.** Worktrees are per agent at
`/projects/<slug>/<id>`, which is `main`'s layout, and `clone-repo@` moves a
pre-split checkout under the id its branch names rather than nesting a clone
inside it. Still single-tenant: `cave-guestd`'s `-cwd` is one flag on a daemon
that serves every agent, and it decides which project directory a restored
transcript is filed under. With one worktree that default was simply true;
with several it is true for one agent and wrong for the rest, so checkpoint
and restore work for whoever matches the default and silently misfile for
anyone else. The daemon already knows each agent's project -- it reads
`/run/cave/sessions/<id>.env` -- so the fix is to derive the cwd per agent
rather than to take it as a flag, and `sessions.Local` has to carry the agent
rather than being built once.

**And root is agent 0's alone, which is why there is still one agent.**
`cave-session@` runs every instance as uid 0, because there is no other user
in the guest to run one as. So a second agent came up root under an id that
says it is not the root agent, read a `/root/.claude/CLAUDE.md` addressed to
the agent that rebuilds the system, and was pointed at agent 0's worktree for
its rebuild command. Nothing was unsafe -- the boundary is the VM either way
-- but the id is what everything else reads to decide what an agent may do,
and one that lies is worse than a feature that is missing.

So `vmcav agent` refuses anything but `--root` for now, and says why. The
allocator underneath it is finished and tested; what is missing is in the
guest, and it is roughly:

- a user for ordinary agents, and the decision of whether that is one shared
  `agent` account or one per id. Given that agents are colleagues rather than
  strangers (above), one account is consistent and one per id buys file
  ownership nobody has asked for yet.
- `cave-session@` running as it. `User=` cannot branch on `%i`, so either
  the session script drops privilege itself when the id is not 0, or the root
  agent gets its own unit. The pty is the wrinkle: `cave-guestd` owns it, and
  a non-root agent has to be able to write to its own terminal.
- everything that assumes `/root`: the seeded claude config, the credentials,
  the git wrapper's identity, `XDG_RUNTIME_DIR`, and this file's address.

All three are done, and the middle one was wrong on its facts: `User=agent%i`
expands the specifier and resolves per instance, verified in the guest. What
is true is the narrower thing -- a unit cannot decide *whether* to set `User=`
from its instance name -- so the root agent gets its own template and the
choice between them is a fact recorded when the agent is created rather than a
guess about its number.

## Non-root agents, and a view each

**Done.** An ordinary agent runs as `agent<id>` out of a declared pool
(`maxAgents`, currently 8, in `guest/configuration.nix`); the root agent still
runs as root, because rebuilding the system the others run in is what it is
for. Which one an agent is gets written into `/run/cave/sessions/<id>.env`
when it is created, and every unit serving that agent reads it back: which
user, which home, which of the two session templates.

The worktree path stopped being the agent's problem. Trees live at
`/var/lib/coding-cave/agents/<id>/projects/<slug>`, and each session unit
binds its own over `/projects` in a private mount namespace, so every agent
works in `/projects/<slug>` and that path means a different tree for each of
them. The id survives where it is useful -- underneath, where a human
debugging two agents wants to tell the trees apart -- and disappears from the
place it was never doing any work, which is the working directory of somebody
who has exactly one tree. This is what `main` gets from bwrap, approached from
the other side: there a sandbox is built around the agent, here a namespace is
handed to its unit. `cave-enter <id>` is the way across from a root shell,
because outside every namespace `/projects` means nothing at all.

What it buys: two agents cannot overwrite each other's files, cannot name each
other's trees, and the thing running with permissions skipped is no longer
root over the whole guest. What it does not buy: any of this is visible to the
root agent, homes are 0700 rather than sealed, and a VM escape is unaffected.
It is a blast radius, not a border, exactly as `docs/vm-cave-boundary.md` has
it.

Two things fell out of doing it that were not obviously part of it:

- **The nix daemon had no proxy.** Substitution worked only for root, because
  a trusted user's client settings are accepted by the daemon and an ordinary
  user's are not; a non-root agent got `Could not resolve host: cache.nixos.org`
  on the first `nix develop` in a fresh checkout. The daemon now carries the
  crinj proxy in its own environment. The alternative -- adding agents to
  `trusted-users` -- would let an agent name its own substituter, which is a
  boundary decision and not one to make in passing.
- **Redeploying from inside killed the agent doing it**, three times, each
  taking the terminal with it. Its own entry, next.

## A redeploy from inside must not stop the agent running it

**Done, and worth knowing why it was not automatic.** The root agent
rebuilding the system it is running in is this VM's core loop
(`docs/vm-root-agent.md`). It is also the one operation here that can destroy
its own caller, and it did: three `nixos-rebuild switch` runs in a row, each
one killing the session it was typed into and dying half-applied at status
101, leaving a generation registered but never activated.

The cause was not the session unit changing, which is the case everybody
guards against and which `restartIfChanged = false` already covered. It was a
dependency:

    cave-session@0.service    Requires=seed-claude-config.service
    seed-claude-config        RemainAfterExit=yes, so it stays *active*

The change deleted `seed-claude-config`. `switch-to-configuration` stops units
that are gone from the config, `Requires=` propagates an explicit stop to
whatever named them, and the session died. `clone-repo@0` was in the same
`Requires=` list and was harmless only because it is `RemainAfterExit=false`:
already inactive, so there was nothing to stop.

Three rules follow, and the third is the one that makes the others hold:

1. **A session depends only on units that go inactive on success.**
   `RemainAfterExit=false`, or `Wants=` instead of `Requires=`. This is stated
   at `seed-agent-home@` in the config, because adding `RemainAfterExit=true`
   there later would silently re-arm the trap.
2. **Session templates are never removed or renamed while instances run**, and
   carry `restartIfChanged = false`.
3. **Removing any unit a live session might name is a two-generation
   operation.** Leave a no-op behind with `restartIfChanged`/`stopIfChanged`
   false, delete it in a later generation once nothing running references it.
   `seed-claude-config` is now that tombstone and can go once every session
   has been restarted onto the new dependencies.

`cave-rebuild` enforces this rather than trusting anyone to remember it: it
runs `nixos-rebuild dry-activate`, which prints exactly which units a switch
would stop, restart, reload and start while touching nothing, and refuses if
any `cave-session@*` or `cave-root-session@*` appears under stop or restart.
Two builds instead of one, the second a cache hit.

It cannot gate its own introduction -- the generation containing it has to be
activated by hand first -- and that first switch was done with the
`dry-activate` output read by eye in its place. Left undone: nothing checks
this in CI, because there is no CI that boots a guest and starts a session.
The check that matters is the one in the script, run every time.

`vmcav deploy` carries the same refusal, because it is the same operation: it
ends in `switch-to-configuration switch` inside the guest, so a host-side
deploy could stop a live session exactly as an in-guest one could, with the
additional charm that whoever loses their agent is not whoever typed the
command. It plans against the closure it has already copied in, so the check
costs a round trip rather than a build, and it runs before the host's boot
artifacts move -- a refusal leaves the host still pointing at what the guest
is running.

## Self-update and deploy are the same activation, and not the same thing

Worth stating because the difference is invisible from inside the guest.

Identical: the activation. Both `cave-rebuild` in the guest and `vmcav
deploy` on the host end in `switch-to-configuration switch` against a
generation registered in `/nix/var/nix/profiles/system`. Everything above
about stop cascades applies to both.

Different, and it is the half that decides whether the change survives:

- **Where the closure is built.** In the guest, from the agent's own checkout,
  substituting from the host store over 7100; or on the host, then `nix copy`
  into the guest. The guest path is what lets an agent test a configuration it
  just wrote without a human in the loop.
- **What boots next.** This guest has no bootloader: the host injects kernel,
  initrd and `init=` when it starts the VM, which is exactly what lets a
  switch register and activate a generation in place. The consequence is that
  in place is all it is. A self-update moves the running system and leaves the
  host's boot artifacts alone, so a stop/start comes back on whatever the host
  last deployed, with the newer generation still on the disk and unselected.
  Nothing reachable from inside the guest can change that, and that is the
  boundary working rather than a gap in it.
- **`deployed-from`.** `vmcav deploy` records the commit it built from, which
  is what `vmcav status` and the staleness warning read. A self-update leaves
  that record untouched, so after one the host believes the guest is older
  than it is.

So a redeploy from inside is a live test of a configuration, not a deployment
of one. The durable form is a human running `vmcav deploy`, which is the same
shape as the git flow: the agent does the work and proves it, a human lands
it. A kernel change needs a restart either way (`vmcav deploy && vmcav stop &&
vmcav start`).

## Uncommitted work does not survive `reformat`, and that is the rule

**Settled, and kept here because it reads like a gap and is not one.** A
reformat destroys the guest disk, and with it any worktree the agent has not
committed. That is the intended behaviour: committing is what makes work real,
pushing to `agent-<id>/<base>` is what puts it where a human can see it, and
the approval gate is the only way it becomes host state. A second path for
preserving work would be a second way for bytes to leave the VM, competing
with the gate for no benefit.

So the VM is cattle in the strong sense. The agent's obligation is to commit
early; the tool's obligation is not to pretend otherwise.

What has since changed is the gap between committing and publishing, not this
rule. The guest's `git` pushes the agent branch after any command that moves
it (`guest/cave-git.sh`), so a commit is on the host the moment it exists and
"commit early" no longer also means "remember to push". Everything above still
holds for a worktree that was never committed, which is the only thing a
reformat now destroys.

The text below is what was considered before that decision, retained only so
the options are not re-derived.

## Preserve uncommitted work across `reformat` (superseded)

`vmcav image reformat` destroys the guest disk, which is the only copy of anything
the agent has not pushed. Committed work survives because pushing to
`agent-<id>/<base>` puts it in the host repo; a dirty worktree does not.

The agent is told to commit early, but "the tool eats your work if you
forget" is not a workable contract, and reformat is the one command a
broken bootstrap forces you to run.

The stance this sits under: the VM is cattle. Anything worth keeping leaves
by a git commit, which is also the only way it passes the approval gate, so
there is no second escape route to build and no state worth preserving for
its own sake. That makes this entry narrow rather than large: the question is
only what to do about work an agent has written and not yet committed.

Still open, and now the only thing in this file that is. Session state moved
to the host (`vm-cave-design.md`), but a worktree cannot follow it: it is a
whole mutable checkout, not a file that only grows and copies out cleanly.
So a dirty worktree is still the one thing a reformat destroys.

Open: what to preserve and where. A pre-reformat WIP commit on the agent's
own branch is the cheapest, and pollutes the branch. A bundle or patch series
parked in the host state dir keeps the branch clean, and invents a second
place work can live. Also unresolved whether this belongs to reformat at all,
or is a guest-side habit (autocommit unit, periodic push).

## Nothing carries the human's commits into the shared repo

The blocker a first real dogfooding run hits, and it is not on any list above.
`ensure_shared_repo` clones the project's bare repo once, at first `start`, and
after that nothing updates it from the working checkout. The agent's `host-cave`
is that bare repo, so an agent cloning or fetching sees the base as it stood
when the VM was first created, however many commits ago.

The designed flow explains why: agent work goes up as `agent-<id>/<base>`,
`cav approve` fast-forwards it into `<base>` *in the shared repo*, and the
human pulls from there. Under that flow the shared repo leads and the
checkout follows. But a human who commits in their own checkout, which is what
happens the moment a person and an agent work on the same project, leads
instead, and nothing carries it across. The agent then rebases onto a base
that is stale by everything the human did, and the first symptom is a
conflict against work it cannot see.

Observed rather than reasoned: the guest ran the test suite against a tree 34
commits behind and reported a failure that had already been fixed. The
one-liner that unblocks it is a fetch into the bare repo
(`git -C <shared> fetch <checkout> <base>:<base>`), which needs no hook and no
push, since fetch does not run `pre-receive`.

**Fixed.** `start` fetches the checkout's current base branch into the shared
repo, `vmcav status` does it on every run, and `vmcav push [branch]
[--force]` does it on demand. Status is what closes the case `start` alone
cannot: a person and an agent working at the same time, where the person
commits between one start and the next and the agent is rebasing onto a base
frozen at boot. Staleness is then bounded by when you last looked, which is
the rule the session sync and the clock correction already follow.

Quiet unless it moves something, and quiet when it refuses: a base the human
rewrote is not a fast-forward, and that is theirs to resolve with `push
--force` rather than something to report on every run.

The original note, kept for the mechanism: `start` fetches the checkout's current base
branch into the shared repo, and `vmcav push [branch] [--force]` does it on
demand. A fetch into the bare repo rather than a push out of the checkout,
because fetch does not run `pre-receive` at all and refuses a
non-fast-forward without a leading `+`, so the safe case needs no flag and
the unsafe one cannot happen by accident. Agent branches are left alone: the
shared repo's copy is the one the agent pushed.

Still open, and now a smaller question than it looked: whether the human's
checkout should instead push through the same gate the agent does, or whether
the shared repo should stop being a snapshot and become a remote of the
checkout. Both are positions on who owns the base, and neither is needed to
dogfood. Also unbuilt, and the reason `push` is only half of `main`'s: there,
a push to an *agent* branch updates that agent's workspace, where here it
moves a ref the agent has yet to fetch.

## Session management

Mostly built, and the design is in `vm-cave-design.md`. Records are host
state, under `~/.local/share/coding-cave/sessions/<uuid>.jsonl` with a
`key=value` sidecar for agent and project, copied out with the host doing all
the naming, never replaced by anything shorter or with a different prefix.
`cave-vm sessions sync|restore|share|list` on the host, `cave-guestd sessions
list|read|write` in the guest, over ssh for a running VM and over `debugfs`
for a stopped one. Wired into `reformat`, `stop`, and the return from an
attach.

What is left:

- ~~Nothing syncs for an agent that exits with nobody attached.~~ **Built.**
  `status` syncs, which with `stop`, a detach and `reformat` covers every
  moment the host talks to the guest about agents. No timer: staleness is
  bounded by when you last looked, which needs no clock. What made it
  affordable was moving transcripts off ssh onto the control plane, where a
  sync costs 3-4ms against 95ms. Verified end to end: a record written in the
  guest with nobody attached arrives in the host archive on the next `status`,
  and an appended one is carried as `grew` rather than replaced.
- **Ownership is bookkeeping, not enforcement.** `sync -agent N` attributes
  records to whoever the caller names, and `vmcav` always says `1`. Every
  agent is root in one home, so any of them can write any transcript. The
  archive does not depend on this (a rewritten record is set aside rather than
  applied), but "its own sessions" means only what the sidecar says until the
  per-agent split lands, which is the multiple-agents entry below.
- **The `last` sighting only moves when bytes move**, so it says when a
  record last grew rather than when it was last seen. Harmless today, wrong
  if anything ever reads it as liveness.
- **Memory is untouched**, deliberately, and needs its own rule: per-project,
  shared between agents, rewritten rather than grown, so the length and prefix
  checks that protect a transcript say nothing useful about it. Worth knowing
  before designing a channel for it: memory is per-project and lives at
  `~/.claude/projects/<encoded-cwd>/memory/`, which is the directory the
  transcript sync already walks. So carrying it is an extension of a path that
  exists rather than a new mechanism, and the reason it does not happen today
  is only that the sync matches `<uuid>.jsonl` and nothing else.

## An agent's identity does not survive a stop/start

`cave-guestd start` writes `/run/cave/sessions/<id>.env` -- the agent's id,
project, base branch, and whether it is the root agent -- and every unit
serving that agent reads it back. `/run` is tmpfs, so a VM stop takes all of
it, while the things it describes (the worktree under
`/var/lib/coding-cave/agents/<id>`, the home, the branch on the host) all
survive. The agent is the only part of itself that does not.

Two failures come out of that, both observed:

- **Nothing restarts the agents.** After a restart the guest comes up with
  worktrees and homes for agents that no longer exist as far as systemd is
  concerned, and a human has to know to run `vmcav agent` again. A session
  whose `.env` is gone fails as `Failed to load environment files: No such
  file or directory`, which reads like a bug in the unit rather than the
  absence of a fact.
- **Recreating one can get it wrong.** The env file is where "this is the
  root agent" is recorded, and it is passed at creation (`--root`). Recreate
  agent 0 without it and the identity resolves to a user the guest does not
  declare; `agentIdentity` now says so, but saying so is not the same as not
  needing to. Observed as a guest that came back, ran nothing, and reported
  itself over ssh as not answering.

**Half done, and the half that does not touch the open question.** The record
is now kept beside the worktree it describes (`agent.env`), so recreating an
agent cannot get its root-ness wrong: `start` reads it back and keeps what the
agent was, saying so. That was the second failure above, observed as a guest
that came back, ran nothing and reported itself as not answering.

That is not the guest deciding what agents exist -- it is the guest not
forgetting what it was told about one it already has, and the record is
deleted with the worktree by `agent remove`. The first failure stands: nothing
restarts the agents after a stop, and that is the part which is properly the
host's call.

The record is now also what the host is *told*. A stopped agent stays on the
roster the control plane serves, described by the durable copy rather than the
one in `/run`, so `vmcav status` names it and says what it was created for
after a restart that took the tmpfs. That is worth having on its own -- an
agent holding an id with nothing on screen about it was how "no free agent id"
became unexplainable -- and it is also the input either shape of the fix
needs. The host cannot replay agents it cannot see, and it can see them now,
with their project and base branch attached.

What is undecided is which side owns the record. Persisting it in the guest
(`/var/lib/coding-cave/agents/<id>/agent.env`) is the small change and keeps
the guest able to restore itself, but it means the guest deciding what agents
exist, which is the host's call everywhere else. The host already has the
registry (`internal/registry`), so the other shape is for `vmcav start` to
replay it -- recreating each agent it knew about, in the same way it created
them the first time. That fits the boundary better and is more work: the
registry would have to record the base branch and the root flag, which today
it does not. Either way it wants deciding before agent hibernation, which
assumes an agent can be brought back at all.

## Starting and stopping agents, and knowing what is running

There is no way to start a second agent, stop one, or ask what is running.
`vmcav agent` attaches to "the" agent and `status` reports the VM plus
whatever the guest's socket directory says.

One defect worth recording rather than rediscovering: the host registry
decides liveness with `alive(pid)` against a *host* pid, which is meaningless
for an agent living in the VM. It is the same trap the VM pidfile already
sprang, asking your own namespace about someone else's process and being told
nothing is there. Nothing depends on it yet because there is only ever one
agent, which is why it has not shown up as a bug.

The shape proposed, and not yet agreed, is to split the two registers by what
each actually owns. Liveness belongs to the guest, where systemd owns
`cave-session@<id>` and the socket directory is the roster, so the host asks
rather than tracking. Durable facts belong to the host registry, which
already carries agent id, project, the root-agent flag and session id, and
should allocate ids instead of the hardcoded `1`.

Then `cav agent [id]` ensures the VM, ensures the unit and attaches, `cav
agent stop <id>` stops the unit and syncs that agent's records, and `status`
prints a line per agent from the guest joined to the registry's facts. Build
order, each step visible on its own: `cave-guestd list` reporting state,
start time and project; `status` showing it; start and stop by id; then id
allocation.

## Agent hibernation

A VM holds many agents, and it restarts: a host-side deploy that changes the
kernel, a stop at the end of a day, a crash. Every agent dies with it. There
is no way to quiesce them first, and no way to bring them back doing what
they were doing.

The present code assumes the opposite of all of this: `vmcav agent` attaches
to "the" agent, and nothing quiesces anything. Sessions surviving their own
restarts is now handled, one process and one unit each, but that is a
different problem from the machine going down under all of them.

What hibernation has to cover is at least: quiescing an agent at a point
where its work is recoverable (which overlaps the uncommitted-work entry
above), recording enough to resume the claude session rather than start a
fresh one, and restoring cwd, branch and whatever the agent had in flight.

Open, and genuinely undecided:

- The level it happens at. Settled: session-level, each agent's claude
  session id recorded and resumed with `--resume`, which is what the
  container cave does through the registry. Machine-level was tempting,
  since Cloud Hypervisor can pause, snapshot and restore a VM's memory and
  every agent would come back mid-thought with no per-agent machinery at
  all. It is out because it does not exist on the other host: vfkit's
  `POST /vm/state` takes `Pause`, `Resume`, `Stop` and `HardStop` and
  nothing else, and although Virtualization.framework has had save and
  restore since macOS 14, and vfkit's own state enum names `Saving` and
  `Restoring`, there is no way to ask for either. Pause and resume are the
  portable pair and belong in the backend interface for maintenance;
  snapshot is a Linux-only convenience that nothing may depend on.
- Who holds the roster of live agents: the host registry, as with container
  caves, or the guest, with the host asking.
- What a redeploy does to hibernated agents. An in-guest `nixos-rebuild
  switch` restarts units under a live agent, and a kernel change means a
  reboot; whether hibernation is what makes those safe, or is orthogonal.
- What survives a reformat. Answered: sessions are host state, so a reformat
  does not reach them at all.

## Memory management

Nothing carries `~/.claude/` memory into or out of the guest. The host seeds
`claude.json` only, so an agent starts with no memory of previous sessions.

Losing it at reformat is answered by memory being host state, alongside the
session logs. What is still open is whether guest memory has anything to do
with the host's own: seeded from it, kept separate per VM, or committed to
the repo like a project's `CLAUDE.md`. The login already works this way, and
is the precedent rather than the exception.

## Multiple agents per VM

**Deliberately deferred.** The near-term goal is one agent building the
coding cave from inside the coding cave, and one agent is enough for that.
Nothing below should be built ahead of that milestone; it is recorded so the
single-agent assumptions are known rather than discovered.

A VM is meant to hold many agents. Everything built so far assumes exactly
one, in ways that are cheap to list and not cheap to change:

- `vmcav agent` attaches to "the" agent, with no way to name one, though
  `cave-guestd attach <id>` underneath it takes one and starts the session if
  it is not there, so this is the host CLI rather than the guest
- the git daemon injects `CODING_CAVE_AGENT_ID=1` for the whole guest, so
  the pre-receive guard cannot tell co-resident agents apart (the hook says
  as much: it gates by namespace, not by agent)
- ~~**one worktree per project, shared by every agent.**~~ **Done**, and this
  entry called it correctly: `main` has the same path and no such problem
  because bwrap gives each agent a private mount namespace and therefore its
  own view of `/projects/<slug>`, so the fix was a namespace rather than a
  path scheme. The id-in-the-path version shipped first and was the wrong
  answer for exactly the reason predicted here. See *Non-root agents, and a
  view each* above.
- one ssh identity and one `/projects/coding-cave` working tree, so two agents
  would share a checkout
- one home, so one `~/.claude/projects`: every agent can read and rewrite
  every other agent's transcript, which is what keeps session ownership at
  the level of bookkeeping
- `/root` is written literally about twenty times across the guest's seeding
  units (gnupg, authorized_keys, gitconfig, `claude.json`, the credentials,
  the checkout) and `HOME` is set to it in the session unit. So moving an
  agent off that home is not one setting: a unit missed keeps seeding `/root`
  while the agent reads somewhere else, and it fails by the agent quietly
  having no credentials rather than by anything erroring

~~Open: whether agents are separate unix users or all root with separate
worktrees~~ -- settled: separate users, one per id, from a pool declared in
the guest's config. Still open: where ids are allocated (the host registry, as
in container caves, versus the guest); and what `vmcav agent` becomes when
there are several.

**Not open, and worth deleting before someone builds it: a vsock port and a
git daemon per agent.**

Start from what `CODING_CAVE_AGENT_ID` actually is, because the name invites
the mistake. It is not the daemon's identity; the daemon is not an agent. It
is what the daemon asserts about whoever connected to it, and it works today
only because there is exactly one client. So the id belongs to a
*connection*, not to a process.

A listener per agent is how `main` binds the id to a connection, and there it
is correct rather than a workaround. Three things hold it up: a listener per
agent (`socat TCP-LISTEN:<agent port>,fork EXEC:cav-git-daemon`, carrying that
agent's id in its environment), a network namespace per cave, and a firewall
in that namespace whose allowlist is the agent's own daemon port plus crinj.
So one cave cannot reach another's port and be attributed as it. The kernel
enforces the attribution, not a convention.

The VM cannot copy any of that. It needs a vsock port per agent to get a
listener per agent, and under vfkit every port is a `--device` argument fixed
in the launch argv, so nothing may allocate one at runtime;
`vm-cave-cross-platform.md` predicted exactly this shape of discovery. And
there is no per-cave network namespace in the guest to firewall, since caves
do not exist there yet at all.

The deeper reason it cannot be a host-side stamp at all: every agent's
connection arrives on the same socket, so with several agents the host cannot
tell them apart in principle rather than merely in practice. Only the side
that opened the connection can.

There is a further consequence that rules out the obvious repair as well.
`vmcav start` starts the VM, not an agent, and every agent in it shares that
VM. So no host-side artifact can carry the answer either: putting an agent id
in the seed disk, which is the established way a host fact reaches the guest,
would bake one agent's identity into a disk serving all of them. The current
`AGENT_ID` at start time has the same flaw and is correct only for as long as
there is exactly one agent.

**The shape that does work: demultiplex inside the guest.** `main` runs a
listener per agent on the host, and the VM runs one per cave in the guest: a
socket `cave-guestd` owns, one per cave, all funnelled onto the single vsock
7300, with guestd prefixing each connection with the id of the cave it came
from. The host relay reads the prefix and hands it to `receive-pack`. One
vsock port however many agents, so the launch argv is untouched and it ports
to vfkit unchanged.

The agent cannot forge the prefix because bwrap gives it exactly one socket
path and no route to 7300, and guestd sits outside every sandbox. That is the
same trust structure as `main` with the enforcement moved from a kernel
firewall to the sandbox's filesystem view.

Blocked on caves, which is design phase 6 and does not exist yet: with one
root agent there is nothing for guestd to tell apart, so building the demux
now would be machinery with no input. Until then one agent means one constant
id, which is correct; what is wrong is any interface implying the host picks
it per push.

Once agents are separate uids behind bwrap, `cave-guestd` sits outside every
agent's sandbox and knows which cave opened a connection, so it can attach
the id on the way out over the one git port. That trades "the host injects it, unforgeable from inside the VM" for
"guestd asserts it, unforgeable from inside a cave", which is the threat
model already written down: the VM is the boundary to the host, and bwrap in
the guest is a blast-radius choice between agents. A VM escape forges agent
identity either way, and by then it has lost more than that.

Name the weakening rather than discovering it later: on `main` the
attribution is enforced by a kernel firewall, and here it would rest on
guestd being correct. What does not change is who is protected from whom. The
host's gate still refuses any push outside `agent-*/`, whatever id is
claimed, so the worst a forged id buys is writing into another agent's branch
namespace inside the same VM.

The lifecycle half of this is a separate entry above, and can land first: it
makes who is running visible, which is the honest step before enforcing who
owns what.

Related: [Agent hibernation](#agent-hibernation) above, and the root-agent
flag already in `cave-vm/internal/registry` that makes root a per-VM agent
property rather than a reserved id.

## Other agent harnesses

Everything is claude-specific, in more places than the name suggests: the
unit runs `claude --dangerously-skip-permissions`, `IS_SANDBOX=1` exists
only to satisfy claude's root guard, the seed carries `claude.json`,
`seed-claude-config` forces claude's trust and onboarding keys, the guest's
managed settings pre-accept claude's bypass-permissions dialog, `vmcav
agent` attaches to a daemon session that is assumed to be running claude,
and crinj's OAuth chain is configured for Anthropic's token endpoint.

Codex, antigravity and whatever follows each arrive with their own version
of all of it: an auth story (a different OAuth provider, or a plain API key,
which is `[[host]]` header injection rather than the OAuth chain), their own
config and state files to seed, their own first-run prompts to pre-accept,
and their own idea of a resumable session.

The one piece that generalizes for free is crinj's placeholder mechanism:
it is provider-agnostic, so abbreviated bootstrapping (a VM that comes up
authenticated without a login) is the same trick per provider. What does
not generalize is where each harness keeps its credentials and in what
format.

Open: whether an image ships every harness or one per image; whether an
agent becomes a typed thing (harness plus id) in the registry, which is
also what `vmcav agent` would need to pick between; and whether the
harness-specific seeding is a list of files the harness declares, so adding
one is config rather than code.

## Graphical work: headless compositors and input injection

Asked for by a project outside this repo. A dictation tool supports exactly
one window manager (sway), and to support the rest it has to inject keystrokes
into a compositor and read back what arrived. Every remaining question there
was produced by reading source rather than by testing, because no cave has a
compositor in it. The deterministic test the project wants is: start a
headless compositor, focus a terminal running `cat > /tmp/out`, fire the tool
against a canned WAV, and assert the file contents.

**Why this belongs to the VM and not to `main`.** A container cave can already
be handed a device with `cav -d`, and `-d /dev/uinput` must never be offered.
uinput is a kernel-global facility, and a cave holding that node creates a
virtual keyboard in the *host's* kernel: an agent testing keystroke injection
would be typing into the human's real session, with no sandbox anywhere in the
path. The VM's kernel is its own, so the same test injects into the guest and
stops there. This is a capability the container caves cannot be configured
into, rather than one they merely lack.

What the guest does not have today:

- ~~**No uinput.**~~ **Done.** `hardware.uinput.enable`, which is the module,
  the `uinput` group, and a udev rule at 0660 with `static_node=uinput` so the
  node exists whether or not anything has loaded the module.
- ~~**No `XDG_RUNTIME_DIR`**~~ **Done.** `/run/cave-agent-%i`, created and
  removed by the session unit's own `RuntimeDirectory=` at 0700. Not
  `/run/user/0`, which belongs to logind for a root ssh login and would be
  deleted under a live `vmcav shell` when the session stopped.
- **No software GL.** There is no `/dev/dri` and there should not be, so every
  compositor here renders on llvmpipe or pixman. That is a mesa closure in the
  environment, not a guest capability.
- **No terminal emulator and no font**, which the sink half of the test needs.

Both of the above are verified by evaluating the guest config, not by booting
one. Neither can be a flake check: the guest evaluates only with `--impure`,
since crinj's CA and the Go module's path arrive from the environment.

**Where the desktop stack lives is the real question.** `systemPackages` is a
deliberate floor and is also the agent's PATH, and the matrix here is sway,
river, Hyprland, labwc, mutter, kwin, Xvfb plus i3, and three injection tools.
That is one project's dependency set, so it belongs in a per-project
environment, which is design phase 9 (`cave.nix`) and does not exist. What is
undecided is whether phase 9 has to land first or whether a project can carry
it in its own flake and `nix develop` today: the guest substitutes closures
from the host store over 7100, so the cost is a host-side build rather than a
guest one. The guest-image share of this is small either way, and is only the
kernel module, the device node, and who may open it.

Related, and blocked on caves: whether an unprivileged user can drive uinput
at all is the question the project actually has to answer, and it cannot be
tested in a guest where every agent is root. That is phase 6.

**The blocker is not the desktop stack, it is that the guest serves one
project.** The guest side is already parameterized: `clone-repo@` and
`cave-session@` are templates that take a project, `/projects/<slug>` has no
constant in it, and `vmcav agent <project> <base>` takes the name. The host
side is not. `start_git_daemon` whitelists exactly two paths, the shared repo
and a `<slug>.git` symlink beside it, both derived from the checkout `vmcav`
was run in, so asking for any other project resolves to nothing. Standing in a
second checkout instead does not help: the state dir is machine-wide but
`SHARED` and `SLUG` are per-checkout, so a second `start` tries to bind a
second daemon on 9418 and fails.

So a second project needs a machine-wide directory of shared repos, one per
project, that the daemon serves together. Undecided: whether that directory
holds symlinks into each checkout's `.coding-cave/cave-shared.git`, which
keeps the container caves' path and makes the daemon's base path leave this
project's tree, or whether the shared repos move under the state dir and the
per-checkout path becomes the link. The second is cleaner and diverges from
`main`, which knows those repos by their own path.

The alternative is real and should be named rather than discovered: a public
project can be cloned straight from the internet through crinj, which needs
none of the above and works today. It also puts the work outside the approval
gate, which is the thesis, so it is a fallback for reading someone else's code
rather than a way to develop in here.

**Sizing is per-VM, not per-agent.** The defaults are 4 vCPU and 4096 MiB for
the whole machine, and CPU-only speech inference next to a kwin session shares
them with every other agent in it. The flags exist, so this is not a blocker;
whether the default moves is a question about what a host can spare, and
nothing measures it yet.

**Egress is unverified.** The model weights come from HuggingFace, so crinj's
policy has to admit it. Nobody has tried.

**Audio stays outside.** No sound device in the guest, and none should be
added: a microphone reaching in is a host-to-guest crossing of the worst kind,
and `vm-cave-boundary.md` has no room for it. The canned-WAV test needs no
device. Open, and smaller: whether the tool starts at all without a sink, and
whether a null PipeWire sink in the guest is enough if it does not.

**Nothing carries a picture out.** A compositor bug is a screenshot, `grim`
writes it to a guest file, the agent can read it and a human cannot. This is
the missing `cav cp` that `vm-cave-parity.md` already records, and this is the
first work that makes it hurt.

## Image management, much later

`vmcav image create` builds one image into one disk at one path, for one VM.
Eventually there are several VMs, several images, and a reason to keep more
than one image around: rolling back a bad guest, running two VMs on
different images, sharing one image between VMs that were created from it.

Open: whether images are named and content-addressed (the built toplevel is
already a hash, so an image could just be its store path plus a disk
created from it) or whether a VM simply remembers what it was built from;
whether disks are copy-on-write from a base image rather than a full copy,
which is what would make several VMs per image cheap; and where the
registry of both lives.

## Deployment, once the single-root-agent milestone lands

Settled, so that it is not re-derived:

- The guest config in `guest/` is the *default* the package ships, not a
  live config. Editing it changes what the next built package installs and
  nothing that is running.
- Outside updates go over ssh into a running VM, the way NixOS already does
  this: build on the host, `nix copy` the closure in, activate there,
  transport being the vsock ProxyCommand. Built: `vmcav deploy`. The closure
  goes in before the host's out-links move, so a failed activation still
  leaves a VM that boots what was deployed. A kernel change needs a restart
  and says so.
- What a VM boots is read off its own disk at `start`: the system profile the
  guest activated names the toplevel, and that names the kernel and initrd.
  So an in-guest `nixos-rebuild switch` survives a restart with no host-side
  sync, and a disk boots on a host whose store never had the closure (a gc, a
  fresh machine), because the kernel and initrd can be pulled back out of the
  disk. The host's out-links stay as gc roots, not as the source of truth.
- A stopped VM cannot be updated. Accepted restriction: start it and deploy,
  or `reformat` to the shipped default.
- Inside updates are just the agent running `nixos-rebuild switch` as root.
- The repo therefore stops being load-bearing: it is the project the agent
  works on, not where the guest config comes from. `CAVE_GUEST_REF`, the
  hardcoded `agent-1`, and the spec-commit state all go away with it.

Done: the host consumes what an agent pushes. `vmcav review` and
`vmcav approve` are the last hop, and `vmcav status` says when work is
waiting. Fast-forward only, so a base that has moved on sends the agent back
to rebase rather than growing a merge nobody tested. Both verbs work in the
human's checkout against the `cave` remote, the way `main` does it: review
checks the agent's branch out, approve lands it here and in the shared repo.
There was a `vmcav diff` and it is gone -- reviewing means reading the code
with the tools you already have, and `main` never had one.

Open: whether the spec ref means "the agent's branch" (agent deploys without
approval, human is the outsider) or "what the VM runs" (both move it, the
human's deploy is a first-class command). That choice decides the other two.

Reopened by the control RPC, and by phase 4 landing after the above was
settled: whether deploy stays a push. `nix copy` over ssh was the only way to
get a closure into the guest when it was decided. The guest now has the host's
store on vsock 7100 and verified it works with crinj absent, so deploy could
invert: name a toplevel over the control RPC, let the guest substitute the
closure itself, then activate. That is a pull, it removes deploy's dependency
on ssh, and it makes sshd a debugging affordance rather than part of the
control plane.

Which raises the question that decides how much of this is worth doing:
whether the guest keeps sshd at all. Attach does not need it (a raw vsock
stream plus a resize message is what a pty relay is), session sync does not
need it, and ssh's authentication is close to redundant on a link whose host
end is a unix socket only the VM's owner can open, and who can already power
the guest off through the API socket. Keeping both a control RPC and an ssh
control path is the outcome to avoid; keeping sshd purely for a human is
defensible and should be a decision rather than a leftover.

## Nothing runs the Mac host end to end

**Access to the Mac has since gone away, for the time being.** Nothing below
can be re-checked until it comes back, so treat every macOS claim in this
repo as of its recorded date and do not add more. Work proceeds from the VM
downward on the Linux host, which is unaffected: the VMM split is already
behind one interface, so the vfkit backend keeps building and keeps being
unverified.

The hardware was no longer the blocker. The Apple silicon host that
`vm-cave-cross-platform.md` took its evidence from is provisioned and
reachable over ssh, with vfkit v0.6.4 installed and working and 170 GB free.
What is on it is the residue of that validation run: the guest artifacts it
booted (`Image`, `initrd`, `nixos.img`, `seed.img`), a hand-written
`boot.sh`, and a `cave-vm` binary. The Lima VM's disk is still there but
`limactl` is not, and the coding-cave checkout and crinj were deleted once
the findings were written up.

So every macOS result on record was produced by hand, and nothing on that
machine can be re-run from this repo. `vmcav` is the reason: it is a Linux
shell script that breaks in the seven places
`vm-cave-cross-platform.md` enumerates (no `/proc`, no `setsid`, BSD
`readlink`, no `socat`/`mke2fs`/`gpg`, `x86_64-linux` hardcoded), so "run the
tool on the Mac" is not a thing that can be attempted today, only
approximated by driving vfkit directly again.

Open: whether to keep re-validating by hand against the current guest, which
is cheap and proves only what is already documented, or to pull the VM
lifecycle into `cave-vm` now, which is what the cross-platform doc concludes
and what would make the Mac a first-class host rather than a demo. The second
is real scope and lands ahead of where `vm-cave-design.md` currently puts it.
Also unresolved: the aarch64 guest needs a Linux builder, so re-provisioning
Lima is a prerequisite either way.

## Rosetta, optional and later

`vfkit --device rosetta` shares Apple's translation runtime into the guest
over virtio-fs; the guest registers it through `binfmt_misc` and then executes
`x86_64` Linux binaries on an `aarch64` guest.
`vm-cave-cross-platform.md` treats it as what would make the two hosts
behaviourally equivalent rather than merely similar.

Deferred, and running the guest natively is the position: an aarch64 guest on
an aarch64 host is the straightforward configuration, and nix content is built
per system and equivalent either way. What Rosetta buys is the bytes nix never
sees, npm optional dependencies shipped as x86 binaries, pip wheels,
Playwright's browser download, `go install` output, container images, plus
`nix.settings.extra-platforms` for the cases where aarch64 cache coverage is
thin enough to mean compiling on four cores.

So the trigger to build it is evidence rather than principle: an agent blocked
on a toolchain that ships x86-only. Two things to establish first, since
neither shows up where it would be noticed: Rosetta's instruction coverage (no
AVX historically, AVX2 in recent macOS) and whether it is installed on the host
at all.

## Where the effort should go, given the field

A survey of the agent-sandbox landscape (recorded in `agent-sandbox-field.md`)
changes the priority order, without changing the plan.

Docker Sandboxes and the microVM crowd have commoditised the isolation
boundary, which is most of phases 0-3 and 10. Three things nobody else does,
and which no competitor's product can be configured into:

- **A nix store shared across agents.** Nothing in the survey uses nix at all;
  every one is OCI images or a stock distro. Note this is also unsolved on
  `main` today, where store dedup was removed and each cave has a private
  store. One guest with a stock multi-user daemon is the answer `main` gave
  up on, and the nix package cache and `pkgdd` have no equivalent anywhere.
- **Git as the coordination mechanism with a host-side gate.** Agent
  branches, `pre-receive`, `cav approve`. Docker's `--clone` is fetch-based
  with no gate; Dagger's `container-use` is the nearest and is containers
  only.
- **Inter-agent comms.** Docker forbids it outright between sandboxes.

The reframe this section was written around, that on Linux bwrap already gives
caves so the VM buys only macOS support and the one-store model, no longer
holds. copy.fail and dirty-frag settled it: bwrap is not the host boundary
any more, because the host boundary may not be a kernel one. The VM is the
boundary on every platform, and there is no second backend to keep
sandbox-agnostic.

What survives is that out-hardening Docker is not the point and not
achievable on their budget. The store and the coordination gate are still the
things nobody else has.

bwrap does not disappear: inside the guest it is still what separates caves
from each other. That is now a blast-radius choice, where an escape reaches
the other agents in one VM, and no longer the thing standing between an agent
and the host.

So: keep the VMM layer boring and small (two process-backends, skip Windows),
stop treating cold boot as a headline metric since one VM per host boot means
never competing on it, and prioritize phase 4 (the nix package cache) and
phase 8 (pkgdd) over 1-3. Land the git bridge (phase 7) early, since the
approval gate is the thesis.

Phase 4 is done: with crinj absent, a closure the guest did not have arrives
from the host's store over vsock and verifies, and a host-absent path fails
rather than reaching anything. That leaves phase 8 as the remaining half of
the argument, since language package caches are where an agent's bytes
actually go.

Closed: the argument for staying sandbox-agnostic, keeping bwrap on Linux and
using `sbx` or Apple's `container` on macOS. It was far less code and it
always failed on the shared store needing one VM; it now also fails on the
boundary itself, which is the point above.
