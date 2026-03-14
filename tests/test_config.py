"""Tests for config module: identity forwarding and GPG detection."""
import json
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coding_cave.config import _detect_gpg_agent_socket, _git_config, setup_env
from coding_cave import _state

FLAKE_NIX = Path(__file__).parent.parent / "flake.nix"


class TestGitConfig:
    def test_reads_existing_config(self, tmp_path):
        """_git_config reads a git global config value."""
        # This test relies on the actual host git config, which may
        # not have user.name set. Use a fake gitconfig instead.
        gitconfig = tmp_path / ".gitconfig"
        gitconfig.write_text("[user]\n\tname = TestName\n\temail = test@test.com\n")
        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            assert _git_config("user.name") == "TestName"
            assert _git_config("user.email") == "test@test.com"

    def test_returns_none_for_missing(self, tmp_path):
        """_git_config returns None for unset keys."""
        gitconfig = tmp_path / ".gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n")
        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            assert _git_config("user.signingkey") is None


class TestGpgDetection:
    def test_returns_none_when_no_gpg(self, tmp_path):
        """_detect_gpg_agent_socket returns None when no socket exists."""
        with patch.dict(os.environ, {"GNUPGHOME": str(tmp_path)}):
            assert _detect_gpg_agent_socket() is None

    def test_finds_socket_in_gnupghome(self, tmp_path):
        """_detect_gpg_agent_socket finds socket in GNUPGHOME."""
        sock = tmp_path / "S.gpg-agent"
        sock.touch()  # not a real socket, but exists
        with patch.dict(os.environ, {"GNUPGHOME": str(tmp_path)}):
            assert _detect_gpg_agent_socket() == str(sock)


