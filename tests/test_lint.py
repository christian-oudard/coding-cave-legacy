"""Static analysis tests to prevent recurring bugs.

These tests grep the codebase for patterns that have caused bugs before.
They run without any external dependencies (no git, no network).

Run with: python3 -m unittest -v tests.test_lint
"""

import ast
import importlib
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CAV_DIR = PROJECT_ROOT / "cav"


class TestImportSmoke(unittest.TestCase):
    """Every module must import without error.

    Catches: missing imports, undefined names at load time, syntax errors.
    This would have caught the NameError on _seed_claude_config and
    the missing 'import os' in user_env.py.
    """

    def test_all_modules_import(self):
        """Every .py file under cav/ must be importable."""
        sys.path.insert(0, str(PROJECT_ROOT))
        failures = []
        for py in sorted(CAV_DIR.glob("*.py")):
            mod_name = f"cav.{py.stem}"
            try:
                importlib.import_module(mod_name)
            except Exception as e:
                failures.append(f"{mod_name}: {e}")
        if failures:
            self.fail("Failed to import:\n" + "\n".join(failures))


class TestPathHomeLint(unittest.TestCase):
    """Prevent bare Path.home() calls outside approved locations.

    Path.home() inside a cave returns /home/cave-coder, not the real host
    home. Code that needs the host home must use _host_home(). Code that
    needs depth-aware paths must use the appropriate helper.

    This would have caught the 3 separate Path.home() bugs that each
    required individual fixes.
    """

    # Approved Path.home() usage: {filename: {function_or_variable_names}}
    # Duplicate keys merged manually since Python dicts overwrite.
    ALLOWED = {
        # DATA_DIR default uses Path.home()
        "config.py": {"DATA_DIR"},
        # _host_home fallback + CODING_CAVE_HOST_HOME fallback in launch()
        # _crinj_ca_path intentionally checks cave home (where cave-local crinj puts its cert)
        "sandbox.py": {"_host_home", "launch", "_crinj_ca_path"},
        # _cave_nix_path uses CODING_CAVE_HOST_HOME with Path.home() fallback
        "user_env.py": {"_cave_nix_path"},
        # _offer_cave_nix runs on host only (cav init), Path.home() is correct
        "init.py": {"_offer_cave_nix"},
    }

    def test_no_bare_path_home(self):
        """Path.home() must not appear outside approved patterns."""
        violations = []
        for py in sorted(CAV_DIR.glob("*.py")):
            try:
                tree = ast.parse(py.read_text(), filename=py.name)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "home"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "Path"):
                    continue
                # Check if this call is inside an approved function or assignment
                approved = False
                allowed_names = self.ALLOWED.get(py.name, set())
                for parent in ast.walk(tree):
                    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if parent.name in allowed_names:
                            if (parent.lineno <= node.lineno
                                    <= parent.end_lineno):
                                approved = True
                                break
                    if isinstance(parent, ast.Assign):
                        for target in parent.targets:
                            if (isinstance(target, ast.Name)
                                    and target.id in allowed_names
                                    and parent.lineno <= node.lineno
                                    <= (parent.end_lineno or parent.lineno)):
                                approved = True
                                break
                if not approved:
                    violations.append(f"{py.name}:{node.lineno}")
        if violations:
            self.fail(
                "Bare Path.home() found outside approved locations.\n"
                "Inside a cave, Path.home() returns the cave home, not the host home.\n"
                "Use _host_home() for host paths, or add to ALLOWED if intentional.\n"
                "Violations:\n  " + "\n  ".join(violations)
            )


