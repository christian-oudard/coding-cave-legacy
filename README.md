# Coding Cave

Coordinates multiple Claude agents working in parallel on a Git project, using
Git as the sole coordination mechanism. Each agent runs in a bubblewrap sandbox
with Nix packages.

See [SPEC.md](SPEC.md) for the full specification.

## Install

**Requirements:** NixOS, kernel ≥ 5.19 (for overlayfs over idmapped lower layers), bubblewrap (`bwrap`).

Add coding-cave to your NixOS flake inputs:

```nix
inputs.coding-cave = {
  url = "github:christian-oudard/coding-cave";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

Import `nixosModules.codingCave` into your system configuration (e.g. in `nixosConfigurations.<host>.modules`):

```nix
inputs.coding-cave.nixosModules.codingCave
```

Then `sudo nixos-rebuild switch`. That installs `cav` system-wide and brings up `coding-cave-host.service`, which provides the idmapped `/nix/store` mount caves consume as overlayfs lowerdir. Verify with `systemctl status coding-cave-host` and `cav --help`.

### Development install

For a dev build that coexists with the system one:

```
$ nix profile install /path/to/coding-cave#cav-debug
```

Installs `cav-debug` (dev profile, with `-v` timing logs and debug assertions) into your nix profile. The release `cav` from the host module remains the default; reach for `cav-debug` when iterating on cav itself.

### Non-NixOS hosts

Not yet supported by the install path. The host module is NixOS-specific; a future `cav install-host` subcommand will drop the equivalent systemd unit on other distros. For now, port the contents of `modules/host.nix` to your distro's service manager by hand.

## Quickstart

```
$ cd ~/code/myproject          # must be a git repo with at least one commit
$ cav init .                   # set up the shared repo and hooks
$ cav agent                    # launch an agent (auto-assigns agent ID)
```

The agent runs Claude Code in a sandbox. When you're happy with the work:

```
$ cav pull                     # fetch agent commits to your local branch
$ cav review 1                 # inspect what the agent did
$ cav approve 1                # fast-forward your base branch
```

To keep iterating:

```
$ cav push                     # send your edits back to the agent
$ cav status                   # see unmerged agent commits
```

## Bringing your dotfiles

The cave does not auto-mount your host `$HOME`. Anything you want available inside it (vim config, shell rc files, git config, ...) has to be declared in `~/.config/coding-cave/cave.nix`. Cave.nix is a home-manager module under the hood, so you have the full HM API: `home.file`, `xdg.configFile`, and the `programs.*` modules.

The pattern is to expose the dotfiles subtrees you want as `path:` flake inputs, then link them. Keep each input scoped to a directory that's safe to copy into the nix store — every byte of the input is mirrored into a store path that the cave can read, so a single broad `path:~/dotfiles` will pull in `.ssh/`, age-encrypted files, and anything else lurking. Pick the subdirectories the cave actually needs:

```nix
{
  inputs = {
    nvim-config = {
      url = "path:/home/you/dotfiles/dot_config/nvim";
      flake = false;
    };
  };

  config = { pkgs, nvim-config, ... }: {
    home.packages = with pkgs; [ neovim ];

    xdg.configFile."nvim" = {
      source = nvim-config;
      recursive = true;
    };

    home.sessionVariables.EDITOR = "nvim";
  };
}
```

`recursive = true` symlinks every file under the source individually instead of symlinking the directory itself. That leaves `~/.config/nvim/` writable for other modules — useful if you also want home-manager to drop generated files alongside (e.g. `xdg.configFile."nvim/lua/hm-generated.lua".text = ...`). Without it, the whole tree comes in as one symlink and nothing else can land inside.

For tools with a home-manager module (neovim, zsh, git, direnv, ...) prefer the declarative form — plugins, options, and aliases compose better than a raw symlink:

```nix
programs.neovim = {
  enable = true;
  defaultEditor = true;
  plugins = with pkgs.vimPlugins; [ vim-nix vim-fugitive ];
  extraConfig = builtins.readFile "${nvim-config}/init.vim";
};
```

After editing cave.nix, rebuild the environment:

```
cav env update nvim-config   # only if a flake input changed
cav env build
```

Note: the cave has no general internet egress, so plugin managers that fetch at runtime (lazy.nvim, packer, vim-plug, ...) will not work. Bring plugins in through nix instead.

## Testing

```
$ nix develop --command cargo test
```

Unit and integration tests live in `cav-rs/`. Run against real git repos, no
network required.

## Build

Nix-only. `nix build` produces the `cav` binary and all helper binaries
(`cav-git-pre-receive`, `cav-git-post-receive`, `cav-git-daemon`,
`cav-supervisor`, `cav-comms-broker`, `cav-comms-channel-server`).