class TestFlakeNixSafety:
    """Static analysis of flake.nix to catch host/cave confusion.

    add-runtime blocks run on the HOST before bwrap starts.
    They must never perform filesystem operations (mkdir, chmod, cp, etc.)
    on jailHome (/home/cave-coder) — that path only exists inside the cave.
    Legitimate uses like --bind targets are fine (they're bwrap args, not
    host filesystem ops).
    """

    JAIL_HOME = "/home/cave-coder"

    # Filesystem-mutating commands that must not target jailHome in add-runtime.
    # Matches both literal /home/cave-coder and Nix interpolation ${jailHome}.
    JAIL_HOME_PAT = r"(?:/home/cave-coder|\$\{jailHome\})"
    MUTATING_CMDS = re.compile(
        r"^\s*(?:mkdir|chmod|chown|cp|mv|rm|touch|ln|cat|printf|tee|install)"
        r"\b.*" + JAIL_HOME_PAT,
        re.MULTILINE,
    )

    @staticmethod
    def _extract_blocks(text, block_type):
        """Extract shell code from (block_type '' ... '') blocks in flake.nix."""
        # Nix multi-line strings: (block_type '' ... '')
        # We match the content between '' markers, handling '' escaping.
        blocks = []
        pattern = re.compile(
            r"\(" + re.escape(block_type) + r"\s+''(.*?)(?<!')''\)",
            re.DOTALL,
        )
        for m in pattern.finditer(text):
            blocks.append(m.group(1))
        return blocks

    def test_add_runtime_no_mutating_ops_on_jail_home(self):
        """add-runtime must not mkdir/chmod/cp/etc under /home/cave-coder."""
        flake = FLAKE_NIX.read_text()
        blocks = self._extract_blocks(flake, "add-runtime")
        assert blocks, "Should find at least one add-runtime block"

        violations = []
        for i, block in enumerate(blocks):
            # Unescape Nix '' escapes: ''$ -> $, ''" -> ", ''\n -> \n
            clean = block.replace("''$", "$").replace("''\\", "\\")
            for match in self.MUTATING_CMDS.finditer(clean):
                # Exclude bwrap args (RUNTIME_ARGS+=)
                line = match.group(0).strip()
                if "RUNTIME_ARGS" in line:
                    continue
                violations.append(f"add-runtime block {i}: {line}")

        assert not violations, (
            "add-runtime blocks must not perform filesystem ops on "
            f"{self.JAIL_HOME} (runs on host):\n" + "\n".join(violations)
        )

    def test_wrap_entry_exists(self):
        """Sanity check: flake.nix has a wrap-entry block."""
        flake = FLAKE_NIX.read_text()
        assert "wrap-entry" in flake

    def test_gpg_agent_sock_forwarded(self):
        """CODING_CAVE_GPG_AGENT_SOCK is forwarded into the cave via try-fwd-env."""
        flake = FLAKE_NIX.read_text()
        assert '(try-fwd-env "CODING_CAVE_GPG_AGENT_SOCK")' in flake

    def test_gnupghome_not_forwarded(self):
        """CODING_CAVE_GNUPGHOME is host-only (bind-mount in lifecycle.py)."""
        flake = FLAKE_NIX.read_text()
        assert '(try-fwd-env "CODING_CAVE_GNUPGHOME")' not in flake

    def test_gnupg_in_packages(self):
        """gnupg is included in sandbox packages (needed for GPG signing)."""
        flake = FLAKE_NIX.read_text()
        assert "gnupg" in flake

    def test_gpg_mkdir_in_wrap_entry_not_add_runtime(self):
        """GPG .gnupg dir creation must happen in wrap-entry, not add-runtime."""
        flake = FLAKE_NIX.read_text()
        wrap_entries = self._extract_blocks(flake, "wrap-entry")
        # wrap-entry uses a lambda, extract differently
        assert any(".gnupg" in block for block in wrap_entries) or \
            "mkdir -p \"$HOME/.gnupg\"" in flake, \
            ".gnupg creation should be in wrap-entry"
        # And NOT in add-runtime (covered by test_add_runtime_no_mutating_ops)

    def test_gpg_signing_config_in_wrap_entry(self):
        """GPG signing key and gpgsign config set inside cave (wrap-entry)."""
        flake = FLAKE_NIX.read_text()
        # These git config commands must run inside the cave where
        # ~/.gitconfig is writable, not on the host
        assert "git config --global user.signingkey" in flake
        assert "git config --global commit.gpgsign" in flake
        # Verify they're in wrap-entry, not add-runtime
        add_runtime_blocks = self._extract_blocks(flake, "add-runtime")
        for block in add_runtime_blocks:
            assert "git config --global user.signingkey" not in block, \
                "signingkey config must be in wrap-entry, not add-runtime"
            assert "git config --global commit.gpgsign" not in block, \
                "gpgsign config must be in wrap-entry, not add-runtime"

    def test_git_identity_in_wrap_entry_not_add_runtime(self):
        """GIT_AUTHOR/COMMITTER env vars must be set in wrap-entry.

        add-runtime runs on the host before bwrap, but bwrap uses --clearenv,
        so env vars exported in add-runtime are dropped. The identity setup
        must happen in wrap-entry (inside the cave) where CODING_CAVE_GIT_*
        vars have already been forwarded via try-fwd-env.
        """
        flake = FLAKE_NIX.read_text()
        add_runtime_blocks = self._extract_blocks(flake, "add-runtime")
        for block in add_runtime_blocks:
            clean = block.replace("''$", "$").replace("''\\", "\\")
            assert "GIT_AUTHOR_NAME" not in clean, \
                "GIT_AUTHOR_NAME must be set in wrap-entry, not add-runtime (--clearenv drops it)"
            assert "GIT_COMMITTER_NAME" not in clean, \
                "GIT_COMMITTER_NAME must be set in wrap-entry, not add-runtime (--clearenv drops it)"


