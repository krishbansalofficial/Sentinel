"""Hidden-test runners: where a task's hidden tests run once the agent has exited.

Each runner takes a fresh tree (the agent's result plus ``hidden_tests/``) that
the agent never saw, and runs ``hidden_test_command`` with no network:

* :class:`SandboxHiddenTestRunner` (Linux): the verified Linux sandbox
  (`execution.linux_sandbox`) over that tree, network off, the interpreter's
  install bound read-only. The result's ``boundary`` is ``LINUX_SANDBOX`` only
  when the sandbox verified; it never falls back to an unconfined run.
* :class:`VerificationHiddenTestRunner` (Windows): the hidden tests are added to
  the attempt's fixture repository and run through the Change's confined
  verification (an AppContainer check box) over the API.

The platform picks one explicitly (`cli`); an unconfined host runner exists only
in the test support code.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from backend.app.evals.runner import AgentOutcome, HiddenTestResult
from backend.app.evals.suite import EvalTask

OUTPUT_TAIL_BYTES = 4000
_TRUSTED_PATH = "/usr/local/bin:/usr/bin:/bin"


def _tail(data: bytes) -> str:
    return data[-OUTPUT_TAIL_BYTES:].decode("utf-8", "replace")


def resolve_hidden_command(command: tuple[str, ...]) -> list[str]:
    """``python``/``python3`` is this interpreter; anything else from a trusted PATH."""
    first, *rest = command
    if first in ("python", "python3"):
        return [os.path.realpath(sys.executable), *rest]
    if os.path.isabs(first):
        return [first, *rest]
    found = shutil.which(first, path=_TRUSTED_PATH)
    if found is None:
        raise RuntimeError(f"hidden test executable {first!r} is not installed")
    return [os.path.realpath(found), *rest]


class SandboxHiddenTestRunner:
    """Hidden tests inside the verified Linux sandbox, network off."""

    def __init__(self, cgroups: Callable[[], Any], *, limits: Any = None,
                 timeout_seconds: float = 600.0) -> None:
        self._cgroups_factory = cgroups
        self._cgroups = None
        self._limits = limits
        self._timeout = timeout_seconds

    def _hierarchy(self):
        if self._cgroups is None:
            hierarchy = self._cgroups_factory()
            hierarchy.prepare()
            self._cgroups = hierarchy
        return self._cgroups

    def run(self, task: EvalTask, tree: Path, *, outcome: AgentOutcome) -> HiddenTestResult:
        from backend.app.execution.cgroups import RunLimits
        from backend.app.execution.linux_sandbox import SandboxSpec, spawn_linux_sandbox

        argv = resolve_hidden_command(task.hidden_test_command)
        prefixes = sorted({Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()})
        readonly = tuple(path for path in prefixes if not str(path).startswith("/usr"))
        spec = SandboxSpec(
            argv=tuple(argv), cwd=tree, writable=(tree,), readonly=readonly, network=False,
            env={"PATH": _TRUSTED_PATH, "HOME": str(tree), "LANG": "C.UTF-8",
                 "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"})
        started = time.monotonic()
        cgroup = self._hierarchy().create_run(uuid4(), self._limits or RunLimits())
        process = spawn_linux_sandbox(spec, cgroup, redact=lambda text: text)
        timed_out = False
        try:
            stdout, stderr = process._popen.communicate(timeout=min(self._timeout,
                                                                    task.timeout_seconds))
        except Exception:
            timed_out = True
            process.kill()
            stdout, stderr = process._popen.communicate(timeout=10)
        finally:
            facts = process.linux_sandbox
            process.close()
        code = None if timed_out else process.returncode
        return HiddenTestResult(
            passed=(code == 0), exit_code=code, timed_out=timed_out,
            duration_seconds=time.monotonic() - started,
            boundary="LINUX_SANDBOX" if facts.verified else "UNKNOWN",
            output_tail=_tail((stdout or b"") + (stderr or b"")))


class VerificationHiddenTestRunner:
    """Hidden tests through the Change's confined verification (AppContainer check box)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def run(self, task: EvalTask, tree: Path, *, outcome: AgentOutcome) -> HiddenTestResult:
        if outcome.change_id is None:
            raise RuntimeError("confined verification needs the attempt's Change")
        change_id = UUID(outcome.change_id)
        repository = Path(self._client.get_change(change_id)["repository_path"])
        # The fixture repository holds the applied result; the hidden tests join it
        # only now, after the agent exited and its workspace was applied.
        shutil.copytree(tree / "hidden_tests", repository / "hidden_tests", dirs_exist_ok=True)
        first, *rest = task.hidden_test_command
        started = time.monotonic()
        result = self._client._request(
            "POST", f"/api/v1/changes/{change_id}/verify",
            json_body={"executable": first, "args": rest,
                       "timeout_seconds": task.timeout_seconds})
        verification = result.get("verification") or result
        status = verification.get("status")
        output = (verification.get("stdout") or "") + (verification.get("stderr") or "")
        return HiddenTestResult(
            passed=status == "PASSED", exit_code=verification.get("exit_code"),
            timed_out=status == "TIMED_OUT", duration_seconds=time.monotonic() - started,
            boundary=verification.get("boundary") or "APPCONTAINER",
            output_tail=output[-OUTPUT_TAIL_BYTES:])


class UnconfinedHiddenTestRunner:
    """Hidden tests as a plain host child: only behind an explicit opt-in, labelled UNCONFINED.

    For machines with no confined runner available to the chosen agent (for
    example the mock agent on Windows, which has no Change to verify). Every
    result it produces carries ``boundary = "UNCONFINED"``; it is never chosen
    automatically.
    """

    def run(self, task: EvalTask, tree: Path, *, outcome: AgentOutcome) -> HiddenTestResult:
        from backend.app.execution.hidden_host import run_hidden_tests_unconfined

        argv = resolve_hidden_command(task.hidden_test_command) if os.name != "nt" else [
            os.path.realpath(sys.executable) if part in ("python", "python3") else part
            for part in task.hidden_test_command]
        started = time.monotonic()
        result = run_hidden_tests_unconfined(argv, cwd=tree, timeout=task.timeout_seconds)
        return HiddenTestResult(
            passed=(not result.timed_out and result.returncode == 0),
            exit_code=None if result.timed_out else result.returncode,
            timed_out=result.timed_out, duration_seconds=time.monotonic() - started,
            boundary="UNCONFINED", output_tail=_tail(result.stdout + result.stderr))
