"""Build-time tests for the home-manager conversion of mkCaveEnv.

These tests evaluate the coding-cave flake's lib.mkCaveEnv with a fixture
user module and assert that:
- system + user hooks both end up in settings.json
- system + user CLAUDE.md fragments concatenate in declaration order
- the .claude/ tree is materialized as real files (not store symlinks)
- home.sessionVariables flow through to env.sh
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


FLAKE_PATH = Path(__file__).resolve().parent.parent


def nix_build(expr):
    """Build a nix expression and return the output store path."""
    r = subprocess.run(
        [
            "nix", "build", "--impure", "--no-link", "--print-out-paths",
            "--expr", expr,
        ],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise AssertionError(f"nix build failed:\n{r.stderr}")
    return Path(r.stdout.strip().split("\n")[0])


def cave_env_expr(user_module_body):
    """Build a nix expression that produces a cave-user-env for a fixture."""
    return f"""
let
  flake = builtins.getFlake "path:{FLAKE_PATH}";
  caveEnv = flake.lib.mkCaveEnv {{
    claude-code = flake.inputs.claude-code-nix;
    userModule = {{ pkgs, ... }}: {user_module_body};
  }};
in caveEnv.packages.x86_64-linux.default
"""


class TestHmConversion(unittest.TestCase):
    """Verify the deep-merge layer composes system and user config."""

    def test_empty_user_module_baseline(self):
        out = nix_build(cave_env_expr("{ }"))
        self.assertTrue((out / "files" / ".claude" / "settings.json").is_file())
        self.assertTrue((out / "files" / ".claude" / "CLAUDE.md").is_file())
        self.assertTrue((out / "home-path").is_symlink())
        self.assertTrue((out / "env.sh").is_file())

        settings = json.loads((out / "files" / ".claude" / "settings.json").read_text())
        # cave-system defaults
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["reasoning_effort"], "high")
        self.assertEqual(settings["sandbox"]["enabled"], False)
        self.assertEqual(settings["statusLine"]["type"], "command")

    def test_files_are_real_not_symlinks(self):
        """home-files contents must be real files in $out, not symlinks
        into the store, so the cave's persistent home survives across
        sessions when the store overlay disappears.
        """
        out = nix_build(cave_env_expr("{ }"))
        settings_path = out / "files" / ".claude" / "settings.json"
        self.assertTrue(settings_path.is_file())
        self.assertFalse(settings_path.is_symlink())
        claude_md_path = out / "files" / ".claude" / "CLAUDE.md"
        self.assertTrue(claude_md_path.is_file())
        self.assertFalse(claude_md_path.is_symlink())

    def test_hooks_deep_merge(self):
        """User hooks must end up in settings.json without erasing the
        coding-cave defaults.
        """
        out = nix_build(cave_env_expr("""{
          coding-cave.claude-code.hooks.Stop = [{
            hooks = [{ type = "command"; command = "echo USER_STOP_HOOK"; }];
          }];
        }"""))
        settings = json.loads((out / "files" / ".claude" / "settings.json").read_text())
        self.assertIn("hooks", settings)
        self.assertIn("Stop", settings["hooks"])
        self.assertEqual(len(settings["hooks"]["Stop"]), 1)
        self.assertEqual(
            settings["hooks"]["Stop"][0]["hooks"][0]["command"],
            "echo USER_STOP_HOOK",
        )
        # Cave-system extraSettings should still be present
        self.assertEqual(settings["model"], "opus")

    def test_memory_fragments_concatenate(self):
        """User memory fragments concat in declaration order: system first."""
        out = nix_build(cave_env_expr("""{
          coding-cave.claude-code.memoryFragments = [
            "## User addendum\\n\\nBe extra careful."
          ];
        }"""))
        claude_md = (out / "files" / ".claude" / "CLAUDE.md").read_text()
        # System fragment text from defaults/system-claude.md
        self.assertIn("Coding Cave", claude_md)
        # User fragment
        self.assertIn("User addendum", claude_md)
        # System should come before user (declaration order)
        self.assertLess(
            claude_md.index("Coding Cave"),
            claude_md.index("User addendum"),
        )

    def test_extra_settings_deep_merge(self):
        """User extraSettings deep-merge on top of cave-system defaults."""
        out = nix_build(cave_env_expr("""{
          coding-cave.claude-code.extraSettings = {
            includeCoAuthoredBy = false;
            statusLine.padding = 2;
          };
        }"""))
        settings = json.loads((out / "files" / ".claude" / "settings.json").read_text())
        self.assertEqual(settings["includeCoAuthoredBy"], False)
        # statusLine.command from cave-system survives the deep merge
        self.assertEqual(settings["statusLine"]["command"], "~/.claude/statusline.sh")
        # User addition
        self.assertEqual(settings["statusLine"]["padding"], 2)

    def test_session_variables_in_env_sh(self):
        """home.sessionVariables flow through to env.sh."""
        out = nix_build(cave_env_expr("""{
          home.sessionVariables = {
            EDITOR = "nano";
            FOO_BAR = "baz";
          };
        }"""))
        env_sh = (out / "env.sh").read_text()
        self.assertIn('export EDITOR="nano"', env_sh)
        self.assertIn('export FOO_BAR="baz"', env_sh)

    def test_statusline_script_materialized(self):
        """The statusline.sh from cave-system gets baked as a real file."""
        out = nix_build(cave_env_expr("{ }"))
        statusline = out / "files" / ".claude" / "statusline.sh"
        self.assertTrue(statusline.is_file())
        self.assertFalse(statusline.is_symlink())
        # Should still be executable
        self.assertTrue(os.access(statusline, os.X_OK))


class TestBundleResolution(unittest.TestCase):
    """Verify the resolveBundle helper extracts skills/commands from src."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        # Create a bundle dir with a skill and a command
        skill_dir = self.tmpdir / "skills" / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: demo\n---\n\nDemo skill body.\n"
        )
        cmd_dir = self.tmpdir / "commands"
        cmd_dir.mkdir()
        (cmd_dir / "demo-cmd.md").write_text(
            "---\ndescription: demo command\n---\n\nDemo command body.\n"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_bundle_src_extraction(self):
        """A bundle with `src` pointing at a dir with skills/ and commands/
        should produce ~/.claude/skills/<name>/SKILL.md and
        ~/.claude/commands/<name>.md.
        """
        out = nix_build(cave_env_expr(f"""{{
          coding-cave.claude-code.bundles = [ {{ src = {self.tmpdir}; }} ];
        }}"""))
        skill = out / "files" / ".claude" / "skills" / "demo-skill" / "SKILL.md"
        self.assertTrue(skill.is_file(), f"missing: {skill}")
        self.assertIn("Demo skill body", skill.read_text())

        cmd = out / "files" / ".claude" / "commands" / "demo-cmd.md"
        self.assertTrue(cmd.is_file(), f"missing: {cmd}")
        self.assertIn("Demo command body", cmd.read_text())


class TestPluginsPassthrough(unittest.TestCase):
    """coding-cave.claude-code.plugins forwards to programs.claude-code.plugins,
    which HM bakes as --plugin-dir args in the claude wrapper script."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        plugin_meta = self.tmpdir / ".claude-plugin"
        plugin_meta.mkdir()
        (plugin_meta / "plugin.json").write_text(
            '{"name":"canonical-demo","description":"demo","version":"0.0.1"}\n'
        )
        skill_dir = self.tmpdir / "skills" / "canonical-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: canonical-skill\ndescription: demo\n---\n\nBody.\n"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_plugin_dir_baked_into_wrapper(self):
        out = nix_build(cave_env_expr(f"""{{
          coding-cave.claude-code.plugins = [ {self.tmpdir} ];
        }}"""))
        claude_wrapper = out / "home-path" / "bin" / "claude"
        self.assertTrue(claude_wrapper.is_file(), f"missing: {claude_wrapper}")
        wrapper_text = claude_wrapper.read_text()
        # The tmpdir is copied into the store; the basename is preserved.
        self.assertIn("--plugin-dir", wrapper_text)
        self.assertIn(self.tmpdir.name, wrapper_text)


if __name__ == "__main__":
    unittest.main()
