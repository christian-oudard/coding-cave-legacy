{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    jail-nix.url = "sourcehut:~alexdavid/jail.nix";
    claude-code = {
      url = "github:sadjow/claude-code-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    claude-loop-src = {
      url = "github:christian-oudard/claude-loop";
      flake = false;
    };
  };

  outputs = { self, nixpkgs, jail-nix, claude-code, claude-loop-src, ... }: let
    pkgs = import nixpkgs { system = "x86_64-linux"; };
    jail = jail-nix.lib.init pkgs;

    claude-loop = pkgs.python3Packages.buildPythonApplication {
      pname = "claude-loop";
      version = "0.1.0";
      src = claude-loop-src;
      pyproject = true;
      build-system = [ pkgs.python3Packages.hatchling ];
    };

    defaultClaudeSettings = ./claude-settings.json;
    version = self.shortRev or self.dirtyShortRev or "dev";

    jailHome = "/home/cave-coder";

    baseCombinators = with jail.combinators; [
      no-die-with-parent
      (set-hostname "coding-cave")
      # pasta wraps bwrap in a user namespace where the host user appears as root.
      # Force bwrap to map whatever external UID it has to cave-coder (1000) inside.
      (unsafe-add-raw-args "--uid 1000 --gid 1000")
      # /dev/fuse for fuse-overlayfs (needed for nested caves)
      (add-runtime ''
        [ -e /dev/fuse ] && RUNTIME_ARGS+=(--dev-bind /dev/fuse /dev/fuse)
      '')
      (unsafe-add-raw-args "--symlink /bin/sh /bin/bash")
      (unsafe-add-raw-args "--symlink ${pkgs.zsh}/bin/zsh /bin/zsh")
      (set-env "SHELL" "/bin/bash")
      (set-env "CODING_CAVE_VERSION" version)
      (set-env "HOME" jailHome)
      # Persistent home directory (bind mount, survives across sessions).
      (add-runtime ''
        CAVE_HOME="$HOME/.local/share/coding-cave/home"
        mkdir -p "$CAVE_HOME"
        RUNTIME_ARGS+=(--bind "$CAVE_HOME" ${jailHome})
      '')
      # `network` prevents bwrap from doing --unshare-net, so it inherits
      # the surrounding network namespace (pasta's isolated one on the host,
      # or the outer cave's namespace when running nested).
      network

      # no-new-session omitted: cave init needs its own session to survive terminal close

      # Nix CLI for autonomous package management inside the cave
      # Writable /nix/store overlay (fuse-overlayfs, mounted by wrapper.py).
      # Host nix DB is seeded at startup so nix doesn't try to re-register
      # (chmod) existing lower-layer paths, which fails on overlayfs.
      (try-fwd-env "CODING_CAVE_NIX_MERGED")
      (try-fwd-env "CODING_CAVE_NIX_DB_DIR")
      (add-runtime ''
        if [ -n "''${CODING_CAVE_NIX_MERGED:-}" ]; then
          RUNTIME_ARGS+=(--bind "$CODING_CAVE_NIX_MERGED" /nix/store)
          RUNTIME_ARGS+=(--tmpfs /nix/var --dir /nix/var/nix)
          if [ -n "''${CODING_CAVE_NIX_DB_DIR:-}" ] && [ -d "''${CODING_CAVE_NIX_DB_DIR}" ]; then
            RUNTIME_ARGS+=(--bind "''${CODING_CAVE_NIX_DB_DIR}" /nix/var/nix/db)
          fi
        fi
      '')
      (set-env "NIX_CONFIG" "sandbox = false\nexperimental-features = nix-command flakes")
      (set-env "NIX_PATH" "nixpkgs=${nixpkgs}")

      # Dev tools
      (add-pkg-deps (with pkgs; [
        claude-code.packages.x86_64-linux.default
        nix
        git
        github-cli
        ripgrep
        fd
        jq
        python3
        curl
        coreutils
        gawk
        gnused
        gnugrep
        findutils
        diffutils
        patch
        gnutar
        gzip
        unzip
        wget
        perl
        tree
        less
        file
        ncurses
        which
        zsh
        eza
        nano
        direnv
        uv
        glibc.bin
        claude-loop
        util-linux
        fuse-overlayfs
        fuse3
        gnupg
      ]))
      (set-env "EDITOR" "${pkgs.nano}/bin/nano")
      (set-env "TERMINFO_DIRS" "${pkgs.ncurses}/share/terminfo")
      (set-env "LOCALE_ARCHIVE" "${pkgs.glibcLocales}/lib/locale/locale-archive")
      (set-env "LANG" "en_US.UTF-8")
      (try-fwd-env "COLORTERM")

      # Git identity: forwarded from host when available (eliminates
      # re-authoring on pull).  Falls back to throwaway identity.
      (try-fwd-env "CODING_CAVE_GIT_NAME")
      (try-fwd-env "CODING_CAVE_GIT_EMAIL")
      (try-fwd-env "CODING_CAVE_GIT_SIGNING_KEY")
      (try-fwd-env "CODING_CAVE_GIT_GPG_SIGN")
      (try-fwd-env "CODING_CAVE_GPG_AGENT_SOCK")
      (try-fwd-env "CODING_CAVE_GNUPGHOME")
      (add-runtime ''
        if [ -n "''${CODING_CAVE_GIT_NAME:-}" ]; then
          GIT_AUTHOR_NAME="''${CODING_CAVE_GIT_NAME}"
          GIT_COMMITTER_NAME="''${CODING_CAVE_GIT_NAME}"
        else
          GIT_AUTHOR_NAME="coding-cave"
          GIT_COMMITTER_NAME="coding-cave"
        fi
        export GIT_AUTHOR_NAME GIT_COMMITTER_NAME
        if [ -n "''${CODING_CAVE_GIT_EMAIL:-}" ]; then
          GIT_AUTHOR_EMAIL="''${CODING_CAVE_GIT_EMAIL}"
          GIT_COMMITTER_EMAIL="''${CODING_CAVE_GIT_EMAIL}"
        else
          GIT_AUTHOR_EMAIL="coding-cave@noreply"
          GIT_COMMITTER_EMAIL="coding-cave@noreply"
        fi
        export GIT_AUTHOR_EMAIL GIT_COMMITTER_EMAIL
        # GPG commit signing via forwarded agent socket.
        # Git reads signing key and gpgsign from config, not env vars.
        # Write a global gitconfig inside the cave with these settings.
        if [ -n "''${CODING_CAVE_GPG_AGENT_SOCK:-}" ] && [ -e "''${CODING_CAVE_GPG_AGENT_SOCK}" ]; then
          mkdir -p ${jailHome}/.gnupg
          chmod 700 ${jailHome}/.gnupg 2>/dev/null || true
        fi
      '')

      # Credential forwarding
      (try-fwd-env "CLAUDE_CODE_OAUTH_TOKEN")
      (try-fwd-env "ANTHROPIC_API_KEY")
      (try-fwd-env "GITHUB_TOKEN")

      # Extra bwrap args file from wrapper.py
      (try-fwd-env "CODING_CAVE_BWRAP_ARGS")

      # First-run init for Claude Code state
      (try-fwd-env "CODING_CAVE_CLAUDE_SETTINGS")
      (try-fwd-env "CODING_CAVE_SKILLS_DIR")
      (add-runtime ''
        CAVE_HOME="$HOME/.local/share/coding-cave/home"
        CLAUDE_DIR="$CAVE_HOME/.claude"
        if [ ! -d "$CLAUDE_DIR" ]; then
          mkdir -p "$CLAUDE_DIR"
          if [ -n "''${CODING_CAVE_CLAUDE_SETTINGS:-}" ] && [ -f "''${CODING_CAVE_CLAUDE_SETTINGS}" ]; then
            cp "''${CODING_CAVE_CLAUDE_SETTINGS}" "$CLAUDE_DIR/settings.json"
          else
            cp ${defaultClaudeSettings} "$CLAUDE_DIR/settings.json"
          fi
          chmod 644 "$CLAUDE_DIR/settings.json"
          printf '%s' '{"hasCompletedOnboarding":true,"theme":"dark","effortCalloutDismissed":true}' > "$CAVE_HOME/.claude.json"
        fi
        # Seed built-in skills (claude-loop)
        SKILLS_DIR="$CLAUDE_DIR/skills"
        mkdir -p "$SKILLS_DIR"
        for f in ${claude-loop.src}/commands/*.md; do
          [ -f "$f" ] || continue
          name="''${f##*/}"
          name="''${name%.md}"
          if [ ! -d "$SKILLS_DIR/$name" ]; then
            mkdir -p "$SKILLS_DIR/$name"
            cp "$f" "$SKILLS_DIR/$name/SKILL.md"
          fi
        done
        # Seed per-user skills if missing
        if [ -n "''${CODING_CAVE_SKILLS_DIR:-}" ] && [ -d "''${CODING_CAVE_SKILLS_DIR}" ]; then
          for d in "''${CODING_CAVE_SKILLS_DIR}"/*/; do
            [ -d "$d" ] || continue
            base="$(basename "$d")"
            if [ ! -d "$SKILLS_DIR/$base" ]; then
              cp -r "$d" "$SKILLS_DIR/$base"
            fi
          done
        fi
      '')

      # Extra bwrap args from wrapper.py (project mounts, dotfiles, passwd).
      (add-runtime ''
        if [ -n "''${CODING_CAVE_BWRAP_ARGS:-}" ] && [ -f "''${CODING_CAVE_BWRAP_ARGS}" ]; then
          while IFS= read -r -d "" arg; do
            RUNTIME_ARGS+=("$arg")
          done < "''${CODING_CAVE_BWRAP_ARGS}"
        fi
      '')

      # Entry: set up /projects, configure GPG signing, save env, then exec
      (wrap-entry (entry: ''
        mkdir -p /projects
        export TMPDIR="$HOME/.cache/tmp"
        mkdir -p "$TMPDIR"
        export PATH="$HOME/.local/bin:$PATH"
        # Configure GPG commit signing if host forwarded signing key
        if [ -n "''${CODING_CAVE_GIT_SIGNING_KEY:-}" ]; then
          git config --global user.signingkey "''${CODING_CAVE_GIT_SIGNING_KEY}"
        fi
        if [ -n "''${CODING_CAVE_GIT_GPG_SIGN:-}" ]; then
          git config --global commit.gpgsign "''${CODING_CAVE_GIT_GPG_SIGN}"
        fi
        export -p > /tmp/cave-env
        chmod 444 /tmp/cave-env
        # Ensure shells started via nsenter get the cave environment.
        # Without this, nsenter inherits the host's env and source
        # cave-env from bash -c is unreliable.
        printf '%s\n' '. /tmp/cave-env' > /etc/zshenv
        printf '%s\n' '. /tmp/cave-env' > /etc/profile
        exec "$@"
      ''))
    ];

    gpuCombinators = with jail.combinators; [
      gpu
      (add-runtime ''
        for dev in /dev/nvidia{0,1,ctl,-uvm,-uvm-tools,-modeset}; do
          [ -e "$dev" ] && RUNTIME_ARGS+=(--dev-bind "$dev" "$dev")
        done
        NVIDIA_SMI="$(command -v nvidia-smi 2>/dev/null || true)"
        if [ -n "$NVIDIA_SMI" ]; then
          NVIDIA_BIN="$(dirname "$(realpath "$NVIDIA_SMI")")"
          RUNTIME_ARGS+=(--ro-bind "$NVIDIA_BIN" "$NVIDIA_BIN")
          RUNTIME_ARGS+=(--symlink "$NVIDIA_BIN/nvidia-smi" /bin/nvidia-smi)
        fi
        RUNTIME_ARGS+=(--setenv LD_LIBRARY_PATH "/run/opengl-driver/lib''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}")
      '')
    ];

    sandbox = jail "coding-cave" pkgs.bashInteractive baseCombinators;
    sandboxGpu = jail "coding-cave" pkgs.bashInteractive (baseCombinators ++ gpuCombinators);

    # Reference individual files to avoid copying a directory to the Nix
    # store (chmod on dirs fails on fuse-overlayfs inside the cave).
    codingCaveState = pkgs.replaceVars ./coding_cave/_state.py {
      SANDBOX_BIN = "${sandbox}/bin/coding-cave";
      SANDBOX_GPU_BIN = "${sandboxGpu}/bin/coding-cave";
      VERSION = version;
    };
    codingCavePkg = pkgs.runCommand "coding-cave-pkg" {} ''
      mkdir -p $out/coding_cave
      cp ${./coding_cave/__init__.py}  $out/coding_cave/__init__.py
      cp ${./coding_cave/__main__.py}  $out/coding_cave/__main__.py
      cp ${./coding_cave/cli.py}       $out/coding_cave/cli.py
      cp ${./coding_cave/commands.py}  $out/coding_cave/commands.py
      cp ${./coding_cave/config.py}    $out/coding_cave/config.py
      cp ${./coding_cave/git.py}       $out/coding_cave/git.py
      cp ${./coding_cave/gitdir.py}    $out/coding_cave/gitdir.py
      cp ${./coding_cave/lifecycle.py} $out/coding_cave/lifecycle.py
      cp ${./coding_cave/overlay.py}   $out/coding_cave/overlay.py
      cp ${./coding_cave/projects.py}  $out/coding_cave/projects.py
      cp ${./coding_cave/sessions.py}  $out/coding_cave/sessions.py
      cp ${./coding_cave/sync.py}      $out/coding_cave/sync.py
      cp ${codingCaveState}            $out/coding_cave/_state.py
    '';

    # -c with explicit sys.path avoids CWD shadowing the built package
    # (python3 -m puts CWD at sys.path[0], which finds unsubstituted
    # source _state.py when run from the project directory).
    baseScript = pkgs.writeText "coding-cave-script" ''
      export PATH="${pkgs.lib.makeBinPath [pkgs.util-linux pkgs.passt pkgs.fuse-overlayfs]}:$PATH"
      exec ${pkgs.python3}/bin/python3 -c '
import sys
sys.path.insert(0, "${codingCavePkg}")
from coding_cave.cli import main
main()
' "$@"
    '';

    wrapper = pkgs.runCommand "coding-cave-with-alias" {} ''
      mkdir -p $out/bin
      cat ${baseScript} > $out/bin/coding-cave
      chmod +x $out/bin/coding-cave
      ln -s coding-cave $out/bin/cav
    '';

  in {
    packages.x86_64-linux.default = wrapper;
  };
}
