{ config, pkgs, lib, modulesPath, claudeCode, caveEnvInputs, ... }:

let
  # Where session sockets live, one per agent, named after it. /run, so a
  # reboot cannot leave a stale one behind.
  #
  # Not root-only any more: an ordinary agent's session runs as its own user
  # and binds its own socket in here, so the directory is root:cave 1770.
  # Sticky, because group-writable without it would let one agent delete
  # another's socket, and the socket is how a session is reached.
  sessionDir = "/run/cave/sessions";

  # Where an agent's cave environment puts its merged package set. Must match
  # userenv.profileLink in the Go package, which is what creates the link;
  # this is the end that reads it. Deliberately not .nix-profile, which is
  # nix's own per-user profile: colliding with it would make `nix profile
  # list` report an environment nobody installed that way.
  caveProfileLink = ".cave-profile";

  # How many ordinary agents this guest can hold at once.
  #
  # A declared pool rather than users minted on demand, because a user created
  # at runtime is exactly the state this guest is supposed not to accumulate:
  # it would survive in /etc/passwd rather than in this file, and a reformat
  # would silently produce a guest that differs from its own config. The cap
  # is honest and cheap to raise; ids are allocated lowest-free-first, so they
  # stay inside it rather than drifting upward.
  maxAgents = 8;
  agentIds = lib.range 1 maxAgents;
  agentUser = id: "agent${toString id}";
  # Fixed, so a worktree's ownership still means something after a rebuild.
  agentUid = id: 3000 + id;

  # Where an agent's worktrees really live, and the only place a number
  # appears. An agent never sees this path: its session binds
  # <worktreeRoot>/<id>/projects over /projects, so every agent works in
  # /projects/<slug> and none of them can name, or collide with, another's.
  #
  # This is what `main` gets from bwrap -- one path that means a different
  # tree per agent -- and it is why putting the id in the path the agent uses
  # was the wrong fix (docs/vm-cave-todo.md). The id belongs to the bookkeeping
  # underneath, where a human debugging two agents wants it, not to the
  # working directory of somebody who has only one tree to work in.
  worktreeRoot = "/var/lib/coding-cave/agents";
  # In a unit, where %i is the agent id. The source of the bind below.
  agentProjects = "${worktreeRoot}/%i/projects";

  # Who an agent is, worked out from the session's own environment file so
  # that every unit serving one agent agrees about it. The host says which
  # agent this is and whether it is the root one; what that means -- which
  # user, which home, which tree -- is the guest's to decide, and deciding it
  # in one place is what keeps a unit from seeding a home the agent does not
  # read. That failure is silent: the agent comes up with no credentials.
  agentIdentity = ''
    : "''${CAVE_AGENT_ID:?no agent id}"
    if [ "''${CAVE_AGENT_ROOT:-}" = 1 ]; then
      agent_user=root
      agent_home=/root
    else
      agent_user="agent$CAVE_AGENT_ID"
      agent_home="/home/$agent_user"
      # There is no agent0 user -- 0 is the root agent's number -- and there is
      # no agentN beyond the ones declared below. Either way the next unit to
      # reach an `install -o` dies with "install: invalid user 'agent0'", which
      # names neither the cause nor the fix, and takes the session down with it
      # as a dependency failure: the observed shape is an agent that never
      # comes up and a host that reports the guest as not answering. Say what
      # actually happened, here, where both halves of the answer are in scope.
      if ! ${pkgs.coreutils}/bin/id -u "$agent_user" >/dev/null 2>&1; then
        echo "no user $agent_user for agent $CAVE_AGENT_ID:" \
             "it was started without CAVE_AGENT_ROOT=1 (cave-guestd start --root)," \
             "or with an id this guest declares no user for" >&2
        exit 1
      fi
    fi
    # The real tree, which only these units name. The agent sees /projects.
    agent_tree="${worktreeRoot}/$CAVE_AGENT_ID/projects"
  '';

  # cave-guestd: the process that owns one agent's pty, and the client that
  # attaches to one. It replaced dtach, which held a pty but buffered nothing
  # and never resized a session to its attaching client, so every reattach
  # came back to a blank screen at the wrong width.
  #
  # The Go module lives outside this flake's directory, so its source arrives
  # impurely, the same way the crinj CA does: from the host build via
  # $CAVE_VM_SRC, and from an in-guest redeploy out of the agent's own
  # checkout. No silent fallback, a guest without the daemon has no agent.
  caveVmSrc =
    let
      env = builtins.getEnv "CAVE_VM_SRC";
      # The rebuilding agent's own worktree, whichever agent that is. Not a
      # numbered path: this eval runs inside that agent's session, where
      # /projects/<slug> is a bind of its tree and no other agent's is
      # reachable, so naming an id here would name the wrong tree as soon as
      # the root agent was not the one it was written for.
      #
      # The consequence to know: a rebuild run from `vmcav shell` rather than
      # from an agent's session is outside every namespace, so /projects is
      # empty and this falls through to the throw. That is the right failure
      # -- from out there, there is no answer to "which agent's checkout" --
      # and CAVE_VM_SRC is how to say which one you meant.
      inGuest = "/projects/coding-cave/cave-vm";
    in
    if env != "" then env
    else if builtins.pathExists inGuest then inGuest
    else throw ("cave-vm source not found: build from inside an agent's "
      + "session, where ${inGuest} is that agent's checkout, or export "
      + "CAVE_VM_SRC and build with --impure");

  # Guest-side leg of the service plane, one shape for all four outbound
  # bridges: a localhost port the rest of the guest talks to, carried over
  # vsock to the host service behind it. CID 2 is the host (VMADDR_CID_HOST).
  # Re-exposing each service on localhost is what lets the proxy env, the nix
  # substituter and the agent's git remotes stay ignorant of vsock entirely.
  #
  # Socket-activated rather than a long-lived socat, because the alternative
  # was a race: systemd calls a `socat TCP-LISTEN` unit active the instant the
  # process execs, not when it has bound the port, so a unit ordered after it
  # can still arrive to find nothing listening. `clone-repo` failing at boot
  # and taking the agent down with it is the observed shape of that
  # (docs/vm-cave-todo.md). systemd binds these itself at sockets.target,
  # before anything that depends on them runs, so the ordering is a guarantee.
  #
  # It also deletes machinery: no Restart=always papering over a host bridge
  # that was not listening yet, and no requirement that the host end be up
  # before the guest can queue a connection, because the kernel holds it.
  bridgeSocket = port: description: {
    inherit description;
    wantedBy = [ "sockets.target" ];
    # Ordered ahead of everything that dials one of these on startup, which is
    # what a plain service could claim but not deliver.
    before = [ "vm-root-agent.service" ];
    socketConfig = {
      ListenStream = "127.0.0.1:${toString port}";
      # Accept, so systemd hands each connection to its own socat on stdin,
      # which is what socat's own `fork` did anyway.
      Accept = true;
    };
  };

  bridgeService = vsock: {
    description = "Carry one connection to host vsock ${toString vsock}";
    # One instance per connection means transient failures accumulate: a peer
    # that resets mid-transfer leaves socat exiting non-zero, and without this
    # the instance sits in `failed` forever. That is not merely untidy. A unit
    # left failed makes every later switch-to-configuration report the whole
    # deploy as failed, which is the same trap logrotate sprang below, and it
    # buries a real failure in a list of dead connections.
    unitConfig.CollectMode = "inactive-or-failed";
    serviceConfig = {
      ExecStart = "${pkgs.socat}/bin/socat STDIO VSOCK-CONNECT:2:${toString vsock}";
      StandardInput = "socket";
    };
  };

  caveGuestd = pkgs.buildGoModule {
    pname = "cave-guestd";
    version = "0";
    src = builtins.path { path = /. + caveVmSrc; name = "cave-vm-src"; };
    vendorHash = null; # the module has no dependencies, by design
    subPackages = [ "cmd/cave-guestd" ];
  };

  # --- The cave environment (docs/vm-cave-design.md phase 9) ---
  # The guest's half of it: the modules mkCaveEnv composes ahead of a user's
  # cave.nix, as a store path of their own so the wrapper flake in
  # /var/lib/coding-cave/env can name it as an input.
  #
  # Its store path is also the version. `main` needs a caveSourceHash --
  # a hash it computes over its own cave modules at cav-build time -- to
  # notice that one of them was edited; here the path changes when the files
  # do, so the same fact is free.
  caveEnvSrc = builtins.path { path = ./env; name = "cave-env"; };

  # What an environment is built against, taken from this guest's own flake
  # inputs rather than fetched. That is what makes `cave-env build` work with
  # the proxy down and guarantees an agent's claude is the guest's claude: a
  # second nixpkgs would mean a second glibc in one VM, with PATH order
  # deciding which an agent got.
  #
  # storePath, not plain interpolation, and this is the whole point of the
  # derivation: a bare string naming /nix/store/... creates no dependency, so
  # nothing would keep these alive and a garbage collection would leave the
  # guest unable to build an environment without going back to the network for
  # a different revision. Linking them into a path the system closure holds is
  # what roots them. (The eval is always --impure here, which is what
  # builtins.storePath requires.)
  caveEnvPins = pkgs.runCommand "cave-env-pins" { } ''
    mkdir -p $out
    ln -s ${builtins.storePath caveEnvInputs.nixpkgs} $out/nixpkgs
    ln -s ${builtins.storePath caveEnvInputs.home-manager} $out/home-manager
    ln -s ${builtins.storePath caveEnvInputs.claude-code} $out/claude-code
  '';

  # `cav env` from the inside, for iterating on the environment without
  # starting anything. The host half of this is phase 9's remaining work: a
  # control-plane endpoint that pushes cave.nix in and a `cav env build` that
  # calls it. Until then the spec is edited in here, which is exactly what the
  # root agent is for.
  caveEnvCli = pkgs.writeShellScriptBin "cave-env" ''
    set -eu
    spec=/var/lib/coding-cave/env/cave.nix
    case "''${1:-}" in
      build)
        shift
        exec /run/current-system/sw/bin/cave-guestd env build "$@"
        ;;
      edit)
        # The template is not part of the cave-env store path on purpose:
        # editing it would otherwise change that path, and every agent's
        # environment would rebuild because a comment moved.
        install -d -m 755 /var/lib/coding-cave/env
        [ -f "$spec" ] || install -m 644 ${./default-cave.nix} "$spec"
        ''${EDITOR:-vim} "$spec"
        echo "cave-env: run 'cave-env build' to apply it, or start a session."
        ;;
      show)
        echo "spec:   $spec"
        echo "result: $(readlink -f /var/lib/coding-cave/env/result || echo '(not built)')"
        echo
        echo "settings.json as this agent has it:"
        cat "$HOME/.claude/settings.json" 2>/dev/null || echo "  (none)"
        ;;
      *)
        echo "usage: cave-env build [-force] | edit | show" >&2
        exit 2
        ;;
    esac
  '';

  # One definition of "run the agent", used by the boot unit and by the
  # daemon when it has to restart a dead session, so the two cannot drift
  # into launching claude differently.
  caveAgent = pkgs.writeShellScriptBin "cave-agent" ''
    # Which project, from the session's own environment. No default: an agent
    # started without one has nothing to work on, and landing it in some other
    # project's worktree is worse than failing.
    : "''${CAVE_PROJECT:?no project}"

    # The cave environment, if this agent has one materialized. Two things
    # come out of it, and both have to happen here rather than in the unit:
    # the unit is written once for every agent and this is per-home.
    profile="$HOME/${caveProfileLink}"
    if [ -d "$profile/bin" ]; then
      # Ahead of the system floor, so a cave.nix can put a newer claude, or a
      # tool that shadows one of the system's, in front of it. That is the
      # point of having a user tier at all.
      PATH="$profile/bin:$PATH"
      export PATH
    fi
    # home.sessionVariables. A systemd unit reads neither hm-session-vars.sh
    # nor environment.sessionVariables, so the built env.json is where they
    # come from. `set -a` makes each assignment an export without naming them.
    vars=/var/lib/coding-cave/env/result/env.json
    if [ -r "$vars" ]; then
      set -a
      # shellcheck disable=SC1090
      . <(${pkgs.jq}/bin/jq -r 'to_entries[] | "\(.key)=\(.value|@sh)"' "$vars")
      set +a
    fi

    # The agent's own worktree, which is simply the project: the session's
    # mount namespace has already bound this agent's tree over /projects, so
    # there is no other agent's checkout here to land in by mistake and no id
    # to get wrong. Whoever is reading this in a shell sees the same path.
    cd "/projects/$CAVE_PROJECT"

    # Through PATH, so the environment's claude wins when there is one, and
    # the system's answers when there is not. Deliberately not the pinned
    # ${claudeCode} store path this used to name: that made the version an
    # agent runs a property of the deployed system alone, with no way for a
    # cave.nix to move it, and it is the same self-update bug the session
    # unit already avoids one level up.
    claude_bin=${claudeCode}/bin/claude
    if command -v claude >/dev/null 2>&1; then
      claude_bin=claude
    fi

    set -- --dangerously-skip-permissions "$@"

    # Pick up where this agent left off.
    #
    # Stopping an agent and starting it again used to hand you a blank
    # conversation, with the old one reachable only by opening the picker and
    # recognising it. That is a poor trade for something the agent already
    # knows: an agent is a durable thing -- an id, a worktree, a branch -- and
    # a restart is not a new one.
    #
    # No bookkeeping is needed for it, and that is the point. claude files a
    # transcript under the directory the session ran in, and an agent's
    # directory is now stable: /projects/<slug>, the same path on every start,
    # with no id in it since the worktree moved behind the mount namespace. So
    # "the most recent conversation in this directory" is already exactly
    # "what this agent was last doing". Nothing has to record a session id or
    # keep it in step with the registry.
    #
    # Conditional because --continue with nothing to continue is an error, and
    # an agent that will not start is worse than one that starts blank. The
    # encoding is sessions.EncodeProject: slashes to dashes, nothing else.
    #
    # And the condition is not sufficient, which is why the retry below
    # exists. A transcript on disk is not the same question as a conversation
    # claude will continue: it keeps its own idea of what is resumable, so a
    # file can be there and `--continue` still answer "No conversation found
    # to continue" and exit 1. The case that makes this more than theory is a
    # restore -- records copied back from the host's archive into a fresh home
    # are exactly transcripts on disk that this guest has never had a session
    # for, so after a reformat every agent would have refused to start.
    transcripts="$HOME/.claude/projects/$(printf '%s' "/projects/$CAVE_PROJECT" | tr / -)"
    # Three cases, not two. `--continue` takes the last conversation, which is
    # the right default and is not always the one meant: an agent with a
    # month of sessions has one you want and several you do not, and picking
    # it was only possible from inside, in claude's own picker. The host can
    # name one now, and it arrives here as an id in the session's environment.
    #
    # Named beats last, and both lose to --fresh: asking for a blank
    # conversation is the more specific request of the two.
    resume=()
    what=""
    if [ -z "''${CAVE_AGENT_FRESH:-}" ]; then
      if [ -n "''${CAVE_AGENT_SESSION:-}" ]; then
        resume=(--resume "''${CAVE_AGENT_SESSION}")
        what="session ''${CAVE_AGENT_SESSION}"
      elif [ -n "$(find "$transcripts" -name '*.jsonl' -print -quit 2>/dev/null)" ]; then
        resume=(--continue)
        what="the last conversation"
      fi
    fi

    if [ "''${#resume[@]}" -gt 0 ]; then
      began=$(${pkgs.coreutils}/bin/date +%s)
      "$claude_bin" "''${resume[@]}" "$@"
      rc=$?
      [ "$rc" -eq 0 ] && exit 0
      # A refused resume fails immediately and before there is a session to
      # lose; a session somebody actually worked in does not end in under ten
      # seconds. So a quick non-zero exit is the resume being refused, and
      # anything slower is the agent's own exit and stays the agent's own exit
      # -- relaunching that would restart a session the human just left.
      if [ "$(( $(${pkgs.coreutils}/bin/date +%s) - began ))" -lt 10 ]; then
        echo "cave-agent: could not resume $what; starting a new one" >&2
        exec "$claude_bin" "$@"
      fi
      exit "$rc"
    fi

    exec "$claude_bin" "$@"
  '';

  # Redeploy the guest from inside it, without killing the agent doing it.
  #
  # This is the root agent's core loop (docs/vm-root-agent.md) and the one
  # operation here that can destroy its own caller. `nixos-rebuild switch` will
  # stop a live session for reasons that have nothing to do with the session
  # unit changing, so the rule that matters cannot be expressed as a property
  # of one unit. It is a property of the *plan*: a deploy from in here must not
  # stop or restart any session that is currently running.
  #
  # switch-to-configuration will tell you that plan if you ask it, so ask it
  # first and refuse on the answer. dry-activate builds the same closure and
  # prints exactly which units it would stop, restart, reload and start,
  # touching nothing. Two builds rather than one, and the second is a cache
  # hit, which is a small price for the failure it prevents: the alternative
  # is finding out by losing the terminal you were typing in, with the switch
  # dead half-applied at status 101 and a generation registered but never
  # activated.
  #
  # What it deliberately does not do is stop the agent from rebuilding while
  # other agents are running. Their sessions must survive it, and if they
  # would not, that is the bug -- so it names them and stops rather than
  # negotiating.
  caveRebuild = pkgs.writeShellScriptBin "cave-rebuild" ''
    set -eu
    ref="''${1:-/projects/coding-cave/guest#cave-guest-dev}"
    echo "cave-rebuild: planning $ref"
    # --impure because the crinj CA and the cave-vm source both arrive that
    # way; every caller of this flake already builds impurely.
    plan=$(${pkgs.nixos-rebuild}/bin/nixos-rebuild dry-activate \
             --flake "$ref" --impure 2>&1) || {
      echo "$plan" >&2
      echo "cave-rebuild: the build failed; nothing was activated" >&2
      exit 1
    }
    # "would NOT stop the following changed units:" is the line that says a
    # session is safe, and it must not be mistaken for the one that says it is
    # not. Anchored on the exact prefixes for that reason.
    risky=$(printf '%s\n' "$plan" \
      | ${pkgs.gnugrep}/bin/grep -E '^would (stop|restart) the following' \
      | ${pkgs.gnugrep}/bin/grep -oE 'cave-(root-)?session@[^ ,]+' || true)
    if [ -n "$risky" ]; then
      echo "cave-rebuild: this would stop or restart a live session:" >&2
      printf '  %s\n' $risky >&2
      cat >&2 <<'EOF'
  That kills the agent running in it, and if that is you, it kills this
  command halfway through activating. It is not something to retry.

  Usually it means a unit a session Requires= is being removed or has become
  RemainAfterExit: stopping it propagates the stop to the session. Leave a
  no-op behind for one generation instead of deleting it outright, and see
  the seed-claude-config tombstone in guest/configuration.nix.
