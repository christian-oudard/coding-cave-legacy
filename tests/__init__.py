"""Shared test utilities."""

import os

# Tests assume host-side behavior unless a specific test opts in to
# inside-cave behavior (e.g. via mock.patch.dict or local setUp).
# Running the suite from inside a cave must not change fixture semantics.
os.environ.pop("CODING_CAVE_VERSION", None)

from cav.registry import find_agent_by_session

__all__ = ["find_agent_by_session"]
