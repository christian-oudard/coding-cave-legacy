"""Tests for CLI argument parsing and command construction."""
import wrapper


def parse(argv):
    """Parse argv through the same split logic as main()."""
    claude_args = []
    if "--" in argv:
        idx = argv.index("--")
        claude_args = argv[idx + 1:]
        argv = argv[:idx]
    parser = wrapper.build_parser()
    parsed = parser.parse_args(argv)
    return parsed, claude_args


class TestResumeArg:
    def test_bare_resume(self):
        parsed, _ = parse(["--resume"])
        assert parsed.resume is True

    def test_resume_short(self):
        parsed, _ = parse(["-r"])
        assert parsed.resume is True

    def test_resume_with_session_id(self):
        parsed, _ = parse(["--resume", "abc-123"])
        assert parsed.resume == "abc-123"

    def test_resume_short_with_session_id(self):
        parsed, _ = parse(["-r", "abc-123"])
        assert parsed.resume == "abc-123"

    def test_no_resume(self):
        parsed, _ = parse([])
        assert parsed.resume is None

    def test_resume_on_agent_subcommand(self):
        parsed, _ = parse(["agent", "--resume", "xyz"])
        assert parsed.resume == "xyz"

    def test_bare_resume_on_agent_subcommand(self):
        parsed, _ = parse(["agent", "-r"])
        assert parsed.resume is True


class TestClaudeParts:
    """Test the claude command construction logic extracted from cmd_agent."""

    @staticmethod
    def build_claude_parts(continue_session=False, resume=None, claude_args=None):
        parts = ["claude", "--dangerously-skip-permissions"]
        if continue_session:
            parts.append("--continue")
        elif resume is not None:
            parts.append("--resume")
            if resume is not True:
                parts.append(resume)
        if claude_args:
            parts.extend(claude_args)
        return parts

    def test_no_flags(self):
        assert self.build_claude_parts() == ["claude", "--dangerously-skip-permissions"]

    def test_continue(self):
        parts = self.build_claude_parts(continue_session=True)
        assert parts == ["claude", "--dangerously-skip-permissions", "--continue"]

    def test_resume_picker(self):
        parts = self.build_claude_parts(resume=True)
        assert parts == ["claude", "--dangerously-skip-permissions", "--resume"]

    def test_resume_with_id(self):
        parts = self.build_claude_parts(resume="abc-123")
        assert parts == ["claude", "--dangerously-skip-permissions", "--resume", "abc-123"]

    def test_continue_trumps_resume(self):
        parts = self.build_claude_parts(continue_session=True, resume="abc")
        assert "--resume" not in parts
        assert "--continue" in parts

    def test_resume_with_extra_claude_args(self):
        parts = self.build_claude_parts(resume="abc", claude_args=["--verbose"])
        assert parts == ["claude", "--dangerously-skip-permissions", "--resume", "abc", "--verbose"]
