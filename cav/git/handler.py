"""Git protocol handler for the per-agent daemon.

Reads one pkt-line request from stdin, validates it, and execs the
appropriate git service. Designed to be invoked by socat for each
connection.

Security invariants:
- Only git-upload-pack and git-receive-pack are allowed.
- Paths are resolved under base_path and must not escape it.
- CODING_CAVE_AGENT_ID is set server-side, not client-controllable.
- Payload size is bounded to prevent resource exhaustion.
- Malformed requests are rejected without information leakage.
"""

import os
import sys

MAX_PKT_LEN = 65520  # git protocol maximum


def parse_request(data: bytes) -> tuple[str, str] | None:
    """Parse a pkt-line payload into (service, path) or None on error."""
    parts = data.split(b"\x00")
    try:
        request = parts[0].decode("ascii")
    except (UnicodeDecodeError, IndexError):
        return None
    pieces = request.split(" ", 1)
    if len(pieces) != 2:
        return None
    return pieces[0], pieces[1]


def validate_service(service: str) -> bool:
    return service in ("git-upload-pack", "git-receive-pack")


def resolve_repo(base_path: str, request_path: str) -> str | None:
    """Resolve request path under base_path safely.

    Returns the real path if valid, None if it escapes base_path.
    Uses os.path.commonpath to avoid string prefix attacks like
    /base_pathevil/ matching startswith("/base_path").
    """
    # Normalize: strip leading slashes, collapse ..
    cleaned = os.path.normpath(request_path.lstrip("/"))
    # Reject any component that is ".."
    if ".." in cleaned.split(os.sep):
        return None
    candidate = os.path.normpath(os.path.join(base_path, cleaned))
    # Verify containment using commonpath (immune to prefix tricks)
    try:
        common = os.path.commonpath([base_path, candidate])
    except ValueError:
        return None
    if common != os.path.normpath(base_path):
        return None
    # Reject null bytes (realpath raises ValueError on them)
    if "\x00" in candidate:
        return None
    # Now resolve symlinks (repos are symlinked to actual bare repos)
    return os.path.realpath(candidate)


def main():
    agent_id = os.environ.get("CODING_CAVE_HANDLER_AGENT_ID", "")
    base_path = os.environ.get("CODING_CAVE_HANDLER_BASE_PATH", "")

    if not agent_id or not base_path:
        sys.exit(1)

    os.environ["CODING_CAVE_AGENT_ID"] = agent_id

    # Read pkt-line header (4 hex digits)
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        sys.exit(0)  # EOF / empty connection

    try:
        pkt_len = int(raw_len, 16)
    except ValueError:
        sys.exit(1)

    if pkt_len < 4 or pkt_len > MAX_PKT_LEN:
        sys.exit(1)

    payload = sys.stdin.buffer.read(pkt_len - 4)
    if len(payload) != pkt_len - 4:
        sys.exit(1)  # truncated

    parsed = parse_request(payload)
    if parsed is None:
        sys.exit(1)

    service, path = parsed

    if not validate_service(service):
        sys.exit(1)

    repo = resolve_repo(base_path, path)
    if repo is None:
        sys.exit(1)

    os.execvp(service, [service, repo])


if __name__ == "__main__":
    main()
