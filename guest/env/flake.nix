{
  description = "cave-env: the guest's half of a cave user environment";

  # No inputs, deliberately. nixpkgs, home-manager and claude-code are
  # arguments to mkCaveEnv rather than inputs here, because the generated
  # wrapper flake in /var/lib/coding-cave/env is what pins them: it takes
  # them from the guest system's own closure, so an agent's environment is
  # built against the same nixpkgs the guest is running and cannot drag a
  # second glibc into the VM. A flake with inputs of its own would offer a
  # second answer to that question.
  #
  # This is a flake at all only so the wrapper can name it as an input and
  # lock it by narHash. It lives at a store path installed by the system
  # config, so its version *is* the deployed generation, and `main`'s
  # caveSourceHash -- which existed to notice edits to these files -- has
  # nothing left to do.
  outputs = { self }: {
    lib.mkCaveEnv = import ./lib/mkCaveEnv.nix;
  };
}
