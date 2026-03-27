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
        "sandbox.py": {"_host_home", "launch"},
        # _cave_nix_path uses CODING_CAVE_HOST_HOME with Path.home() fallback
        "user_env.py": {"_cave_nix_path"},
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
        # Match env["CODING_CAVE_*"] = and "CODING_CAVE_*": patterns
        for m in re.finditer(r'["\'](\w+)["\'](?:\s*[:\]])', text):
            name = m.group(1)
            if name.startswith("CODING_CAVE_") or name.startswith("CAV_"):
                sets.add(name)
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
        }

        missing = nix_refs - launch_sets - conditional
        if missing:
            self.fail(
                "flake.nix references env vars not set by launch():\n  "
                + "\n  ".join(sorted(missing))
                + "\nThese will be unset when the sandbox starts."
            )


if __name__ == "__main__":
    unittest.main()