EOF
      exit 1
    fi
    printf '%s\n' "$plan" | ${pkgs.gnugrep}/bin/grep -E '^would ' || true
    echo "cave-rebuild: no live session is touched; switching"
    ${pkgs.nixos-rebuild}/bin/nixos-rebuild switch --flake "$ref" --impure

    # What this did and did not do, said every time, because the difference is
    # invisible from in here and expensive to discover later.
    #
    # This guest carries no bootloader: the host injects kernel, initrd and
    # init= when it starts the VM, which is what lets a switch register and
    # activate a generation in place at all. The consequence is that in place
    # is all it is. The running system has moved; what the host boots has not,
    # and nothing reachable from inside the guest can change that. Stop the VM
    # and it comes back on whatever the host last deployed, with this
    # generation still on the disk and not selected.
    #
    # So a rebuild from in here is a live test of a configuration, not a
    # deployment of one. `vmcav deploy` on the host runs the very same
    # activation -- and now the very same refusal -- but also moves the host's
    # boot artifacts, which is the half that makes it survive a restart.
    cat <<EOF

    cave-rebuild: this is in place only.
      running now: $(readlink /run/current-system)
      host boots:  $(sed -n 's|.*init=\(/nix/store/[^ ]*\)/init.*|\1|p' /proc/cmdline)
    If those differ, a stop/start reverts to the second one. Making it stick
    is \`vmcav deploy\` on the host; a kernel change needs a restart either way.
EOF
  '';

  # Step into an agent's view of the guest: a root shell, or a command, inside
  # the mount namespace where /projects is that agent's worktree.
  #
  # This exists because the namespace is the one thing about the guest a plain
  # root login now gets wrong. An agent works in /projects/<slug> and nobody
  # else has that directory at all; from outside there are only the real trees
  # under the worktree root, filed by id. Both facts are worth keeping -- the
  # agent should not have to know its own number, and a human debugging two
  # agents should be able to see both -- so what is needed is a way across,
  # not a compromise between them.
  #
  # nsenter against the session's main pid, which systemd knows: the session
  # leader holds the namespace for exactly as long as the agent runs, so an
  # agent that has exited fails here with a sentence rather than with an
  # errno from a mount that is no longer there.
  caveEnter = pkgs.writeShellScriptBin "cave-enter" ''
    set -eu
    id="''${1:?usage: cave-enter <agent id> [command...]}"
    shift
    pid=0
    root=""
    for unit in "cave-session@$id" "cave-root-session@$id"; do
      pid=$(${pkgs.systemd}/bin/systemctl show -p MainPID --value "$unit.service" 2>/dev/null || echo 0)
      if [ "''${pid:-0}" != 0 ]; then
        case "$unit" in cave-root-session@*) root=1 ;; esac
        break
      fi
    done
    if [ "''${pid:-0}" = 0 ]; then
      echo "cave-enter: agent $id is not running." >&2
      echo "  Its worktrees, if it has any, are under ${worktreeRoot}/$id/projects" >&2
      exit 1
    fi

    # As the agent, not as root in the agent's namespace.
    #
    # The namespace alone answers "where are its files"; it does not answer
    # "what can it do with them", and root in there can read and write what
    # the agent cannot. So the question this command exists for -- what is
    # agent 2 seeing? -- came back answered by something that is not agent 2,
    # and a permission problem would look like no problem at all. The guest's
    # own CLAUDE.md warns about exactly this shape for tests run as root.
    #
    # The root agent stays root, because that is what it is.
    as=""
    who=root
    home=/root
    if [ -z "$root" ]; then
      uid=$(${pkgs.coreutils}/bin/id -u "agent$id" 2>/dev/null || echo "")
      gid=$(${pkgs.coreutils}/bin/id -g "agent$id" 2>/dev/null || echo "")
      if [ -z "$uid" ] || [ -z "$gid" ]; then
        echo "cave-enter: agent $id is running but has no agent$id user." >&2
        echo "  Refusing to enter as root, which would answer a different question." >&2
        exit 1
      fi
      as="--setuid=$uid --setgid=$gid"
      who="agent$id"
      home="/home/agent$id"
    fi

    # The environment as well as the uid.
    #
    # nsenter changes who you are and not what you are told you are, so
    # HOME came in from whoever ran this -- root, in the case that matters --
    # and every program that reads it went looking in /root. Seen the first
    # time an ordinary agent was entered for real: git as agent3 reported it
    # could not read /root/.config/git, which is true and is not a question
    # anybody asked. A tool that had found something readable there would
    # have answered about root's configuration instead, silently.
    #
    # Set rather than cleared, because an empty HOME is its own wrong answer:
    # programs fall back to the passwd entry, the cwd, or /, and which one is
    # per program.
    #
    # And the agent's own identity, which is the half that bites. CAVE_AGENT_ID
    # came in from the caller too, so entering agent 3 from the root agent's
    # shell left every program in there believing it was agent 0 -- including
    # cave-git, whose one job is to refuse to publish outside the running
    # agent's namespace. Demonstrated rather than reasoned about: a commit made
    # in agent 3's worktree while entered this way pushed a branch to
    # agent-0/*, and the shared repo accepted it, because by then nothing in
    # the chain disagreed.
    #
    # Dropped first and then set from the agent's own record, so a variable
    # the target does not have cannot survive from the caller -- entering an
    # ordinary agent from the root agent's shell must not leave
    # CAVE_AGENT_ROOT=1 behind.
    env_as="${pkgs.coreutils}/bin/env -u CAVE_AGENT_ID -u CAVE_AGENT_ROOT \
      -u CAVE_AGENT_FRESH -u CAVE_AGENT_SESSION -u CAVE_PROJECT -u CAVE_BASE \
      HOME=$home USER=$who LOGNAME=$who"
    envfile="${sessionDir}/$id.env"
    if [ -r "$envfile" ]; then
      while IFS= read -r line; do
        case "$line" in
          # Values here are an id, a project slug and a branch name, all
          # written by cave-guestd from checked input, so none has a space to
          # split on.
          [A-Z]*=*) env_as="$env_as $line" ;;
        esac
      done < "$envfile"
    fi

    # --wd=/projects, because standing anywhere else in an agent's namespace
    # is standing in the guest's filesystem with extra steps.
    if [ "$#" -eq 0 ]; then
      exec ${pkgs.util-linux}/bin/nsenter -t "$pid" -m --wd=/projects $as \
        $env_as ${pkgs.bash}/bin/bash -l
    fi
    exec ${pkgs.util-linux}/bin/nsenter -t "$pid" -m --wd=/projects $as $env_as "$@"
  '';

  # The guest's git, which pushes what it commits (guest/cave-git.sh).
  #
  # It shadows git rather than sitting in front of it on PATH: the agent's
  # unit, an ssh login and every subprocess claude spawns would each have to
  # get that order right, and a git that silently stopped syncing looks
  # exactly like a git that is working. Real git stays reachable at its own
  # store path, which is what the wrapper runs.
  caveGit = pkgs.symlinkJoin {
    name = "cave-git-${pkgs.git.version}";
    paths = [ pkgs.git ];
    postBuild = ''
      rm $out/bin/git
      substitute ${./cave-git.sh} $out/bin/git \
        --replace '#!/usr/bin/env bash' '#!${pkgs.bash}/bin/bash' \
        --replace '@REAL_GIT@' '${pkgs.git}/bin/git'
      chmod +x $out/bin/git
    '';
  };

  # git's gpg.program inside the guest. Signing is forwarded to the host
  # broker over the service plane, because the secret key deliberately cannot
  # reach the guest; everything else (notably verification) runs the real gpg
  # locally against the seeded public keyring.
  caveGpg = pkgs.runCommand "cave-gpg" { } ''
    mkdir -p $out/bin
    substitute ${./cave-gpg.py} $out/bin/cave-gpg \
      --replace '#!/usr/bin/env python3' '#!${pkgs.python3}/bin/python3' \
      --replace '@REAL_GPG@' '${pkgs.gnupg}/bin/gpg'
    chmod +x $out/bin/cave-gpg
  '';

  # `cav-host agent-tag`, the cave's byline, and the one piece of `main`'s
  # in-cave command surface a statusline actually depends on.
  #
  # It exists here because a statusline is not cav's to own. `main`'s default
  # is one line -- `exec cav-host agent-tag` -- and everything richer is the
  # user's own script overriding statusLine.command and composing on top of
  # this byline. A guest without the command does not merely lose a default:
  # it makes every statusline written against a container cave fail here, and
  # a statusline that fails renders as nothing with the reason nowhere.
  #
  # Same output as `main`'s cav_host.rs, deliberately, down to the "?" when it
  # cannot tell. There the name comes from CODING_CAVE_AGENT_NAME, which the
  # sandbox injects already joined; here the two halves arrive separately in
  # the session's environment file, so they are joined here instead.
  #
  # `bell` and `version` are not implemented. bell writes to /proc/1/fd/1,
  # which in a cave is the launching terminal and in this guest is systemd's
  # console -- the same code would ring a bell nobody is listening to, which
  # is worse than saying so.
  caveHost = pkgs.writeShellScriptBin "cav-host" ''
    case "''${1:-}" in
      agent-tag)
        printf 'Coding Cave - %s@%s\n' \
          "''${CAVE_PROJECT:-?}" "''${CAVE_AGENT_ID:-?}"
        ;;
      bell|version)
        echo "cav-host: $1 is not available in the VM cave." >&2
        exit 1
        ;;
      *)
        echo "usage: cav-host agent-tag" >&2
        exit 1
        ;;
    esac
  '';

  # One session, in the two forms it comes in. Everything an agent's session
  # is -- the pty, the mount namespace, the environment, what it depends on --
  # is here once, and the two instantiations differ only in who runs it.
  #
  # Two units rather than one, because `User=` is what has to differ and a
  # unit cannot decide its own user from its instance name. The alternative
  # was a session that starts as root and drops privilege itself, which the
  # pty rules out: cave-guestd opens the pty before it could drop, so the
  # slave would be root's and the agent could not reopen its own terminal.
  # Letting systemd set the user means the daemon, the pty and the agent are
  # all the same uid, and nothing has to be handed over.
  #
  # Which one an agent gets is recorded in its session env file, so
  # cave-guestd starts the right one and `cave-vm` stops the right one.
  mkSession = { root }: {
    description =
      if root
      then "Root agent session %i (claude as root, its pty held by cave-guestd)"
      else "Agent session %i (claude as agent%i, its pty held by cave-guestd)";
    after = [ "vsock-crinj.socket" "seed-agent-home@%i.service" "clone-repo@%i.service" ];
    wants = [ "vsock-crinj.socket" ];
    requires = [ "seed-agent-home@%i.service" "clone-repo@%i.service" ];
    # Still false, for the one case the split does not cover: this unit's own
    # definition changing. A session is exactly what may not be restarted
    # under a working agent, so a new binary reaches sessions started after
    # the deploy, and the live ones keep the one they have.
    restartIfChanged = false;
    # Deliberately not a list of store paths. This unit is not restarted by a
    # switch, by design, so anything resolved when it started is frozen for as
    # long as the daemon lives: an agent that rebuilt its own system would
    # still be running the git, nix and claude of the generation it started
    # under. /run/current-system/sw/bin follows the generation instead, so a
    # session begun after a deploy gets the new tools while live sessions are
    # left alone.
    path = [ "/run/current-system/sw" ];
    # systemd services do not read environment.sessionVariables, so the
    # agent's egress + TLS trust are set here explicitly. The crinj CA is
    # already in the system bundle (security.pki above); the two cert vars
    # are still needed because claude's runtime does not consult the system
    # bundle path on its own (verified: /login gets a cert-chain error
    # without SSL_CERT_FILE, and node subprocesses need NODE_EXTRA_CA_CERTS).
    environment = {
      HTTP_PROXY = "http://127.0.0.1:10255";
      HTTPS_PROXY = "http://127.0.0.1:10255";
      NO_PROXY = "localhost,127.0.0.1,::1";
      SSL_CERT_FILE = "/etc/ssl/certs/ca-certificates.crt";
      NODE_EXTRA_CA_CERTS = "/etc/ssl/certs/ca-certificates.crt";
      # A statement about the VM, not about the uid. claude refuses
      # --dangerously-skip-permissions as root unless it sees an
      # already-sandboxed environment, which is why this started as the root
      # agent's; it is set for every agent because the sentence it asserts --
      # this process is inside a sandbox -- is equally true of all of them.
      IS_SANDBOX = "1";
      # systemd sets HOME only for units with User=, so a root unit gets none
      # and every tool that resolves config through $HOME misbehaves under the
      # agent. git is the one that bites: it never reads the seeded
      # .gitconfig, so `git commit` fails with "Author identity unknown" even
      # though the seed installed it correctly. Stated for both, because a
      # home that disagrees with the one seed-agent-home@ wrote is the same
      # bug wearing a different uid.
      HOME = if root then "/root" else "/home/agent%i";
      # A unit has no terminal, so TERM is unset and anything curses-ish
      # falls back to dumb rendering: no colour, no cursor addressing, and
      # output that reads like a terminal with no scrollback. The daemon
      # passes its own environment to the agent, so it has to be stated here.
      # Only the type: the size comes from whatever terminal attaches.
      TERM = "xterm-256color";
      # Where a Wayland display socket goes. Every compositor requires this
      # and refuses to start without it, and a system unit has none, because
      # logind sets it only for a login session.
      #
      # Deliberately not /run/user/0, which is logind's to create and delete
      # for a root ssh login: RuntimeDirectory= below would take ownership of
      # that path and remove it when the session stopped, pulling it out from
      # under any `vmcav shell` that happened to be open. A path of this
      # unit's own shares with nothing, and carries the instance id, which is
      # what several agents in one guest need anyway.
      XDG_RUNTIME_DIR = "/run/cave-agent-%i";
    };
    serviceConfig = {
      # The session process stays in the foreground and exits with its agent,
      # so systemd tracks the real process and the unit's state is the
      # session's state rather than a guess about a pid after a double fork.
      Type = "simple";
      # The project and base this session was created for. Same file the
      # checkout unit reads, so the two cannot disagree about which worktree
      # the agent lands in.
      EnvironmentFile = "/run/cave/sessions/%i.env";
      # The session process's own path is pinned, because nothing can swap a
      # process that is holding a pty. The agent's is not: naming it through
      # the current system means a session started after a deploy picks up
      # whatever that deploy installed. Baking the store path here was a
      # self-update bug -- the agent could rebuild its own system, activate
      # it, and still launch the previous claude forever.
      ExecStart = "${caveGuestd}/bin/cave-guestd session -id %i -dir ${sessionDir} "
        + "-- /run/current-system/sw/bin/cave-agent";
      # Creates XDG_RUNTIME_DIR above and removes it when the session stops.
      # 0700 because the spec has it private to its owner, and compositors
      # check the mode rather than trusting the variable.
      RuntimeDirectory = "cave-agent-%i";
      RuntimeDirectoryMode = "0700";
      # An agent that exits stays exited. Coming back is someone asking for it
      # again, which starts this unit afresh.
      Restart = "no";

      # This is the whole of "its own view of projects". The agent's tree is
      # bound over /projects for this unit and nothing else, so every agent
      # works in /projects/<slug>, that path means a different tree for each
      # of them, and no agent can name -- or collide with -- another's.
      #
      # It is also why the id is not in the path any more. The numbered
      # directory still exists underneath, because a human debugging two
      # agents needs to tell the trees apart; what changed is that it stopped
      # being the agent's problem. Same shape `main` gets from bwrap, from
      # the other side: there a sandbox is built around the agent, here a
      # namespace is handed to its unit.
      PrivateMounts = true;
      BindPaths = "${agentProjects}:/projects";
    } // lib.optionalAttrs (!root) {
      User = "agent%i";
      Group = "cave";
    };
  };
in
{
  imports = [
    # virtio driver set for the initrd. Written for QEMU guests but it is the
    # same virtio-pci device family Cloud Hypervisor exposes.
    "${modulesPath}/profiles/qemu-guest.nix"
  ];

  system.stateVersion = "24.11";

  # --- VM shape (direct kernel boot) ---
  # Headless: on x86_64 the boot log goes to the 8250 serial port, which CHV
  # exposes with `--serial file=<log>`. Deliberately NOT the virtio console
  # (hvc0): virtio_console is a module, so hvc0 is silent until the initrd
  # loads it and early boot failures would leave no trace. Memory and cpu
  # count are host-side VMM flags, not guest config.
  #
  # Apple's Virtualization.framework exposes no legacy UART, so on aarch64
  # the virtio console is the only one there is and hvc0 is forced. The early
  # boot blind spot above is unavoidable there; vfkit's --device
  # virtio-serial,logFilePath is where that log lands.
  boot.kernelParams =
    if pkgs.stdenv.hostPlatform.isAarch64
    then [ "console=hvc0" ]
    else [ "console=ttyS0" ];

  # How the guest behaves when the host-side memory flag turns out to be too
  # small, which is the guest's half of the sizing question above.
  #
  # Without swap of any kind that ceiling is a wall rather than a slope: the
  # only page the kernel can reclaim is file-backed, so it evicts the page
  # cache and reads it straight back. The observed shape is one claude session
  # peaking at 3.3G in a 4G guest and reading 376G off the disk in 37 minutes
  # -- an agent that looks hung rather than one that looks slow, which is a
  # much harder thing to diagnose from outside.
  #
  # zram rather than a swap file, because the disk is the thing already under
  # pressure: this swaps to compressed RAM, so a reclaimed anonymous page
  # costs CPU (which is idle while a guest thrashes) instead of the disk I/O
  # that is the problem. It does not raise the ceiling and is not meant to --
  # `[vm] memory_mb` does that. It makes overshooting it degrade.
  zramSwap = {
    enable = true;
    # Of guest RAM, and it is a compressed-page budget rather than an
    # allocation: what it actually consumes is this divided by the compression
    # ratio, and only for pages that have been reclaimed.
    memoryPercent = 50;
  };

  # Root is the whole device, no partition table (make-disk-image
  # partitionTableType = "none"), so the kernel gets root=/dev/vda directly.
  fileSystems."/" = {
    device = "/dev/vda";
    fsType = "ext4";
    # Grow the filesystem to fill the disk on every boot.
    #
    # Two sizes decide this and they are set in different places, which is how
    # they drifted. make-disk-image below builds the image at closure size
    # plus additionalSpace -- about 6G -- while the host allocates the disk at
    # config.toml's disk_size_gb, 100G by default. Nothing grew the ext4 to
    # match, so the guest ran in a 6G filesystem inside a 100G disk and simply
    # filled up: `df` said 84% while the host saw a nearly empty sparse image.
    #
    # The failure is worth naming because of how it presents. Nix reports it
    # as "No space left on device" from inside a build sandbox, halfway
    # through a store path, which reads like a broken derivation rather than a
    # full disk; and the disk is not full, the filesystem is. systemd-growfs
    # runs before local-fs.target, so anything that needs the space has it.
    options = [ "x-systemd.growfs" ];
  };

  # Boot state is host-injected (--kernel/--initramfs/--cmdline) and the disk
  # carries no bootloader, so grub-install would abort an in-guest
  # `nixos-rebuild switch` before it activated anything. Disabling GRUB lets
  # switch register and activate generations, which is the root agent's
  # redeploy path (docs/vm-root-agent.md). The consequence to remember: a
  # generation that changes the kernel needs the host to boot the new one,
  # which is why bootloader boot is the graduation step in that doc.
  boot.loader.grub.enable = false;

  # The raw root image CHV boots. Built once on the host; vmcav copies it into
  # the state dir on first start, after which the guest owns it and
  # nixos-rebuild mutates it in place.
  system.build.chvDisk = import "${modulesPath}/../lib/make-disk-image.nix" {
    inherit pkgs lib config;
    partitionTableType = "none";
    installBootLoader = false;
    format = "raw";
    diskSize = "auto";
    additionalSpace = "3G";
    copyChannel = false;
  };

  # --- Networking: none. Every host service arrives over vsock ---
  # The guest has no NIC at all, so unproxied egress is impossible by
  # construction rather than by firewall rule (vm-cave-design.md). Host
  # services reach the guest over virtio-vsock and are re-exposed on
  # localhost by the forwarders below, so nothing in this config, in the
  # agent's git remotes, or in its proxy env has to know vsock exists.
  # UTC, said rather than inherited.
  #
  # `main` pins a cave's clock to UTC so the host's geographic region stays
  # outside the sandbox, with `coding-cave.timeZone` as the only lever. The
  # guest owns its own /etc/localtime, so the mechanism differs and the
  # contract must not.
  #
  # It was already UTC, but only because that is what NixOS defaults to --
  # the right answer for a reason that has nothing to do with the contract,
  # and one that would stop holding the first time somebody set this to make a
  # journal easier to read. Written down, changing it is a decision somebody
  # made; left out, it was a decision nobody knew they were making.
  #
  # The lever is this line, which is a system change and goes through the
  # approval gate like any other. That is the right weight for widening what
  # the guest knows about where it is.
  time.timeZone = "UTC";

  # No NTP client, because there is nothing for one to talk to. NixOS enables
  # timesyncd by default, so this guest has been running one since the first
  # boot: a daemon whose whole purpose is reaching the network, in the one
  # machine here defined by having no NIC.
  #
  # It never worked, and could not have. `NTPSynchronized=no` after months of
  # uptime is the daemon reporting that itself. What it did instead was wake
  # on a timer, fail to reach a server it has no route to, and take four
  # watchdog kills across one boot -- SIGABRT, a core dump each, in the same
  # clusters as the services below, for a service that had nothing to do.
  #
  # The clock is right anyway, and not by luck: kvm-clock takes it from the
  # host, which is why the guest reads within seconds of real time even after
  # the host has been suspended for a day. The host corrects the rest on every
  # command that reaches in here (`correct_guest_clock` in scripts/vmcav),
  # which is the design -- a guest with no trustworthy clock source should not
  # be guessing, and that comment already says "no timesyncd" as though this
  # line existed. It did not. Now the running system matches the claim the
  # host's reasoning rests on.
  services.timesyncd.enable = false;

  networking.hostName = "coding-cave";
  networking.useDHCP = false;
  networking.firewall.enable = false;

  environment.sessionVariables =
    let proxy = "http://127.0.0.1:10255";
    in {
      HTTP_PROXY = proxy;
      HTTPS_PROXY = proxy;
      http_proxy = proxy;
      https_proxy = proxy;
      NO_PROXY = "localhost,127.0.0.1,::1";
      no_proxy = "localhost,127.0.0.1,::1";
      # NixOS bakes the system CA path into curl/go/nix, but node and
      # claude's runtime do not read it on their own; point them at the
      # system bundle, which includes the crinj CA (security.pki below).
      SSL_CERT_FILE = "/etc/ssl/certs/ca-certificates.crt";
      NODE_EXTRA_CA_CERTS = "/etc/ssl/certs/ca-certificates.crt";
    };

  # --- TLS trust: crinj's intercepting CA, baked into the system store ---
  # crinj MITMs TLS, so the whole guest must trust its CA. Baking it into
  # security.pki lands it in /etc/ssl/certs/ca-certificates.crt, the one
  # path OpenSSL-based tools, go, and nix already consult, so trust is
  # system-wide instead of per-service env plumbing. The CA is
  # host-specific and must not be committed, so it reaches the eval
  # impurely (every caller already builds with --impure):
  #   - host build (vmcav): $CODING_CAVE_CRINJ_CA carries the PEM
  #   - in-guest redeploy: read from the mounted seed share
  # No silent fallback: a build without the CA yields a guest whose agent
  # cannot reach the API, so fail the eval loudly instead.
  security.pki.certificates =
    let
      env = builtins.getEnv "CODING_CAVE_CRINJ_CA";
      share = "/etc/cave-host/ca.pem";
    in [
      (if env != "" then env
       else if builtins.pathExists share then builtins.readFile share
       else throw ("crinj CA not found: export CODING_CAVE_CRINJ_CA and "
         + "build with --impure (host), or build --impure in the guest "
         + "where ${share} is mounted"))
    ];

  # Host-provided runtime seed. Carries the files that are host-specific and
  # must not be committed in this flake: crinj's intercepting CA (ca.pem, also
  # read at eval time by in-guest redeploys, see security.pki above), the
  # host's claude login state (claude.json), this VM's own captured login
  # placeholder (credentials.json), its git identity, and its ssh public key.
  # `nofail` plus a short timeout keep a boot with no seed disk attached from
  # hanging on a device that is never going to appear.
  #
  # Delivered as a small read-only ext4 disk rather than a filesystem share:
  # CHV has no 9p, and its virtio-fs would drag in a virtiofsd the launcher
  # has to supervise (vm-cave-design.md notes the stock one is unusable
  # unprivileged). A handful of small static files do not justify that, and
  # vmcav can build the image rootlessly with `mke2fs -d`.
  fileSystems."/etc/cave-host" = {
    device = "/dev/vdb";
    fsType = "ext4";
    options = [ "ro" "nofail" "x-systemd.device-timeout=2" ];
  };

  # --- Who an agent runs as ---
  # Ordinary agents are not root. The root agent still is, because its job is
  # to rebuild the system every other agent runs in, and that is the one job
  # that needs it (docs/vm-root-agent.md). Everyone else gets a user of their
  # own out of the pool declared here.
  #
  # What this is and is not. Separate uids mean an agent's files are its own
  # and a stray `rm -rf` reaches one tree rather than all of them, and it means
  # the thing an agent runs -- claude with permissions skipped -- is no longer
  # running as root over the whole guest. It is not a wall between agents:
  # homes are 0700 but the root agent can read any of them, and the boundary
  # that means anything is still the VM's (docs/vm-cave-boundary.md). Inside
  # the guest agents are colleagues, and this is a blast radius, not a border.
  users.groups.cave.gid = 3000;
  users.users = builtins.listToAttrs (map
    (id: {
      name = agentUser id;
      value = {
        isNormalUser = true;
        uid = agentUid id;
        group = "cave";
        # uinput so an agent can drive a headless compositor, which is the
        # capability this guest exists to make safe (hardware.uinput below).
        extraGroups = [ "uinput" ];
        home = "/home/${agentUser id}";
        createHome = true;
        # No password and no key: nothing logs in as an agent. A session is
        # started by systemd and reached through its socket, and a human who
        # needs to be one uses the root shell they already have.
        hashedPassword = "!";
      };
    })
    agentIds);

  # The session socket directory, and the per-agent worktree root beneath it.
  # Declared here rather than left to whoever gets there first: cave-guestd
  # would create the socket directory 0700 root-owned, which is correct only
  # while every session is root's, and an ordinary agent would then fail to
  # bind its own socket with a permission error that names nothing useful.
  systemd.tmpfiles.rules = [
    "d /run/cave 0755 root root -"
    "d ${sessionDir} 1770 root cave -"
    "d /var/lib/coding-cave 0755 root root -"
    "d ${worktreeRoot} 0755 root root -"

    # zsh's TMPPREFIX, which the cave environment sets to /tmp/claude/zsh.
    # zsh puts heredoc temp files under that prefix and does not create the
    # directory, so with it missing every heredoc fails -- including the one
    # `/persist:go` uses to pass its arguments. The error it raises says
    # "can't create temp file for here document: no such file or directory",
    # which names neither zsh's setting nor the missing directory and reads
    # like the tool that ran the heredoc is broken.
    #
    # 1777 like /tmp itself: agents are separate uids and share this, so it
    # has to be writable by all of them and sticky so none can remove
    # another's. Under /tmp, so it is cleared on boot and recreated here.
    "d /tmp/claude 1777 root root -"

    # /bin/bash, because scripts written anywhere else expect it.
    #
    # NixOS ships /bin/sh and /usr/bin/env and nothing more, which is correct
    # for a machine whose software it builds. This guest is not that: it runs
    # scripts from checkouts and from a user's own dotfiles, and `#!/bin/bash`
    # is what most of the world writes. Such a script fails here with "bad
    # interpreter" and exit 127 -- and hides, because running it by hand as
    # `bash script.sh` bypasses the shebang and works, so the caller looks
    # like the fault. The user's statusline failed exactly this way, showing
    # nothing and logging nothing.
    #
    # The alternative was rewriting each script's shebang on the way in, which
    # is what this replaces. That fights the user's own config -- it is theirs,
    # not the cave's -- and has to be redone every time upstream changes. One
    # symlink fixes every script from every repo instead.
    "L+ /bin/bash - - - - ${pkgs.bash}/bin/bash"
  ];

  # --- Access: host comes in over ssh, then attaches to the agent daemon ---
  services.openssh = {
    enable = true;
    settings = {
      PermitRootLogin = "prohibit-password";
      PasswordAuthentication = false;
    };
  };
  services.getty.autologinUser = "root";

  # The inbound leg: terminal attach is the host connecting in, so something
  # must accept AF_VSOCK on 22 and hand it to sshd. systemd-ssh-generator does
  # this by itself under Cloud Hypervisor, but not under vfkit, where it binds
  # AF_UNIX only and leaves nothing answering, so attach silently has no
  # listener. This is the fallback for that case, not the normal path.
  systemd.services.vsock-ssh = {
    description = "Accept host ssh on vsock 22 if nothing else does";
    wantedBy = [ "multi-user.target" ];
    after = [ "sshd.service" ];
    serviceConfig = {
      ExecStart = "${pkgs.socat}/bin/socat "
        + "VSOCK-LISTEN:22,fork,reuseaddr TCP:127.0.0.1:22";
      # Where the generator did bind the port, socat cannot, and exits 1
      # immediately. That is the expected outcome, not a failure: the port is
      # answered either way. Saying so matters beyond tidiness, because a unit
      # left in failed state makes every later switch-to-configuration report
      # the deploy as failed (the same trap as logrotate below), and with
      # Restart it also burns the start limit on a race it cannot win.
      SuccessExitStatus = 1;
      Restart = "on-failure";
      RestartSec = 1;
    };
  };

  # Stop the guest killing its own services every time the host sleeps.
  #
  # A service watchdog restarts something that has stopped responding. Inside a
  # VM it cannot tell that from the hypervisor freezing the guest: the host
  # suspends, the deadline passes in wall time the guest never saw, and on
  # resume systemd finds every watchdog overdue at once and kills them all.
  #
  # Observed rather than reasoned. In one boot of this guest, journald, udevd,
  # logind and oomd were watchdog-killed at fourteen distinct moments, each a
  # cluster of one to four services within a millisecond of each other -- one
  # event apiece, not four hangs -- spaced overnight and across a weekend, in
  # the shape of a laptop lid. journald's start/stop count matches the cluster
  # count exactly.
  #
  # The trade is one-sided. None of these has ever actually hung here, so the
  # protection has caught nothing; while journald being killed drops whatever
  # it had buffered, which is precisely the evidence docs/vm-cave-todo.md is
  # still missing for the `clone-repo` failure that "took its journal with
  # it". A watchdog that cannot fire correctly in this environment is worth
  # less than the logs it costs.
  #
  # Not a system-wide default: these four are the ones observed, and a unit
  # that genuinely can hang should keep its watchdog.
  systemd.services.systemd-journald.serviceConfig.WatchdogSec = lib.mkForce 0;
  systemd.services.systemd-udevd.serviceConfig.WatchdogSec = lib.mkForce 0;
  systemd.services.systemd-logind.serviceConfig.WatchdogSec = lib.mkForce 0;
  systemd.services.systemd-oomd.serviceConfig.WatchdogSec = lib.mkForce 0;

  # logrotate's config check rejected the store-owned config, which fails on
  # boot and, worse, makes switch-to-configuration return non-zero. The
  # agent's redeploy loop reads that as a failed deploy and would roll back a
  # good one. logrotate isn't needed here (journald self-rotates), so disable
  # it. The original diagnosis was ownership seen through a 9p-mounted
  # /nix/store, which cannot be it any more: the store is on the ext4 root
  # now. Left disabled because nothing here wants logrotate either way, so
  # whether the check would still fail is untested.
  services.logrotate.enable = false;

  # The service plane's guest end. CHV's vsock is the unix-socket hybrid: a
  # guest connection to port N surfaces on the host as the unix socket
  # <vsock.sock>_N, which cave-vm answers. See bridgeSocket in the let block
  # above for the shape and for why these are socket units rather than
  # long-lived socats.
  systemd.sockets = {
    vsock-crinj = bridgeSocket 10255 "Bridge 127.0.0.1:10255 to host crinj over vsock 7200";
    vsock-gpg = bridgeSocket 7400 "Bridge 127.0.0.1:7400 to the host gpg signing broker over vsock 7400";
    vsock-nix = bridgeSocket 7100 "Bridge 127.0.0.1:7100 to the host nix binary cache over vsock 7100";
    vsock-git = bridgeSocket 9418 "Bridge 127.0.0.1:9418 to the host git daemon over vsock 7300";

    # The host's command channel, JSON over HTTP. The same argument as the
    # bridges, one layer down: systemd binds AF_VSOCK itself, so the port is
    # listening before the daemon behind it has started and a caller cannot
    # race its startup. Not a bridge, so it is spelled out: the guest is the
    # server here, and cave-guestd accepts on the socket rather than socat
    # handing one connection to something else.
    cave-control = {
      description = "Host control plane (vsock 7001)";
      wantedBy = [ "sockets.target" ];
      socketConfig = {
        # An empty CID means "this guest", whichever one the VMM assigned.
        ListenStream = "vsock::7001";
        # One daemon serving the listener, not one process per connection.
        Accept = false;
      };
    };
  };

  systemd.services."vsock-crinj@" = bridgeService 7200;
  systemd.services."vsock-gpg@" = bridgeService 7400;
  systemd.services."vsock-nix@" = bridgeService 7100;
  systemd.services."vsock-git@" = bridgeService 7300;

  # Signing. The gpg-agent socket is deliberately NOT forwarded: a unix socket
  # cannot cross the VM boundary, and re-exposing the agent on a port would
  # hand signing to every process that can reach it, which is strictly weaker
  # than the 0600 socket a container cave gets. Instead the host runs a broker
  # that accepts a payload, validates the arguments, pins the key, and signs.

  # Public keyring, so `git log --show-signature` and any verification the
  # agent does resolve locally instead of round-tripping to the host. Public
  # material only; nothing here can sign.
  systemd.services.install-pubring = {
    description = "Install the host public keyring for verification";
    wantedBy = [ "multi-user.target" ];
    before = [ "vm-root-agent.service" ];
    unitConfig.ConditionPathExists = "/etc/cave-host/pubring.kbx";
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.writeShellScript "install-pubring" ''
        set -eu
        install -d -m 700 /root/.gnupg
        install -m 600 /etc/cave-host/pubring.kbx /root/.gnupg/pubring.kbx
      ''}";
    };
  };

  # What the agent knows that a checkout does not tell it: that it is root in a
  # VM, which channels exist, that nothing it commits is visible until it
  # pushes, and that the approval gate is the point rather than an obstacle.
  # `vm-root-agent.md` calls for exactly this and nothing implemented it, so
  # every agent so far started with the repo's CLAUDE.md, which is written for
  # someone working on the host.
  #
  # A user-level CLAUDE.md rather than a system prompt: it composes with the
  # repo's own instead of replacing them, and the agent can read the file it is
  # working from.
  systemd.services.install-agent-instructions = {
    description = "Install the root agent's standing instructions";
    wantedBy = [ "multi-user.target" ];
    before = [ "vm-root-agent.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.writeShellScript "install-agent-instructions" ''
        set -eu
        install -d -m 700 /root/.claude
        install -m 644 ${./CLAUDE.md} /root/.claude/CLAUDE.md
      ''}";
    };
  };

  # Install the host's ssh public key for root login. The key is host-specific
  # (generated by the host script), delivered on the seed disk. Copied into
  # /root/.ssh as root:root 600 rather than pointing sshd at the seed file,
  # whose mount ownership and perms would trip sshd StrictModes.
  systemd.services.install-ssh-key = {
    description = "Install host ssh key for root login";
    wantedBy = [ "multi-user.target" ];
    before = [ "sshd.service" ];
    unitConfig.ConditionPathExists = "/etc/cave-host/authorized_keys";
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.writeShellScript "install-ssh-key" ''
        set -eu
        install -d -m 700 /root/.ssh
        install -m 600 /etc/cave-host/authorized_keys /root/.ssh/authorized_keys
      ''}";
    };
  };

  # --- Claude policy settings ---
  # The bypass-permissions warning is a different dialog from the trust one
  # seeded below, and accepting it never touches .claude.json: claude records
  # it as `skipDangerousModePermissionPrompt` in settings, which is why it came
  # back at every reformat while the trust dialog stayed accepted.
  #
  # Stated here as policy rather than written into /root/.claude/settings.json,
  # because that is what it is: the agent runs with
  # --dangerously-skip-permissions by construction, and a VM with no NIC is the
  # sandbox the dialog is asking about. Policy is also the tier the agent
  # cannot clobber by rewriting its own settings, and being config rather than
  # disk state it survives a reformat for free. The container cave writes the
  # same file for its own reason (the cave-messages channel allowlist).
  environment.etc."claude-code/managed-settings.json".text = builtins.toJSON {
    skipDangerousModePermissionPrompt = true;
  };

  # System-wide git config. autoSetupRemote so the agent's first `git push` on
  # a branch it just forked creates and tracks the remote branch instead of
  # failing with "no upstream"; the agent branch is created locally by
  # clone-repo below and may not exist on the host yet.
  environment.etc."gitconfig".text = ''
    [push]
    	default = current
    	autoSetupRemote = true
    [gpg]
    	program = ${caveGpg}/bin/cave-gpg
  '';

  # --- Git identity ---
  # The agent's core loop ends in "commit and push", and git refuses to commit
  # without an identity. vmcav stages the host's user.name/user.email into the
  # seed share (host-specific, so it cannot be committed here). The fallback
  # keeps a boot with no seed able to commit rather than failing at the last
  # step of the loop; it is deliberately obviously-not-a-person.
  systemd.services.install-gitconfig = {
    description = "Install git identity for root";
    wantedBy = [ "multi-user.target" ];
    before = [ "vm-root-agent.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.writeShellScript "install-gitconfig" ''
        set -eu
        seed=/etc/cave-host/gitconfig
        if [ -f "$seed" ]; then
          install -m 644 "$seed" /root/.gitconfig
        elif [ ! -f /root/.gitconfig ]; then
          printf '[user]\n\tname = vm-root-agent\n\temail = vm-root-agent@coding-cave.invalid\n' \
            > /root/.gitconfig
        fi
      ''}";
    };
  };

  # --- Nix: flakes + daemon, so the agent redeploys from inside ---
  nix.settings.experimental-features = [ "nix-command" "flakes" ];
  nix.settings.trusted-users = [ "root" ];
  nixpkgs.config.allowUnfree = true;

  # The host cache's address and signing key arrive on the seed disk, because
  # the key is generated on whichever machine this VM is running on and so
  # cannot be committed here or baked into the image. `!include` rather than
  # `include`: a boot with no seed attached must still have a working nix, and
  # the leading bang is what makes a missing file not an error.
  #
  # require-sigs stays at its default of true. That is what the fragment's
  # trusted-public-keys line is for, and it is why the host signs at all: the
  # vsock link cannot be reached from outside this machine, but "unreachable"
  # is not the same claim as "verified", and only the second one survives
  # someone later deciding to reach the cache another way.
  nix.extraOptions = ''
    !include /etc/cave-host/nix.conf
  '';

  # The daemon does the substituting, so the daemon is what needs the proxy.
  #
  # This was invisible while every agent was root. A trusted user's client
  # settings are accepted by the daemon, so root's own proxy env carried the
  # fetch; an ordinary agent is not trusted, its env is ignored, and the
  # daemon reached for cache.nixos.org with no NIC and no resolver behind it.
  # The failure is "Could not resolve host", which reads like a broken network
  # rather than a missing variable, and it lands on `nix develop` -- the first
  # thing an agent does in a project it has just checked out.
  #
  # Not a widening: this is the same crinj proxy every other byte leaves
  # through, named for a process that could not otherwise ask. The alternative
  # -- adding agents to trusted-users -- would let an agent name its own
  # substituter, which is a boundary decision and not one to make in passing.
  systemd.services.nix-daemon.environment = {
    http_proxy = "http://127.0.0.1:10255";
    https_proxy = "http://127.0.0.1:10255";
    no_proxy = "localhost,127.0.0.1,::1";
  };

  # --- Synthetic input, for agents working on window managers ---
  # /dev/uinput, which is what lets an agent start a headless compositor and
  # type into it. The module, the `uinput` group and a udev rule at 0660 with
  # static_node=uinput, so the node exists whether or not anything has loaded
  # the module yet.
  #
  # This belongs to the guest and could not belong to a container cave. uinput
  # is kernel-global: a cave handed that device node creates a virtual keyboard
  # in the *host's* kernel, so an agent testing keystroke injection would be
  # typing into the human's real session with no sandbox in the path. The guest
  # runs its own kernel, so the same test stops at the VM boundary. It is the
  # one capability here that `cav -d` must never be used to imitate.
  #
  # No compositor, no mesa and no injection tools in this image. Those are one
  # project's dependency set and belong in that project's own flake, which the
  # guest can build because it substitutes from the host store over 7100.
  hardware.uinput.enable = true;

  # Put the cave environment on a login shell's PATH.
  #
  # An agent's session gets it by sourcing the built env.json, so the tools a
  # cave.nix installs -- persist, the LSP servers, fd, fzf -- are there for the
  # agent and nowhere else. A `vmcav shell` login is a plain ssh session that
  # sources none of that, so the same guest answered `persist` for the agent
  # and `command not found` for the human looking over its shoulder.
  #
  # $HOME rather than a fixed path, so this is one rule for root and for every
  # agent user; the directory is a symlink into the store that seed-agent-home@
  # writes, so it is absent until an agent has been seeded and the test skips.
  environment.extraInit = ''
    if [ -d "$HOME/.cave-profile/bin" ]; then
      PATH="$HOME/.cave-profile/bin:$PATH"
    fi
  '';

  # --- Guest toolchain: the minimal floor, nothing more ---
  # This list is also the agent's PATH: its unit resolves tools through
  # /run/current-system/sw/bin rather than through store paths frozen when the
  # daemon started, so anything the agent needs has to be here to be reachable
  # after a deploy. bash and nix are named explicitly for that reason, rather
  # than relied on as somebody else's default.
  environment.systemPackages = with pkgs; [
    # Not pkgs.git: the guest's git is the one that pushes what it commits.
    caveGit
    caveGuestd
    caveAgent
    caveEnter
    caveEnvCli
    caveHost
    caveRebuild
    claudeCode
    bash
    nix
    vim

    # The toolbox an agent assumes exists. `main` carries this same list in
    # its cave-system home-manager module, because cav runs on whatever distro
    # the host is and cannot configure a system it does not own. The guest
    # owns its system, so the floor belongs here: every agent gets it whether
    # or not a cave environment has ever been built, and a cave.nix that adds
    # nothing still lands somewhere usable.
    #
    # It is a floor and not a preference. Anything an agent should merely be
    # able to have goes in cave.nix, where a person can change it without a
    # deploy.
    coreutils gnugrep gnused gawk findutils diffutils
    gnutar gzip xz bzip2 unzip
    curl ripgrep jq less which procps patch file tree
    gnupg
  ];

  # What the deployed system says a cave environment must be built against.
  # Read by `cave-guestd env`, and written here so that it moves with the
  # generation: an environment built against the previous system's nixpkgs
  # rebuilds itself at the next session start, because these paths are half
  # of the build stamp.
  environment.etc."cave/env.json".text = builtins.toJSON {
    caveEnv = "${caveEnvSrc}";
    inputs = {
      nixpkgs = caveEnvInputs.nixpkgs;
      "home-manager" = caveEnvInputs.home-manager;
      "claude-code" = caveEnvInputs.claude-code;
    };
  };

  # The same three paths, as an etc entry whose source is a store path, which
  # is what makes the system closure depend on them. env.json above is text
  # and creates no such dependency: without this, a garbage collection would
  # take the sources out from under the next env build. See caveEnvPins.
  environment.etc."cave/env-pins".source = caveEnvPins;

  # A tombstone, and the reason it has to exist for one generation.
  #
  # seed-claude-config is gone: its work is per-agent now and seed-agent-home@
  # does it. But a session that is *already running* was started with
  # `Requires=seed-claude-config.service`, and that unit is RemainAfterExit,
  # so it is still active. Delete it from the config and switch-to-configuration
  # stops it, `Requires=` propagates the stop, and the running agent -- which
  # may be the very agent performing the deploy -- is killed mid-sentence. That
  # is not a theory: it happened three times, each one taking the terminal with
  # it and leaving the switch dead at status 101 with the new generation built
  # but never activated.
  #
  # So the unit stays, doing nothing, until no live session names it. It can be
  # deleted in any later generation once every session has been restarted onto
  # the new dependencies. restartIfChanged/stopIfChanged false so that this
  # deploy does not touch the running instance either.
  #
  # The general rule this is an instance of is below, on seed-agent-home@.
  systemd.services.seed-claude-config = {
    description = "Superseded by seed-agent-home@ (kept so live sessions do not stop)";
    restartIfChanged = false;
    stopIfChanged = false;
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.coreutils}/bin/true";
    };
  };

  # --- Claude config seeding, per agent ---
  # claude prompts interactively without accepted trust/onboarding flags for
  # its project dir, and without login state. Seed the agent's home before its
  # session starts: preserve any existing state, else start from the host's
  # staged claude.json (login state; crinj injects the real token at the
  # network layer), then force the trust/onboarding flags for its worktree.
  #
  # Per agent rather than once at boot, because a home is now per agent. The
  # old unit wrote /root and nothing else, which was right while every agent
  # was root and becomes the quiet failure the moment one is not: the agent
  # reads a home nobody seeded, finds no credentials, and stops at /login
  # having reported nothing. It also seeds the git identity and the standing
  # instructions, so that everything an agent's home needs is one unit and a
  # new home cannot be half-populated by a unit somebody forgot to instance.
  systemd.services."seed-agent-home@" = {
    description = "Seed agent %i's home: claude config, git identity, environment";
    # The trust key is a path, and the path is the project the agent was
    # given; nothing here waits on the checkout existing.
    #
    # The two bridges are for the environment build at the end. Everything it
    # evaluates is already in the store, but what it *builds* is not: the
    # first one on a fresh guest substitutes a good deal, from the host's
    # store over 7100 and from upstream through the proxy. Wants= rather than
    # Requires=, because a build that cannot substitute is slow, not broken,
    # and this unit may not take an agent down.
    after = [ "vsock-nix.socket" "vsock-crinj.socket" ];
    wants = [ "vsock-nix.socket" "vsock-crinj.socket" ];
    serviceConfig = {
      Type = "oneshot";
      # Load-bearing, and not merely tidiness. A session `Requires=` this
      # unit, and `Requires=` propagates an explicit stop: if this stayed
      # active after running, then removing it -- or anything else that
      # stopped it, including a later rebuild -- would stop every session
      # that named it, killing live agents. Going inactive on success means
      # there is nothing left for a stop to propagate through.
      #
      # The rule, for anything a session depends on: go inactive when you are
      # done, or be depended on with Wants= rather than Requires=. cave-rebuild
      # refuses a deploy that would break it, but the refusal is a backstop,
      # not the design.
      RemainAfterExit = false;
      EnvironmentFile = "/run/cave/sessions/%i.env";
      ExecStart = "${pkgs.writeShellScript "seed-agent-home" ''
        set -eu
        ${agentIdentity}
        : "''${CAVE_PROJECT:?no project}"
        install -d -m 700 -o "$agent_user" "$agent_home"
        install -d -m 700 -o "$agent_user" "$agent_home/.claude"

        # The base is this VM's own previous state, captured before the last
        # reformat, never the host's ~/.claude.json: those flags are keyed to
        # the host's own projects and its own history and MCP config, none of
        # which has any business in here. An empty base is fine, the keys
        # forced below are the ones that matter.
        seed="$agent_home/.claude.json"
        host=/etc/cave-host/claude.json
        if [ -f "$seed" ]; then base=$(cat "$seed")
        elif [ -f "$host" ]; then base=$(cat "$host")
        else base='{}'; fi

        # The host's own business, taken out of whichever copy this came from.
        #
        # The file was being seeded whole, which put the host user's entire
        # project list into every agent's home: their username, their
        # directory layout, and the names of projects that have nothing to do
        # with this VM. The paragraph above already said that was not meant to
        # happen, and the three things it names -- projects, history, MCP
        # config -- are exactly the three taken out here. This is the code
        # catching up with its own comment.
        #
        # Applied to an existing home too, not only a fresh seed, because the
        # homes that already exist were seeded before this and are the ones
        # holding the paths. Projects are filtered rather than dropped so an
        # agent keeps its own: a key under /projects is this guest's, and
        # anything else belongs to a filesystem that is not in here. An agent
        # whose own entry is filtered out sees one trust dialog and the seed
        # puts the entry back below.
        #
        # What is kept is login and onboarding state, which is why this file
        # is staged at all: without it an agent starts logged out.
        base=$(printf '%s' "$base" | ${pkgs.jq}/bin/jq -c '
          del(.mcpServers, .history)
          | .projects //= {}
          | .projects |= with_entries(select(.key | startswith("/projects/")))')

        # Trust the path the agent will actually be standing in, which is the
        # bind target and not the tree behind it. Keying this to the real
        # worktree would leave claude prompting about a directory the agent
        # cannot see the name of, on every start.
        printf '%s' "$base" > "$seed.tmp"
        ${pkgs.jq}/bin/jq --arg p "/projects/$CAVE_PROJECT" \
          '.projects[$p].hasTrustDialogAccepted = true
           | .projects[$p].hasCompletedProjectOnboarding = true' \
          "$seed.tmp" > "$seed.tmp2"
        mv "$seed.tmp2" "$seed"
        rm -f "$seed.tmp"
        chown "$agent_user" "$seed"
        chmod 600 "$seed"

        # The login token, without which the agent starts logged out and stops
        # at /login having reported nothing.
        #
        # The rule used to be "install the host's copy if the agent has none,
        # and never overwrite, so an in-guest login wins". The second half is
        # right and the first half was not enough. What the host stages is a
        # placeholder captured the one time this VM logged in, and a token
        # expires: the seed on disk has been expired for weeks. So every agent
        # created since came up holding a dead token, which is not "no
        # credentials" -- it is credentials claude checks locally and rejects
        # before it ever reaches crinj, so the agent could not work at all.
        #
        # Expiry is the test, not presence. A live token is left alone, which
        # keeps an in-guest login winning; a missing or expired one is replaced
        # by the freshest that exists anywhere in this guest. They are all the
        # same account -- one VM, one login -- so an agent that refreshed its
        # token has one every other agent can use, and root's is usually the
        # freshest because root is the agent that has been running.
        cred="$agent_home/.claude/.credentials.json"
        expires_at() {
          [ -f "$1" ] || { echo 0; return; }
          ${pkgs.jq}/bin/jq -r '.claudeAiOauth.expiresAt // 0' "$1" 2>/dev/null || echo 0
        }
        now=$(${pkgs.coreutils}/bin/date +%s%3N)
        if [ "$(expires_at "$cred")" -le "$now" ]; then
          best=""; best_exp=0
          for c in /root/.claude/.credentials.json \
                   /home/*/.claude/.credentials.json \
                   /etc/cave-host/credentials.json; do
            [ -f "$c" ] || continue
            exp=$(expires_at "$c")
            [ "$exp" -gt "$best_exp" ] && { best="$c"; best_exp="$exp"; }
          done
          if [ -n "$best" ] && [ "$best_exp" -gt "$now" ]; then
            echo "seed-agent-home: taking the login from $best"
            install -m 600 -o "$agent_user" "$best" "$cred"
          elif [ ! -f "$cred" ] && [ -f /etc/cave-host/credentials.json ]; then
            # Nothing live anywhere. Seed the host's expired copy regardless,
            # so the agent has the shape of a login to refresh rather than
            # none at all, and say so: the agent will stop at /login and the
            # reason is here rather than in its terminal.
            echo "seed-agent-home: no live login in this guest; seeding an expired one" >&2
            install -m 600 -o "$agent_user" /etc/cave-host/credentials.json "$cred"
          fi
        fi

        # What the agent knows that a checkout does not tell it: which VM it
        # is in, which channels exist, and that committing is what publishes.
        install -m 644 -o "$agent_user" ${./CLAUDE.md} "$agent_home/.claude/CLAUDE.md"

        # git refuses to commit without an identity, and the agent's core loop
        # ends in a commit. The host stages its own user.name/user.email; the
        # fallback keeps a boot with no seed able to commit rather than
        # failing at the last step, and is deliberately not a person.
        gitseed=/etc/cave-host/gitconfig
        if [ -f "$gitseed" ]; then
          install -m 644 -o "$agent_user" "$gitseed" "$agent_home/.gitconfig"
        elif [ ! -f "$agent_home/.gitconfig" ]; then
          printf '[user]\n\tname = cave-agent-%s\n\temail = cave-agent-%s@coding-cave.invalid\n' \
            "$CAVE_AGENT_ID" "$CAVE_AGENT_ID" > "$agent_home/.gitconfig"
          chown "$agent_user" "$agent_home/.gitconfig"
        fi

        # Public keyring, so verification resolves locally instead of
        # round-tripping to the host. Public material only; nothing signs.
        if [ -f /etc/cave-host/pubring.kbx ]; then
          install -d -m 700 -o "$agent_user" "$agent_home/.gnupg"
          install -m 600 -o "$agent_user" /etc/cave-host/pubring.kbx \
            "$agent_home/.gnupg/pubring.kbx"
        fi

        # The cave environment, built if the spec or the system moved since the
        # last one, and copied into this home (docs/vm-cave-design.md phase 9).
        # This is what puts settings.json, the statusline and any skills,
        # commands or packages a cave.nix declares in front of the agent.
        #
        # Last, so that a cave.nix which declares one of the paths written
        # above wins. That is deliberate and it cuts both ways: a spec that
        # declares .claude/CLAUDE.md replaces the guest's standing
        # instructions, which is a thing somebody might mean and should not
        # discover by accident.
        #
        # It does not fail the unit. A session Requires= this, so a spec with
        # a typo in it would otherwise stop every agent from starting --
        # including the one whose job is to fix the spec.
        ${caveGuestd}/bin/cave-guestd env seed \
          -home "$agent_home" -user "$agent_user" || \
          echo "seed-agent-home: no cave environment for agent $CAVE_AGENT_ID" >&2
      ''}";
    };
  };

  # --- Repo clone ---
  # Clone the coding-cave repo the agent works in. Its own oneshot (not
  # buried in the agent launcher) so success/failure is visible in the
  # journal and the agent hard-depends on it: no repo, no agent (fail
  # loudly). CAVE_REPO_URL defaults to the host git daemon, reached on
  # localhost where the vsock-git bridge re-exposes it.
  # Check a project out for one agent. Not a boot unit: which project an agent
  # works on, and from which branch, is known when the agent is created and not
  # before, so a VM that boots with a checkout has guessed. It used to guess
  # twice over, hardcoding the coding-cave repo and taking the base branch from
  # whatever HEAD the shared bare repo froze at when it was first created.
  #
  # A template, one instance per agent, taking its project and base from the
  # environment file the session's own unit reads: the two are started as a
  # pair, so they cannot disagree about which worktree the agent lands in.
  systemd.services."clone-repo@" = {
    description = "Check out a project for agent %i";
    # The git bridge, not network-online.target: there is no NIC, so
    # networkd-wait-online would only ever time out.
    # The socket, not a service: systemd has bound 9418 by the time this
    # runs, which is the whole point of the change. Waiting on a socat
    # service only ever meant waiting for a process to exist.
    after = [ "vsock-git.socket" ];
    requires = [ "vsock-git.socket" ];
    path = [ pkgs.git ];
    serviceConfig = {
      Type = "oneshot";
      # Deliberately NOT RemainAfterExit, which this had while it ran once per
      # boot. A oneshot that stays active is a dependency systemd considers
      # permanently satisfied, so starting the session again would skip the
      # checkout entirely: delete the worktree and the agent comes back with
      # its cwd pointing at nothing. Going inactive on success means every
      # session start re-verifies the checkout, which is what "the agent is
      # given a project" has to mean if it is to survive the project being
      # removed.
      EnvironmentFile = "/run/cave/sessions/%i.env";
      ExecStart = "${pkgs.writeShellScript "clone-repo" ''
        set -eu
        ${agentIdentity}
        # Every one of these is supplied when the agent is created. No
        # defaults: a default here is a guess about what someone is working
        # on, which is the bug this unit used to be.
        : "''${CAVE_PROJECT:?no project}" "''${CAVE_BASE:?no base branch}"

        # This unit runs outside the session's mount namespace, on purpose:
        # it is what creates the directory the bind mounts, so it has to name
        # the real path. It is the last place that does. Everything the agent
        # itself runs sees /projects/<slug>.
        repo="$agent_tree/$CAVE_PROJECT"

        # Two older layouts to carry forward, both of which put a checkout
        # where nothing looks for one any more. Nothing is deleted either way:
        # a tree here may hold the only copy of work that was never pushed.
        #
        #   /projects/<slug>          one checkout shared by everyone, from
        #                             before agents had separate trees
        #   /projects/<slug>/<id>     a tree per agent, but at a path the
        #                             agent had to know the number of
        #
        # The second is the layout this change replaces, and the id in it is
        # exactly the information needed to file it correctly, so the move is
        # mechanical. The first has no id in the path, so its branch is asked
        # instead: agent-N/<base> says whose tree it is even when the
        # directory does not.
        #
        # This unit moves one tree: the one belonging to the agent it was
        # started for. Sweeping the whole directory would be quicker and is
        # wrong, because another agent may be *running* in one of those trees
        # right now, and moving a live worktree leaves that agent holding a
        # working directory whose path no longer exists. Every agent migrates
        # its own on its next start, which is the moment nobody is in it.
        legacy="/projects/$CAVE_PROJECT"
        migrate_to() { # <tree> <owner id>
          local tree="$1" owner="$2" dest
          dest="${worktreeRoot}/$owner/projects"
          install -d -m 755 "${worktreeRoot}/$owner"
          install -d -m 755 -o "$agent_user" "$dest"
          if [ -e "$dest/$CAVE_PROJECT" ]; then
            # Both a new checkout and an old one. Nothing is deleted to
            # resolve that: the old tree may hold the only copy of work that
            # was never pushed, and which of the two is wanted is not a
            # question a unit should answer at boot.
            echo "clone-repo: $dest/$CAVE_PROJECT already exists; leaving $tree alone" >&2
            return 0
          fi
          echo "clone-repo: moving $tree to $dest/$CAVE_PROJECT"
          mv "$tree" "$dest/$CAVE_PROJECT"
        }
        owner_of() { # <tree> -- whose branch is checked out in it
          local on
          on=$(${pkgs.git}/bin/git -C "$1" symbolic-ref --quiet --short HEAD || echo "")
          case "$on" in
            agent-*/*) on=''${on#agent-}; echo "''${on%%/*}" ;;
            *) echo "" ;;
          esac
        }
        if [ -d "$legacy/.git" ]; then
          # The shared pre-split checkout. Claim it only if its branch says it
          # is this agent's, or if it says nothing at all -- an unclaimed tree
          # is better carried forward by whoever turns up than left for a
          # `reformat` to delete.
          owner=$(owner_of "$legacy")
          if [ -z "$owner" ] || [ "$owner" = "$CAVE_AGENT_ID" ]; then
            migrate_to "$legacy" "$CAVE_AGENT_ID"
          else
            echo "clone-repo: $legacy is on agent $owner's branch; leaving it for them" >&2
          fi
        elif [ -d "$legacy/$CAVE_AGENT_ID/.git" ]; then
          migrate_to "$legacy/$CAVE_AGENT_ID" "$CAVE_AGENT_ID"
        fi
        # Only ever if empty, and never recursively: what is left in here
        # belongs to agents that have not started since the change, and a
        # non-empty /projects is not this unit's to remove.
        rmdir "$legacy" 2>/dev/null || true

        # The agent's own history, which the tree move above leaves behind.
        #
        # claude finds a resumable session by scanning
        # ~/.claude/projects/<cwd, slashes turned into dashes>/, and every
        # record inside names the cwd it ran in. Move the tree without them and
        # the picker still lists the sessions but refuses every one of them:
        # "working directory no longer exists or is not accessible". The work is
        # not lost, but it is unreachable by the one route an agent has to it,
        # which for a session that was never committed is the same thing.
        #
        # Source is always root's home. Before agents had users of their own
        # they all ran as root and wrote there, so that is where an old record
        # is whichever agent it belongs to; when this agent is the root one the
        # two are the same home and the move is a rename. Run unconditionally
        # rather than only when the tree moved, because the tree may have moved
        # on an earlier boot -- and idempotence is free here: the glob matches
        # nothing once it has run.
        enc() { echo "$1" | ${pkgs.gnused}/bin/sed 's|/|-|g'; }
        migrate_transcripts() { # <old cwd> <new cwd>
          local old="$1" new="$2" oldenc newenc store src dst name rel f base
          oldenc=$(enc "$old"); newenc=$(enc "$new")
          store="$agent_home/.claude/projects"
          install -d -m 700 -o "$agent_user" "$agent_home/.claude" "$store"
          for src in /root/.claude/projects/"$oldenc"*; do
            [ -d "$src" ] || continue
            name=''${src##*/}
            rel=''${name#"$oldenc"}
            # The encoded name is the cwd, so what is left past the old prefix
            # is wherever the session stood: nothing for the tree itself, or a
            # dash and a subdirectory -- a worktree under .claude/worktrees is
            # the common one. Anything else shares a prefix by coincidence
            # (a project genuinely named <slug>-0) and is not ours to move.
            case "$rel" in ""|-*) ;; *) continue ;; esac
            dst="$store/$newenc$rel"
            install -d -m 700 -o "$agent_user" "$dst"
            for f in "$src"/*.jsonl; do
              [ -e "$f" ] || continue
              base=''${f##*/}
              # Never clobber. Ids are uuids, so a name already at the
              # destination is the same session and not a second one -- and it
              # may be the live one, being appended to as this runs.
              if [ -e "$dst/$base" ]; then continue; fi
              # Every string value beginning with the old path names the same
              # tree at its old location: the cwd claude resumes in, and the
              # directories a rewind restores a checkpoint to. Rewriting the
              # prefix and nothing else leaves the rest of the record byte for
              # byte as claude wrote it. The guarded next character is what
              # keeps /0 from also matching /01.
              ${pkgs.gnused}/bin/sed "s|\"$old\\([\"/]\\)|\"$new\\1|g" "$f" > "$dst/$base"
              chown "$agent_user" "$dst/$base"
              chmod 600 "$dst/$base"
              rm -f "$f"
              echo "clone-repo: carried session ''${base%.jsonl} forward to $new"
            done
            rmdir "$src" 2>/dev/null || true
          done
        }
        migrate_transcripts "/projects/$CAVE_PROJECT/$CAVE_AGENT_ID" "/projects/$CAVE_PROJECT"

        # git clone creates the leaf and not the parents. Owned by the agent,
        # because everything under it is about to be.
        install -d -m 755 "${worktreeRoot}/$CAVE_AGENT_ID"
        install -d -m 755 -o "$agent_user" "$agent_tree"

        # A tree that arrived from an older layout, or from a guest where
        # every agent was root, is owned by root and this agent cannot write
        # it. Hand it over once, here, where the agent's identity is known.
        if [ -e "$repo" ] && [ "$(stat -c %U "$repo")" != "$agent_user" ]; then
          echo "clone-repo: giving $repo to $agent_user"
          chown -R "$agent_user" "$repo"
        fi

        # Run git as the agent, not as root. Ownership is the reason: a tree
        # cloned by root and handed over by chown trips git's own
        # dubious-ownership check the next time this unit runs, and the repair
        # for that is either a safe.directory exception per agent or not
        # creating the problem. HOME explicitly, because runuser does not set
        # one and git without a home is git without an identity.
        git() {
          ${pkgs.util-linux}/bin/runuser -u "$agent_user" -- \
            ${pkgs.coreutils}/bin/env HOME="$agent_home" \
            ${pkgs.git}/bin/git "$@"
        }
        # The project's shared bare repo on the host. Never the human's
        # working checkout: an agent must not be able to write a branch into
        # the tree someone is standing in.
        url="''${CAVE_REPO_URL:-git://127.0.0.1/$CAVE_PROJECT.git}"
        # Land on the agent branch, the only namespace pre-receive accepts
        # (refs/heads/agent-N/<base>). Track the remote branch if it already
        # exists, else fork it from the base the agent was given.
        branch="agent-$CAVE_AGENT_ID/$CAVE_BASE"
        if [ ! -d "$repo/.git" ]; then
          echo "clone-repo: cloning $url"
          # host-cave, not origin, which is the name `main` gives the same
          # remote inside a cave (constants::HOST_SHARED_REMOTE). It says what
          # is on the other end: not the project's upstream, but the host's
          # copy, one approval away from being anybody else's.
          git clone -o host-cave "$url" "$repo"
        fi
        cd "$repo"
        # A checkout made before that name existed still calls it origin.
        # Renamed rather than added, because two remotes for one repo is how a
        # push ends up somewhere nobody is reviewing -- and the git wrapper
        # pushes to host-cave by name, so a checkout that kept the old name
        # would simply stop publishing, silently, which is the failure this
        # whole arrangement exists to prevent.
        if ! git remote get-url host-cave >/dev/null 2>&1 \
           && git remote get-url origin >/dev/null 2>&1; then
          echo "clone-repo: renaming the remote origin -> host-cave"
          git remote rename origin host-cave
        fi
        git fetch -q host-cave
        # The path already names the agent, so another agent's branch being
        # checked out here is not a collision to arbitrate -- it is a sign
        # that something put the wrong tree in the wrong place. Kept as the
        # check it now is: refuse rather than switch the branch under whoever
        # is actually working in it.
        current=$(git symbolic-ref --quiet --short HEAD || echo "")
        case "$current" in
          agent-*/*)
            owner=''${current#agent-}
            owner=''${owner%%/*}
            if [ "$owner" != "$CAVE_AGENT_ID" ]; then
              echo "clone-repo: $repo is on $current, which belongs to agent $owner." >&2
              echo "  This is agent $CAVE_AGENT_ID's directory, so that should not" >&2
              echo "  happen; leaving it alone rather than switching branches." >&2
              exit 1
            fi
            ;;
        esac
        if [ -n "$(git status --porcelain)" ]; then
          # Somebody is working in here. Switching branches under them would
          # be the one thing a reformat is for.
          echo "clone-repo: $repo has uncommitted changes; leaving it alone"
          exit 0
        fi
        if git rev-parse --verify --quiet "refs/remotes/host-cave/$branch" >/dev/null; then
          git checkout -q -B "$branch" --track "host-cave/$branch"
        else
          git checkout -q -B "$branch" "host-cave/$CAVE_BASE"
        fi
        # The trust dialog is accepted in seed-agent-home@, which owns
        # everything under the agent's home. It has to be keyed to
        # /projects/<slug> -- what the agent stands in -- and not to $repo,
        # which is the path this unit uses and the agent never sees.
        echo "clone-repo: $repo on $branch (base $CAVE_BASE)"
      ''}";
    };
  };

  # --- The agent runtime ---
  # claude with bypass permissions, its pty held by a cave-guestd of its own so
  # it survives the ssh connection going away. Deliberately not a multiplexer:
  # nothing here wants windows, panes, a status bar or a prefix key, and a
  # multiplexer's scrollback is worse than the terminal's own. Defined here so
  # the agent redeploys its own unit via nixos-rebuild.
  #
  # A template, one instance per agent, because the alternative is one daemon
  # holding every pty, and then its crashes, its OOM kills and its own upgrade
  # all end every session at once. systemd is the only thing parenting an
  # agent: `cave-guestd attach` starts an instance it cannot find rather than
  # forking one, so nothing an agent is doing belongs to the ssh connection
  # that went looking for it.
  #
  # Two templates, one shape (mkSession, in the let block above). An ordinary
  # agent runs as agent<id>; the root agent runs as root because rebuilding
  # the system every other agent runs in is what it is for. Both get their own
  # view of /projects, so the only thing the split decides is the uid.
  systemd.services."cave-session@" = mkSession { root = false; };
  systemd.services."cave-root-session@" = mkSession { root = true; };

  # --- The control plane ---
  # The host's command channel, JSON over HTTP on vsock 7001. Everything
  # routine `cav` asks the guest comes through here, so that ssh is a human's
  # way in and nothing else depends on it. Its socket is declared with the
  # bridges above; this is the daemon behind it.
  systemd.services.cave-control = {
    description = "Host control plane";
    # No wantedBy: the socket starts this, and a guest nobody talks to should
    # not run it. It answers questions about the session units rather than
    # owning them, so it can be restarted under a live agent, which is exactly
    # what a deploy from inside the VM will do to it.
    requires = [ "cave-control.socket" ];
    after = [ "cave-control.socket" ];
    # The token is host-provided and arrives on the seed disk. Without it there
    # is nothing to authenticate against and the daemon refuses to start, which
    # is the right failure: an unauthenticated control plane is worse than none.
    unitConfig.ConditionPathExists = "/etc/cave-host/control-token";
    serviceConfig = {
      ExecStart = "${caveGuestd}/bin/cave-guestd serve -dir ${sessionDir}";
      Restart = "on-failure";
      RestartSec = 1;
    };
  };

  # Nothing starts a session at boot. Which project an agent works on is known
  # when the agent is created, so a VM that came up with one running had
  # guessed: it hardcoded this repo and took the branch from whatever HEAD the
  # shared bare repo happened to freeze at. `vmcav agent <project> [branch]`
  # writes the session's environment file and starts the pair.
}