class TestGpgLifecycleSafety:
    """Verify GPG forwarding in lifecycle.py doesn't expose private keys."""

    LIFECYCLE_PY = FLAKE_NIX.parent / "coding_cave" / "lifecycle.py"

    def test_gpg_socket_bind_is_rw(self):
        """GPG agent socket must be --bind (rw), not --ro-bind."""
        src = self.LIFECYCLE_PY.read_text()
        # Socket needs rw for bidirectional communication
        assert '--bind", gpg_sock' in src or \
            '"--bind", gpg_sock' in src

    def test_gpg_keyring_bind_is_ro(self):
        """Public keyring files must be --ro-bind (read-only)."""
        src = self.LIFECYCLE_PY.read_text()
        assert '"--ro-bind", src' in src

    def test_private_keys_never_mounted(self):
        """Private key files (private-keys-v1.d, secring.gpg) never bound."""
        src = self.LIFECYCLE_PY.read_text()
        assert "private-keys" not in src
        assert "secring" not in src

    def test_only_specific_keyring_files_mounted(self):
        """Only pubring.kbx, pubring.gpg, trustdb.gpg are mounted."""
        src = self.LIFECYCLE_PY.read_text()
        # The for loop should only iterate over these safe files
        assert "pubring.kbx" in src
        assert "pubring.gpg" in src
        assert "trustdb.gpg" in src

    def test_gpg_env_vars_in_allowlist(self):
        """GPG env vars are in BWRAP_ENV_ALLOWLIST."""
        src = self.LIFECYCLE_PY.read_text()
        assert "CODING_CAVE_GPG_AGENT_SOCK" in src
        assert "CODING_CAVE_GNUPGHOME" in src


class TestBwrapArgsSafety:
    """Verify bwrap args from lifecycle.py never bind into /nix/store.

    bwrap processes args in order: the nix store overlay (--bind nix_merged
    /nix/store) is set up by the sandbox runtime, but lifecycle.py's extra
    args are appended after.  A --ro-bind targeting a destination inside
    /nix/store will fail because bwrap tries to mkdir parents before the
    overlay is mounted.
    """

    @staticmethod
    def _extract_bind_destinations(args):
        """Extract destination paths from bwrap --bind/--ro-bind args."""
        bind_ops = {"--bind", "--ro-bind", "--dev-bind"}
        dests = []
        i = 0
        while i < len(args):
            if args[i] in bind_ops and i + 2 < len(args):
                dests.append(args[i + 2])
                i += 3
            else:
                i += 1
        return dests

    def test_no_bind_destinations_in_nix_store(self, cave_env):
        """No mirror bind mount should target a path inside /nix/store.

        Calls _build_mirror_args() with CODING_CAVE_NVIM_HOST_BIN set to a
        nix store path, verifying no destination lands inside /nix/store.
        """
        from coding_cave.lifecycle import _build_mirror_args

        fake_nvim = cave_env.tmp_path / "fake-nvim"
        fake_nvim.write_text("#!/bin/sh\n")
        fake_nvim.chmod(0o755)

        env_patch = {
            "CODING_CAVE_NVIM_HOST_BIN": str(fake_nvim),
            "CODING_CAVE_ZDOTDIR": "",
            "CODING_CAVE_BASHRC": "",
            "CODING_CAVE_BASH_PROFILE": "",
            "CODING_CAVE_NVIM_CONFIG": "",
        }
        with patch.dict(os.environ, env_patch):
            args = _build_mirror_args()

        dests = self._extract_bind_destinations(args)
        assert len(dests) > 0, "expected at least one bind mount for nvim"
        for dest in dests:
            assert not dest.startswith("/nix/store"), (
                f"bwrap bind destination must not be inside /nix/store "
                f"(overlay not set up yet): {dest}"
            )


