package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"codingcave.dev/cave-vm/internal/userenv"
)

const (
	// Where the spec and the build live. Under /var/lib rather than /etc,
	// because it is state the guest owns and mutates: /etc here is the
	// system's, written by a nixos-rebuild and read-only between them.
	defaultEnvDir = "/var/lib/coding-cave/env"
	// What the deployed system says the environment must be built against.
	// Written by the system config, so it moves with the generation.
	defaultEnvConfig = "/etc/cave/env.json"
)

// envConfig is the system's half: the store paths a build is pinned to. Read
// from a file rather than taken as flags, so a redeploy changes them in one
// place and no caller can be running with a stale set it was told once.
type envConfig struct {
	CaveEnv string         `json:"caveEnv"`
	Inputs  userenv.Inputs `json:"inputs"`
}

// envCmd is the guest side of `cav env`: build the user environment, and put
// it into an agent's home.
//
//	cave-guestd env build [-force]
//	cave-guestd env seed -home DIR -user NAME
//
// `seed` is the one seed-agent-home@ calls, and it does both: an environment
// that has never been built is built now, because the alternative is an agent
// starting with no settings and no explanation.
func envCmd(args []string) {
	if len(args) == 0 {
		usage()
	}
	fs := flag.NewFlagSet("env", flag.ExitOnError)
	dir := fs.String("dir", defaultEnvDir, "the spec and build directory")
	configPath := fs.String("config", defaultEnvConfig, "store paths to build against")
	force := fs.Bool("force", false, "rebuild even when nothing changed")
	home := fs.String("home", "", "the agent's home (seed)")
	username := fs.String("user", "", "the agent's user (seed)")
	fs.Parse(args[1:])

	env, err := loadEnv(*configPath, *dir)
	if err != nil {
		die(err)
	}

	switch args[0] {
	case "build":
		if _, err := ensureBuilt(env, *force); err != nil {
			die(err)
		}
	case "seed":
		if *home == "" || *username == "" {
			die(fmt.Errorf("seed needs -home and -user"))
		}
		result, err := ensureBuilt(env, *force)
		if err != nil {
			// Deliberately not fatal. This runs from seed-agent-home@, which a
			// session Requires=, so dying here would mean a spec with a typo in
			// it stops every agent from starting -- including the one whose job
			// is to fix the spec. The agent comes up with whatever its home
			// already had, and the reason is in the journal.
			fmt.Fprintln(os.Stderr, "cave-guestd: env build failed, leaving the home as it is:", err)
			return
		}
		if result == "" {
			return
		}
		if err := userenv.Materialize(result, *home, *username); err != nil {
			die(err)
		}
	default:
		usage()
	}
}

func loadEnv(path, dir string) (userenv.Env, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return userenv.Env{}, fmt.Errorf("reading %s: %w", path, err)
	}
	var cfg envConfig
	if err := json.Unmarshal(b, &cfg); err != nil {
		return userenv.Env{}, fmt.Errorf("parsing %s: %w", path, err)
	}
	if cfg.CaveEnv == "" || cfg.Inputs.Nixpkgs == "" {
		return userenv.Env{}, fmt.Errorf("%s names no cave-env or nixpkgs", path)
	}
	return userenv.Env{Dir: dir, CaveEnv: cfg.CaveEnv, Inputs: cfg.Inputs}, nil
}

// ensureBuilt returns the path of a current build, building only if the spec
// or the system moved. Returns "" when there is nothing to materialize, which
// only happens if a build failed and there was no previous result.
func ensureBuilt(env userenv.Env, force bool) (string, error) {
	stamp, err := env.Stamp()
	if err != nil {
		return "", err
	}
	if !force && !env.Changed(stamp) {
		return env.Result(), nil
	}
	if _, err := env.Prepare(); err != nil {
		return "", err
	}
	path, err := env.Build()
	if err != nil {
		// A previous result is better than none: the agent keeps the
		// environment it had rather than losing its settings to a typo.
		if _, statErr := os.Lstat(env.Result()); statErr == nil {
			return "", fmt.Errorf("%w (keeping the previous environment)", err)
		}
		return "", err
	}
	if err := env.WriteStamp(stamp); err != nil {
		return "", err
	}
	fmt.Fprintln(os.Stderr, "cave-guestd: cave environment built:", path)
	return env.Result(), nil
}
