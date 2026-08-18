# What crosses the VM boundary

The boundary is the product, so every crossing is named here. Anything not on
this list does not exist, and adding one is a decision rather than an
implementation detail.

## The channels that physically exist

Five, and no others. The guest has no NIC, no virtio-fs, and no host paths.

- **Root disk** (`virtio-blk`, rw). The guest's whole world, including its nix
  store and every agent's worktree. The host writes it only while the VM is
  stopped (`image create`, `reformat`) and reads it only while stopped, with
  `debugfs`, unprivileged and read-only.
- **Seed disk** (`virtio-blk`, ro, `/dev/vdb`). The one host-to-guest channel
  that carries secrets, written before boot and never again.
- **Console** (`virtio-serial` to a file). Guest to host, one way,
  unstructured. The boot log, and how `stop` knows the guest reached power-off.
- **The vsock plane.** Six ports, fixed at boot, listed below.
- **The VMM control socket.** Host-side only, and not a guest channel at all:
  the guest cannot see it. Power-button, pause, resume.

## What goes in

- **Guest identity and configuration**, by seed disk, at boot: the host's ssh
  public key as `authorized_keys`, crinj's CA (baked into the guest's trust
  store, so a missing one is a hard error), the host's git `user.name`,
  `user.email` and `signingkey`, a `nix.conf` fragment naming the host's
  substituter and its public key, the gpg public keyring, and claude's
  `claude.json` and `credentials.json`.
- **The repo**, guest-initiated over vsock 7300, by cloning the shared bare
  repo from the host's git daemon.
- **Nix store paths**, guest-initiated over vsock 7100, by substituting from
  the host's store. This is a real binary cache, so signatures verify and a
  host-absent path fails rather than reaching anything.
- **The guest's own system closure** on deploy. Today a push, `nix copy --to
  ssh://`; see the open question below.
- **Session records**, restored so an agent's `/resume` finds them, and shared
  so agents can read each other's.
- **Agent lifecycle**: start this agent, stop that one, and the prompt a new
  cave starts from.
- **The time**, which nothing does today and which is first in
  `vm-cave-todo.md`.
- **Terminal keystrokes**, while a human is attached.

## What comes out

- **Work, and only as git.** The agent pushes to the host's git daemon on
  vsock 7300, into the project's shared bare repo, where `pre-receive` gates it
  by agent id and `cav approve` fast-forwards it into the real repo. This is
  the sanctioned output path and it should stay the only one: anything that
  makes a second way for bytes to become host state is competing with the
  approval gate.
- **Requests to the internet**, over vsock 7200 to crinj, which is the only
  path out of the machine and logs every CONNECT.
- **Signature requests**, over vsock 7400 to the broker, one operation at a
  time. The secret key never crosses, and the guest sees an operation rather
  than a key or the gpg-agent protocol.
- **Session transcripts**, pulled by the host, appended to host state, never
  replaced by anything shorter or with a different prefix.
- **The guest's claude credential and config**, pulled by the host so a
  reformat costs neither a login nor another round of dialogs. This is the one
  place guest-authored bytes become durable host state outside git, so it fails
  closed: every token must match crinj's placeholder shape, checked field by
  field, or nothing is kept.
- **Whatever a human asks for by name**, with `cav cp`, which is unbuilt here
  and is the counterpart of `main`'s. The transport needs no work: `scp` over
  the ssh config's vsock ProxyCommand already copies in both directions and
  recursively, verified against the running guest. What is missing is the
  addressing, which is the part that was ever interesting: `cave:<path>`
  relative to the agent's worktree, `~/` for its home, `@<n>` once there is
  more than one agent, plus overwrite prompts and completion.

  One of `main`'s guardrails does not carry over, and should not be copied
  without noticing why it existed. `cav cp` refuses to copy a symlink out of a
  cave because a container shares the host's filesystem namespace, so a
  symlink planted in the cave can name a host path and read it through you.
  A guest symlink can name only guest paths. Confirmed rather than assumed: a
  symlink to `/etc/shadow` copied out of the VM yields the *guest's* file.

- **The boot log**, by console file.
- **Which system the guest actually booted**, read off its own disk with
  `debugfs` while stopped, so a guest that rebuilt itself comes back as itself
  and a disk still boots on a host whose store was garbage-collected.

## The rules these follow

1. A guest-initiated port speaks a real protocol to a host service that already
   exists: a binary cache, an HTTP proxy, a git daemon, a signing broker. It is
   never a general-purpose channel.
2. A host-initiated port is control, and `cav` is its only client.
3. Secrets go in exactly once, by seed disk, so there is one place to audit.
   They do not come back out; the gpg key is the model, where the guest gets an
   operation rather than the key.
4. Work the *agent* publishes leaves as git or it does not leave. This is
   about one actor, not about bytes: the gate exists because the agent must
   not be able to make host state on its own. A human reaching in and taking a
   named file out is a different actor doing a deliberate thing, and `cav cp`
   on `main` is exactly that. It does not compete with the gate, and the VM
   should have it.
5. No inbound vsock port may be allocated after boot, because under vfkit the
   port table is the launch argv. Agents are multiplexed by id inside a
   request, never by port.
6. ssh is a debugging escape hatch for a human, not a mechanism any command
   depends on.

## What changes

**The control RPC on 7001 takes over everything routine**: `status`, agent
start/stop/list, the terminal attach stream, the clock push, triggering a
session sync, and reading the guest's claude state. One typed protocol, one
client, and a cold call costs 1-2ms against ssh's 85ms, which is the
difference between `status` feeling instant and feeling slow once it asks per
agent.

**Attach rides the same port**, on a second connection opening with an
`ATTACH <id>` preamble, because a pty relay is a byte stream plus a resize
message and that is what a vsock connection already is.

**ssh stays, for a human.** `cav shell` into root in the guest is worth
keeping and costs nothing as long as no command needs it. The rule is that
removing sshd should break nothing except a person's ability to poke around.

**`debugfs` stays too**, and not as a fallback: a stopped VM has nothing
running to answer an RPC, and the guest most likely to need reading is one too
broken to boot. Two mechanisms here is correct, and the split is by whether
the VM is running rather than by whether the first one failed.

**7100, 7200, 7300 and 7400 do not change.** They are guest-initiated, they
speak protocols the host already serves, and the control plane has no business
in the middle of them.

## Open

- **Deploy: push or pull.** It is `nix copy` over ssh today, which was the only
  way in when it was settled, and it is the last thing besides a human that
  needs sshd. The guest has had the host's store on 7100 since phase 4 landed,
  so deploy could invert: the RPC names a toplevel, the guest substitutes the
  closure itself, then activates. Inverting is what makes rule 6 true rather
  than aspirational, and it is the one real build item on this page.

  To be clear about what "pull" means here, since 7100 and 7300 are easy to
  run together: 7100 is the nix binary cache and a binary cache only ever
  works one way, with the client fetching store paths it names. Git on 7300 is
  the two-way one, and it is already guest-initiated in both directions, since
  fetch and push are the same wire protocol over one connection. So inverting
  deploy invents no channel and no direction. It uses the cache that already
  exists for one more thing, the guest's own system closure, which is store
  paths and not git content and so could never have travelled over 7300.
- **Whether the seed disk stays the shape it is.** It is a filesystem image
  built with `mke2fs -d`, which is one of the two reasons the host half is not
  portable, and everything on it is small. Handing the same files over the
  control RPC at first boot would remove the dependency and add a bootstrap
  ordering problem, since `authorized_keys` and the CA are needed before
  anything is up to ask.
