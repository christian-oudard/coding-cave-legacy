{
  description = "Coding Cave (vm-cave): dev shell for the bootstrap VM tooling";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});

      # The Go binaries. cave-vm is the host CLI the bootstrap script is
      # migrating into: `vmcav review|diff|approve` already run it. cave-guestd
      # is built here too, but it ships in the guest image, not on the host.
      caveVm = pkgs: pkgs.buildGoModule {
        pname = "cave-vm";
        version = "0";
        src = ./cave-vm;
        vendorHash = null; # no dependencies, by design
        subPackages = [ "cmd/cave-vm" ];
        env.CGO_ENABLED = 0;
      };

      # The receive hooks, as executables on PATH rather than files copied out
      # of a checkout. What gets installed into the shared repo is a shim that
      # execs one of these by name, so the hook holds no store path and cannot
      # go stale on a gc or an upgrade, and vmcav works in a project that is
      # not this one. Same shape the container caves use.
      gitHooks = pkgs: pkgs.runCommand "cave-vm-git-hooks" { } ''
        install -Dm755 ${./scripts/git-hooks/pre-receive}  $out/bin/cave-vm-git-pre-receive
        install -Dm755 ${./scripts/git-hooks/post-receive} $out/bin/cave-vm-git-post-receive
      '';

      # Everything vmcav shells out to. The package pins them; the devShell
      # gets the same list so a working-tree vmcav behaves identically.
      # Kept in step with require_tools in scripts/vmcav.
      runtimeDeps = pkgs: [ (caveVm pkgs) (gitHooks pkgs) ] ++ (with pkgs; [
        cloud-hypervisor  # the VMM, plus ch-remote for the ACPI power button
        openssh           # ssh over vsock, ssh-keygen for the agent key
        util-linux        # setsid
        socat             # vsock <-> localhost bridges
        e2fsprogs         # mke2fs, builds the seed image rootlessly
        gnupg             # the host half of the guest's commit signing
        git
        python3           # config parsing (tomllib), vsock-connect, gpg-broker
        jq
        # bash is here for the `#!/usr/bin/env bash` shebang, which resolves
        # against the PATH the wrapper sets, not the caller's.
        bash coreutils gnused gnugrep procps  # id, cksum, truncate, ps, ...
      ]);
    in
    {
      # No `default` alias on purpose. This flake URL also serves main's cav,
      # and a default-attribute profile entry gets named after the directory
      # ("coding-cave"), which reads like the stable tool rather than this
      # branch's script. Named attribute, so the entry is named `vmcav`.
      packages = forAll (pkgs: {
        cave-vm = caveVm pkgs;
        # Bootstrap-era: vmcav is a script, so the derivation is a wrapper
        # that pins its PATH. It reads the guest flake and hooks out of your
        # checkout (found via the cwd's git toplevel), so the wrapper carries
        # no repo contents and stays valid across branches. Retired when the
        # Go binary takes over (docs/vm-cave-design.md).
        vmcav = pkgs.runCommand "vmcav"
          {
            nativeBuildInputs = [ pkgs.makeWrapper ];
            meta.mainProgram = "vmcav";
          }
          ''
            install -Dm755 ${./scripts/vmcav} $out/bin/vmcav
            wrapProgram $out/bin/vmcav \
              --set CAVE_VMCAV_REV ${self.rev or self.dirtyRev or "unknown"} \
              --prefix PATH : ${pkgs.lib.makeBinPath (runtimeDeps pkgs)}
          '';
      });

      # `nix flake check`. Offline by construction: no network in the sandbox,
      # and neither suite makes calls. The VM smoke test is not here, it needs
      # a build and minutes of boot.
      checks = forAll (pkgs: {
        # git is here because internal/approve drives real repos: the thing
        # worth testing is what the ref actually does, not a mock of it.
        go-tests = pkgs.runCommand "cave-vm-go-tests"
          { nativeBuildInputs = with pkgs; [ go git ]; }
          ''
            cp -r ${./cave-vm} src && chmod -R u+w src && cd src
            # CGO_ENABLED=0 is the ground rule for these binaries anyway, and
            # without it `net` wants a C compiler the sandbox has no reason to
            # carry.
            export HOME=$TMPDIR GOFLAGS=-mod=mod GOCACHE=$TMPDIR/go-cache
            export CGO_ENABLED=0
            go test ./... && touch $out
          '';
        # Depends on scripts/ alone, not the flake source, so editing a doc
        # does not invalidate it.
        git-hooks = pkgs.runCommand "git-hooks-test"
          { nativeBuildInputs = with pkgs; [ bash git ]; }
          ''
            mkdir -p repo && cp -r ${./scripts} repo/scripts
            export HOME=$TMPDIR REPO=$PWD/repo
            bash repo/scripts/test-git-hooks.sh && touch $out
          '';
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          # vmcav itself is deliberately absent: it comes from the profile
          # (`nix profile add .#vmcav`), so there is exactly one on PATH and
          # no working-tree copy shadowing it. To run an unreleased edit,
          # `nix run .#vmcav -- start`.
          packages = runtimeDeps pkgs ++ [ pkgs.go ];
        };
      });
    };
}