class TestMergeNixDb:
    """Test incremental nix DB merge for new host paths."""

    def _create_db(self, path):
        import sqlite3
        db = sqlite3.connect(str(path))
        db.executescript("""
            CREATE TABLE ValidPaths (
                id integer primary key autoincrement not null,
                path text unique not null,
                hash text not null,
                registrationTime integer not null,
                deriver text,
                narSize integer,
                ultimate integer,
                sigs text,
                ca text
            );
            CREATE TABLE Refs (
                referrer integer not null,
                reference integer not null,
                primary key (referrer, reference),
                foreign key (referrer) references ValidPaths(id) on delete cascade,
                foreign key (reference) references ValidPaths(id) on delete restrict
            );
        """)
        return db

    def test_new_paths_copied(self, tmp_path):
        from coding_cave.lifecycle import _merge_nix_db
        host_db = tmp_path / "host.sqlite"
        cave_db = tmp_path / "cave.sqlite"
        h = self._create_db(host_db)
        c = self._create_db(cave_db)
        # Both have path A, only host has path B
        h.execute("INSERT INTO ValidPaths (path,hash,registrationTime,narSize) "
                  "VALUES ('/nix/store/aaa','sha256:aaa',1,100)")
        h.execute("INSERT INTO ValidPaths (path,hash,registrationTime,narSize) "
                  "VALUES ('/nix/store/bbb','sha256:bbb',2,200)")
        h.commit(); h.close()
        c.execute("INSERT INTO ValidPaths (path,hash,registrationTime,narSize) "
                  "VALUES ('/nix/store/aaa','sha256:aaa',1,100)")
        c.commit(); c.close()

        _merge_nix_db(host_db, cave_db)

        import sqlite3
        db = sqlite3.connect(str(cave_db))
        paths = {r[0] for r in db.execute("SELECT path FROM ValidPaths")}
        assert paths == {"/nix/store/aaa", "/nix/store/bbb"}
        db.close()

    def test_cave_only_paths_preserved(self, tmp_path):
        from coding_cave.lifecycle import _merge_nix_db
        host_db = tmp_path / "host.sqlite"
        cave_db = tmp_path / "cave.sqlite"
        h = self._create_db(host_db)
        c = self._create_db(cave_db)
        h.execute("INSERT INTO ValidPaths (path,hash,registrationTime,narSize) "
                  "VALUES ('/nix/store/aaa','sha256:aaa',1,100)")
        h.commit(); h.close()
        # Cave has aaa + cave-only ccc
        c.execute("INSERT INTO ValidPaths (path,hash,registrationTime,narSize) "
                  "VALUES ('/nix/store/aaa','sha256:aaa',1,100)")
        c.execute("INSERT INTO ValidPaths (path,hash,registrationTime,narSize) "
                  "VALUES ('/nix/store/ccc','sha256:ccc',3,300)")
        c.commit(); c.close()

        _merge_nix_db(host_db, cave_db)

        import sqlite3
        db = sqlite3.connect(str(cave_db))
        paths = {r[0] for r in db.execute("SELECT path FROM ValidPaths")}
        assert "/nix/store/ccc" in paths
        db.close()

    def test_refs_copied_with_id_remapping(self, tmp_path):
        from coding_cave.lifecycle import _merge_nix_db
        host_db = tmp_path / "host.sqlite"
        cave_db = tmp_path / "cave.sqlite"
        h = self._create_db(host_db)
        c = self._create_db(cave_db)
        # Host: A(id=1) -> B(id=2), B is new
        h.execute("INSERT INTO ValidPaths (path,hash,registrationTime,narSize) "
                  "VALUES ('/nix/store/aaa','sha256:aaa',1,100)")
        h.execute("INSERT INTO ValidPaths (path,hash,registrationTime,narSize) "
                  "VALUES ('/nix/store/bbb','sha256:bbb',2,200)")
        h.execute("INSERT INTO Refs (referrer,reference) VALUES (2,1)")
        h.commit(); h.close()
        # Cave already has A with different id
        c.execute("INSERT INTO ValidPaths (path,hash,registrationTime,narSize) "
                  "VALUES ('/nix/store/aaa','sha256:aaa',1,100)")
        c.commit(); c.close()

        _merge_nix_db(host_db, cave_db)

        import sqlite3
        db = sqlite3.connect(str(cave_db))
        # B should reference A, with cave-local ids
        cave_a_id = db.execute("SELECT id FROM ValidPaths WHERE path='/nix/store/aaa'").fetchone()[0]
        cave_b_id = db.execute("SELECT id FROM ValidPaths WHERE path='/nix/store/bbb'").fetchone()[0]
        refs = db.execute("SELECT referrer,reference FROM Refs").fetchall()
        assert (cave_b_id, cave_a_id) in refs
        db.close()

    def test_noop_when_no_new_paths(self, tmp_path):
        from coding_cave.lifecycle import _merge_nix_db
        host_db = tmp_path / "host.sqlite"
        cave_db = tmp_path / "cave.sqlite"
        h = self._create_db(host_db)
        c = self._create_db(cave_db)
        h.execute("INSERT INTO ValidPaths (path,hash,registrationTime,narSize) "
                  "VALUES ('/nix/store/aaa','sha256:aaa',1,100)")
        h.commit(); h.close()
        c.execute("INSERT INTO ValidPaths (path,hash,registrationTime,narSize) "
                  "VALUES ('/nix/store/aaa','sha256:aaa',1,100)")
        c.commit(); c.close()

        _merge_nix_db(host_db, cave_db)  # should not raise