class TestSandboxEnvCompleteness(unittest.TestCase):
    """Verify launch() sets all env vars that flake.nix expects.

    The sandbox binary (jail.nix) reads env vars to configure bind mounts,
    PATH, and identity. If launch() forgets one, the cave starts broken.

    This would have caught: missing CODING_CAVE_HOME, missing proxy config,
    missing CLAUDE_CODE_OAUTH_TOKEN.
    """

    def _extract_env_refs_from_nix(self) -> set[str]:
        """Parse flake.nix for CODING_CAVE_* env var references."""
        flake = PROJECT_ROOT / "flake.nix"
        if not flake.exists():
            self.skipTest("flake.nix not found")
        text = flake.read_text()
        import re
        # Match env var names in try-fwd-env, fwd-env, and shell variable references
        refs = set()
        for m in re.finditer(r'(?:try-fwd-env|fwd-env)\s+"(CODING_CAVE_\w+)"', text):
            refs.add(m.group(1))
        for m in re.finditer(r'\$\{?(CODING_CAVE_\w+)', text):
            refs.add(m.group(1))
        return refs

    def _extract_env_sets_from_launch(self) -> set[str]:
        """Parse sandbox.py launch() for env vars it sets."""
        sandbox = CAV_DIR / "sandbox.py"
        text = sandbox.read_text()
        import re
        sets = set()
        # Match any quoted CODING_CAVE_* string in sandbox.py
        for m in re.finditer(r'["\'](CODING_CAVE_\w+)["\']', text):
            sets.add(m.group(1))
        return sets

    def test_launch_sets_all_required_env_vars(self):
        """Every CODING_CAVE_* var referenced in flake.nix must be set in launch()."""
        nix_refs = self._extract_env_refs_from_nix()
        launch_sets = self._extract_env_sets_from_launch()

        # These are conditionally set (only when the feature is available)
        conditional = {
            "CODING_CAVE_RECURSIVE",     # only with --recursive
            "CODING_CAVE_GITCONFIG",     # only if host has .gitconfig
            "CODING_CAVE_GPG_SOCKET",    # only if gpg available
            "CODING_CAVE_GPG_PUBRING",   # only if gpg keyring exists
            "CODING_CAVE_USER_ENV",      # only if cave.nix exists
            "CODING_CAVE_USER_CLAUDE_MD",  # only if user CLAUDE.md exists
            "CODING_CAVE_NIX_DB_DIR",    # only if host has nix DB
        }

        missing = nix_refs - launch_sets - conditional
        if missing:
            self.fail(
                "flake.nix references env vars not set by launch():\n  "
                + "\n  ".join(sorted(missing))
                + "\nThese will be unset when the sandbox starts."
            )


class TestClaudeCommandConstruction(unittest.TestCase):
    """Verify launch() passes the right session flags to claude.

    This would have caught bd12bd3, which removed --session-id and left only
    --resume unconditionally. New sessions passed --resume <non-existent-uuid>,
    claude ignored it and started fresh with its own UUID, leaving the DB with
    a pre-generated UUID that never matched any session file on disk.
    """

    def _get_launch_source(self) -> str:
        sandbox = CAV_DIR / "sandbox.py"
        src = sandbox.read_text()
        # Extract just the launch() function body
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "launch":
                lines = src.splitlines()
                return "\n".join(lines[node.lineno - 1:node.end_lineno])
        self.fail("launch() function not found in sandbox.py")

    def test_launch_has_resuming_parameter(self):
        """launch() must accept a 'resuming' parameter to distinguish new vs continue."""
        src = self._get_launch_source()
        self.assertIn("resuming", src,
                      "launch() missing 'resuming' parameter — cannot distinguish "
                      "--session-id (new) from --resume (continue)")

    def test_launch_uses_session_id_for_new_sessions(self):
        """launch() must pass --session-id when starting a new session."""
        src = self._get_launch_source()
        self.assertIn("--session-id", src,
                      "launch() does not pass --session-id for new sessions. "
                      "Without this, claude ignores the pre-generated UUID and "
                      "the DB entry never matches the session file on disk.")

    def test_launch_uses_resume_for_continuing(self):
        """launch() must pass --resume when continuing an existing session."""
        src = self._get_launch_source()
        self.assertIn("--resume", src,
                      "launch() does not pass --resume for continued sessions.")

    def test_session_id_and_resume_are_conditional(self):
        """--session-id and --resume must be under the resuming conditional, not both always."""
        src = self._get_launch_source()
        # Both flags present but mutually exclusive: resuming guards which is used
        self.assertIn("resuming", src)
        self.assertIn("--session-id", src)
        self.assertIn("--resume", src)
        # They must appear in the same conditional block — both after 'resuming'
        resume_pos = src.index("--resume")
        session_id_pos = src.index("--session-id")
        resuming_pos = src.index("resuming")
        self.assertLess(resuming_pos, resume_pos,
                        "'resuming' check must appear before --resume")
        self.assertLess(resuming_pos, session_id_pos,
                        "'resuming' check must appear before --session-id")



