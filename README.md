# Coding Cave

Coordinates multiple Claude agents working in parallel on a Git project, using Git as the sole coordination mechanism. Each agent runs in a bwrap sandbox with nix packages.

See [SPEC.md](SPEC.md) for the full specification.

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
nix develop --command cargo test
```

Integration tests (real git repos, cave launches) are not yet ported. See `cav-rs/` for the Rust test suite.

## Build

Nix-only. `nix build` produces the `cav` binary and all helper binaries.
