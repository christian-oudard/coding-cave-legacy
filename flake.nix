{
  description = "Coding Cave (vm-cave): dev shell for the bootstrap VM tooling";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});

      # The Go binaries. cave-vm is the host CLI the bootstrap script is
      # migrating into: `vmcav review|approve` already run it. cave-guestd
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
        # The VMM, plus ch-remote for the ACPI power button. Named here rather
        # than in require_tools because it is per-platform: an Apple silicon
        # host runs vfkit instead, and cave-vm is what knows the difference.
        cloud-hypervisor
        openssh           # ssh over vsock, ssh-keygen for the agent key
        socat             # scripts/test-gpg-broker.sh only; the plane is Go now
        e2fsprogs         # mke2fs, builds the seed image rootlessly
        gnupg             # the host half of the guest's commit signing
        git
        python3           # config parsing (tomllib), gpg-broker
        jq
        # bash is here for the `#!/usr/bin/env bash` shebang, which resolves
        # against the PATH the wrapper sets, not the caller's.
        bash coreutils gnused gnugrep procps  # truncate, readlink, ps, ...
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
      # and neither suite makes calls. Nothing here boots a VM: that costs a
      # guest build and minutes of boot, so it stays a manual `vmcav start`.
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
      } // (
        # The shell suites, which are all one shape: run a script out of
        # scripts/ against a throwaway repo. None starts a VM, opens a socket
        # or reaches the network, so scripts/ is the whole input and editing a
        # doc does not invalidate any of them.
        let
          shellTest = name: pkgs.runCommand "${name}-test"
            {
              # One set for every suite rather than a table of which needs
              # what. python3, socat and gnupg are the gpg broker's; zsh is
              # for the completion script, which is checked by asking zsh to
              # parse it. A per-suite table would be a second list to keep in
              # step with the first, and the list below is derived precisely
              # so there is no such list to get wrong.
              #
              # cave-vm is among them, and that is a decision rather than a
              # convenience. The shell is becoming a caller of it, so the seam
              # between them is where a port goes wrong -- the shell gathering
              # the wrong inputs, or reading the answer back wrongly -- and a
              # suite that stubs the binary cannot see that seam at all.
              #
              # It was tried once and backed out, because adding it dropped one
              # suite from 52 assertions to 30 and that looked like the suites
              # being fragile. It was not: `correct_guest_clock` ended in a
              # pipeline whose grep matched nothing, which under `set -euo
              # pipefail` ended the script, and the function returns early when
              # cave-vm is absent. So the suites without it could not see a bug
              # that killed every command on a real host, and the suite with it
              # reported that bug honestly and was disbelieved.
              nativeBuildInputs = with pkgs; [
                bash git python3 socat gnupg zsh (caveVm pkgs)
              ];
            }
            ''
              mkdir -p repo && cp -r ${./scripts} repo/scripts
              # guest/ is here for test-cave-git, which tests the guest's git
              # wrapper: it is a shell script against local repos, so the VM
              # has nothing to do with whether it is correct.
              cp -r ${./guest} repo/guest
              export HOME=$TMPDIR REPO=$PWD/repo
              bash repo/scripts/test-${name}.sh && touch $out
            '';

          # Every suite in scripts/, read off the directory rather than
          # listed here.
          #
          # It was a hand-written list of five, and there are ten. The four
          # newest and the gpg broker's were all absent, so `nix flake check`
          # -- the one command that says whether this tree is good -- ran half
          # the tests and said yes. A list of tests is exactly the thing that
          # goes stale, because adding a test is the moment you are thinking
          # about something else.
          suites = map (n: nixpkgs.lib.removeSuffix ".sh"
                            (nixpkgs.lib.removePrefix "test-" n))
            (builtins.filter
              (n: nixpkgs.lib.hasPrefix "test-" n && nixpkgs.lib.hasSuffix ".sh" n)
              (builtins.attrNames (builtins.readDir ./scripts)));
        in
        nixpkgs.lib.genAttrs suites shellTest
      ));

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
