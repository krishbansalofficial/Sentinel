"""The explicit unconfined path for eval hidden tests (``--allow-unconfined-hidden-tests``).

Process creation stays in ``execution/`` (D-04). This runs a hidden-test command
as a plain host child with a minimal environment and bounded output; callers
label every result it produces ``UNCONFINED``. It is never chosen automatically.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from backend.app.execution._process import CapturedProcess, capture, minimal_environment

OUTPUT_LIMIT_BYTES = 256 * 1024


def run_hidden_tests_unconfined(argv: Sequence[str], *, cwd: Path, timeout: float) -> CapturedProcess:
    if not argv or not Path(argv[0]).is_absolute():
        raise ValueError("the hidden-test executable must be an absolute path")
    return capture(list(argv), cwd=cwd, env=minimal_environment(), timeout=timeout,
                   limit=OUTPUT_LIMIT_BYTES, max_timeout=max(timeout, 1))
