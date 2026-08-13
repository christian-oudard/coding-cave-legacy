# Giving an agent ssh access to a remote host

Status: paused, deliberately. The requirement is real and the options are
understood; it is blocked on per-client crinj policy, and granting the
exception without that would hand every cave the same access. Recorded so the
findings are not re-derived.

## The requirement

Give one agent ssh access to exactly one remote host, without giving it
access to the host laptop, and without leaking a tailscale account. The
mechanism has to work for both ordinary key-based ssh and Tailscale SSH.

## What ssh transport needs from crinj: nothing

crinj tunnels raw TCP through CONNECT, so ssh already works through it with no
crinj change. Worth knowing that crinj's `200` response to a CONNECT is
optimistic and proves nothing about whether the destination is reachable.

## The two credential shapes

The distinction that matters is not how strong the credential is, it is what
revocation costs:

> closing a compromised agent session stops the access, but exfiltrating a
> credential doesn't.

A private key copied into the cave is exfiltratable: a compromised agent
shares it and access outlives the session. A forwarded socket is
session-bound: it is a capability the agent holds only while the session is
open, and closing the session ends it.

That argues for forwarding a socket rather than copying a key. Which leads
directly to the finding below.

## Finding: a shared ControlMaster socket is a network pivot

Proven against a throwaway local sshd, not reasoned about. If the host runs an
ssh ControlMaster and the cave is given its control socket, the cave can ask
the master to open **remote port forwards**, and:

**No OpenSSH client option prevents it.** `ClearAllForwardings`,
`PermitRemoteOpen=none` and `ExitOnForwardFailure` were each tested and none
of them block a forward requested over the mux protocol. The mux protocol has
its own message types (`MUX_C_OPEN_FWD` among them) and they are not gated by
the client-side options that constrain the command line.

So sharing a ControlMaster socket inherently grants the ability to open
network paths back through the host. Any design built on socket forwarding
needs a mux-protocol filter in front of it, permitting `MUX_C_NEW_SESSION` and
`MUX_C_ALIVE_CHECK` and refusing the forward messages.

Note also that ssh-agent forwarding is a different and weaker tool than it
looks: the agent protocol has no destination awareness, so a forwarded agent
authenticates to *anything* the agent can reach, and it cannot open forwards
at all.

## Options

1. **Copy a private key into the cave.** Simplest, and the exfiltratable
   shape. Restrictable at the far end with `authorized_keys` options
   (`restrict`, `from=`, `expiry-time=`, `command=`).
2. **Forward the host's ssh-agent.** No destination scoping; the agent signs
   for any host it holds a key for.
3. **A second ssh-agent holding exactly one key, forwarded.** Scopes by key
   rather than by host, which is the right granularity, since the question is
   always "which credential" not "which hostname".
4. **`--ssh-key`, with the cave managing a per-cave agent.** Option 3 with the
   lifecycle owned by the tool rather than the user. Only supports key-based
   ssh, not Tailscale SSH.
5. **Tagged Tailscale auth key, guest joins the tailnet.** Rejected: the
   target here is a shared-in node from another tailnet, so its Tailscale SSH
   policy is not ours to write, and tags and ACLs on our side cannot scope it.
6. **`--ssh-master`, forwarding a filtered ControlMaster socket.** The only
   option that covers Tailscale SSH, because the host's existing
   authenticated connection is reused rather than re-authenticated. Requires
   the mux filter above.

The live choice is 4 or 6. 4 is much less work and covers only key-based ssh.
6 covers both but requires writing a mux-protocol proxy, and the forwarding
attack above is the reason that proxy cannot be skipped.

## Why it is blocked

Whichever option wins, the cave needs to reach one private destination, and
crinj's destination policy has no per-client exceptions. Adding
`--allow-dest` to the shared instance grants it to every cave at once, which
is the opposite of the requirement. See `crinj/docs/per-client-policy.md`.

The sequencing agreed at the time was deny first, per-client later. Deny
shipped as crinj `926924a` and `fa623ea`.