class TestGitWrapperGuard(unittest.TestCase):
    """The git wrapper must not auto-rebase during an active rebase.

    If the wrapper runs git rebase while an outer rebase is in progress,
    the inner rebase fails and git rebase --abort kills the outer rebase.
    The branch silently stays at the old SHA.
    """

    def test_wrapper_skips_rebase_when_rebase_in_progress(self):
        """GIT_WRAPPER must check for .git/rebase-merge or rebase-apply."""
        from cav.sandbox import GIT_WRAPPER
        self.assertIn("rebase-merge", GIT_WRAPPER,
                      "Wrapper must check for .git/rebase-merge to avoid "
                      "nested rebase aborting the outer rebase")
        self.assertIn("rebase-apply", GIT_WRAPPER,
                      "Wrapper must check for .git/rebase-apply to avoid "
                      "nested rebase aborting the outer rebase")
        # The guard must appear before the rebase command
        guard_pos = GIT_WRAPPER.index("rebase-merge")
        rebase_pos = GIT_WRAPPER.index("$_CAV_REAL_GIT\" rebase")
        self.assertLess(guard_pos, rebase_pos,
                        "rebase-merge guard must come before git rebase command")


class TestSeccompFilter(unittest.TestCase):
    """Verify seccomp BPF filter generation is well-formed."""

    def test_filter_size(self):
        """Filter must have exactly 3 + N + 2 instructions (8 bytes each)."""
        from cav.seccomp_gen import generate_filter, BLOCKED_X86_64, BLOCKED_AARCH64
        for arch, blocked in [("x86_64", BLOCKED_X86_64), ("aarch64", BLOCKED_AARCH64)]:
            bpf = generate_filter(arch)
            expected = (3 + len(blocked) + 2) * 8
            self.assertEqual(len(bpf), expected, f"{arch} filter size mismatch")

    def test_filter_blocks_io_uring(self):
        """io_uring syscalls must be in the blocked list."""
        from cav.seccomp_gen import BLOCKED_X86_64
        self.assertIn("io_uring_setup", BLOCKED_X86_64)
        self.assertIn("io_uring_enter", BLOCKED_X86_64)
        self.assertIn("io_uring_register", BLOCKED_X86_64)

    def test_filter_blocks_keyctl(self):
        """Kernel keyring syscalls must be in the blocked list."""
        from cav.seccomp_gen import BLOCKED_X86_64
        self.assertIn("keyctl", BLOCKED_X86_64)
        self.assertIn("add_key", BLOCKED_X86_64)

    def test_filter_allows_needed_syscalls(self):
        """Syscalls needed by the cave must NOT be blocked."""
        from cav.seccomp_gen import BLOCKED_X86_64
        needed = ["clone", "clone3", "unshare", "mount", "umount2",
                   "pivot_root", "setns", "mknod", "personality",
                   "ptrace"]  # needed for gdb/strace; Yama handles scope
        for name in needed:
            self.assertNotIn(name, BLOCKED_X86_64,
                             f"{name} is blocked but needed by cave")

    def test_bpf_instructions_valid(self):
        """Every BPF instruction must have valid jump offsets."""
        import struct
        from cav.seccomp_gen import generate_filter, BLOCKED_X86_64
        bpf = generate_filter("x86_64")
        n_insns = len(bpf) // 8
        for i in range(n_insns):
            code, jt, jf, k = struct.unpack("HBBI", bpf[i*8:(i+1)*8])
            # Jump targets must not exceed filter bounds
            if code & 0x07 == 0x05:  # BPF_JMP class
                self.assertLess(i + 1 + jt, n_insns,
                                f"insn[{i}] jt overflow")
                self.assertLess(i + 1 + jf, n_insns,
                                f"insn[{i}] jf overflow")


class TestDeadCode(unittest.TestCase):
    """Detect unused code with vulture.

    Catches: unused functions, variables, imports, and classes.
    Vulture gives decorated functions lower confidence since it can't trace
    decorator dispatch. We avoid false positives by following a consistent
    pattern: click commands delegate to a module via lazy import, so
    vulture sees the name used. This lets us run at min_confidence=0.
    """

    # Click commands registered via @cli.command() or @group.command()
    # that vulture can't trace through decorator dispatch.
    CLICK_COMMANDS = {"version", "env_build", "env_update", "env_metadata"}

    def test_no_dead_code(self):
        import vulture
        v = vulture.Vulture()
        v.scavenge([str(CAV_DIR)])
        unused = [
            item for item in v.get_unused_code(min_confidence=0)
            if item.name not in self.CLICK_COMMANDS
        ]
        if unused:
            self.fail(
                "Dead code detected:\n"
                + "\n".join(str(item) for item in unused)
            )


if __name__ == "__main__":
    unittest.main()