def _sync_nix_db():
    """Register unregistered nix store paths so nix build works on overlayfs.

    Inside the cave, /nix/store is a fuse-overlayfs mount. The host's store
    is the lower layer. Paths added to the host store after the cave's nix DB
    was seeded exist on disk but aren't in the DB. When nix encounters these,
    it tries to register them (chmod 0555), which fails on overlayfs because
    the cave user doesn't own the lower-layer files.

    Fix: compute NAR hashes for unregistered paths and register them via
    nix-store --load-db (skips chmod).
    """
    import hashlib

    # Get all registered paths
    result = subprocess.run(
        ["nix", "path-info", "--all"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return
    registered = set(result.stdout.strip().splitlines())

    # Get all paths on disk (skip internal dirs like .links and non-store entries)
    store = Path("/nix/store")
    if not store.is_dir():
        return
    _STORE_NAME = re.compile(r"[a-z0-9]{32}-.+")
    on_disk = {f"/nix/store/{p}" for p in os.listdir(store)
               if _STORE_NAME.fullmatch(p)}

    unregistered = on_disk - registered
    if not unregistered:
        return

    # Compute NAR hashes and build --load-db input
    entries = []
    for path in sorted(unregistered):
        try:
            dump = subprocess.run(
                ["nix-store", "--dump", path],
                capture_output=True, timeout=30,
            )
            if dump.returncode != 0:
                continue
            h = hashlib.sha256(dump.stdout).hexdigest()
            entries.append(f"{path}\n{h}\n{len(dump.stdout)}\n\n0")
        except (subprocess.TimeoutExpired, OSError):
            continue

    if entries:
        load = subprocess.run(
            ["nix-store", "--load-db"],
            input="\n".join(entries) + "\n",
            capture_output=True, text=True,
        )
        if load.returncode != 0:
            raise RuntimeError(f"nix-store --load-db failed: {load.stderr}")


class TestNixBuild:
    """Integration tests: nix build and built binary verification.

    Inside the cave, nix DB must be synced first (see _sync_nix_db).
    """

    @pytest.fixture(scope="class", autouse=True)
    def sync_db(self):
        """Sync nix DB before any nix build tests."""
        _sync_nix_db()

    @pytest.fixture(scope="class")
    def built_output(self):
        """Build the flake and return the output path."""
        result = subprocess.run(
            ["nix", "build", "--no-link", "--print-out-paths"],
            capture_output=True, text=True, cwd=str(FLAKE_NIX.parent),
            timeout=300,
        )
        assert result.returncode == 0, f"nix build failed: {result.stderr}"
        return Path(result.stdout.strip())

    @pytest.fixture(scope="class")
    def sandbox_scripts(self, built_output):
        """Extract sandbox and entry scripts from the built output.

        Walks: wrapper -> coding-cave-pkg/_state.py -> SANDBOX_BIN ->
        jail-wrapper entry script.
        """
        wrapper = (built_output / "bin" / "coding-cave").read_text()
        pkg_match = re.search(r"/nix/store/[a-z0-9]+-coding-cave-pkg", wrapper)
        assert pkg_match, "can't find coding-cave-pkg in wrapper"
        state_src = (Path(pkg_match.group()) / "coding_cave" /
                     "_state.py").read_text()

        sandbox_match = re.search(
            r'SANDBOX_BIN\s*=\s*"(/nix/store/[^"]+)"', state_src)
        assert sandbox_match, "can't find SANDBOX_BIN in _state.py"
        sandbox_script = Path(sandbox_match.group(1)).read_text()

        jail_match = re.search(
            r"(/nix/store/[a-z0-9]+-coding-cave-jail-wrapper)/bin/",
            sandbox_script)
        assert jail_match, "can't find jail-wrapper in sandbox script"
        entry_script = (Path(jail_match.group(1)) / "bin" /
                        "coding-cave-jail-wrapper").read_text()

        return sandbox_script, entry_script

    def test_nix_build(self, built_output):
        """nix build produces a working binary."""
        assert built_output.is_dir()
        assert (built_output / "bin" / "coding-cave").exists()
        assert (built_output / "bin" / "cav").is_symlink()

    def test_sandbox_entry_has_gpg_setup(self, sandbox_scripts):
        """Built sandbox entry script configures GPG signing."""
        _, entry_script = sandbox_scripts
        assert 'CODING_CAVE_GPG_AGENT_SOCK' in entry_script, \
            "entry script missing GPG agent socket check"
        assert 'mkdir -p "$HOME/.gnupg"' in entry_script, \
            "entry script missing .gnupg dir creation"
        assert "git config --global user.signingkey" in entry_script, \
            "entry script missing signingkey config"
        assert "git config --global commit.gpgsign" in entry_script, \
            "entry script missing gpgsign config"

    def test_sandbox_has_gpg_env_forwarding(self, sandbox_scripts):
        """Built sandbox forwards GPG env vars via try-fwd-env."""
        sandbox_script, _ = sandbox_scripts
        assert "CODING_CAVE_GPG_AGENT_SOCK" in sandbox_script, \
            "sandbox missing GPG agent socket forwarding"
        # GNUPGHOME is host-only (lifecycle.py bind-mount), not forwarded
        assert "CODING_CAVE_GIT_SIGNING_KEY" in sandbox_script, \
            "sandbox missing signing key forwarding"

    def test_sandbox_entry_has_git_identity(self, sandbox_scripts):
        """Built entry script sets GIT_AUTHOR/COMMITTER from forwarded vars."""
        _, entry_script = sandbox_scripts
        assert "GIT_AUTHOR_NAME" in entry_script, \
            "entry script missing GIT_AUTHOR_NAME setup"
        assert "GIT_COMMITTER_NAME" in entry_script, \
            "entry script missing GIT_COMMITTER_NAME setup"
        assert "CODING_CAVE_GIT_NAME" in entry_script, \
            "entry script missing CODING_CAVE_GIT_NAME reference"

    def test_sandbox_has_gnupg_in_path(self, sandbox_scripts):
        """Built sandbox includes gnupg in PATH."""
        sandbox_script, _ = sandbox_scripts
        assert "gnupg" in sandbox_script, \
            "sandbox PATH missing gnupg"
