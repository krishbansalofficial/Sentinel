"""The eval runner: for each task and attempt, fixture -> agent -> hidden tests -> record.

Per attempt:

1. The task's fixture is materialized into a fresh Git repository (a copy of
   ``repo/`` committed once, or a clone of the pinned ``git`` source at its sha).
2. Before the agent starts, the runner proves the hidden tests are absent from
   that tree: no file has a hidden test's relative path or its content digest.
   The proof is recorded with the result (``hidden_tests_absent``).
3. The agent runs through an :class:`AgentDriver` (the real pipeline through
   the Sentinel API, or the mock agent).
4. A fresh copy of the resulting tree (no ``.git``) gets ``hidden_tests/`` added
   and the task's ``hidden_test_command`` runs in a confined box through a
   :class:`HiddenTestRunner`. The agent never saw those files.
5. The :class:`AttemptResult` (pass/fail, time, cost and tokens or UNKNOWN,
   descendants, policy decision, refusals, Passport id, boundary) goes to the
   recorder (the eval store / API) as soon as it exists.

Errors inside one attempt become an ``ERROR`` result for that attempt; the run
continues with the next one.
"""

from __future__ import annotations

from backend.app.core.telemetry import traced

import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from backend.app.evals.suite import EvalTask
from backend.app.git.safe_exec import GitIdentity, run_git

EVAL_IDENTITY = GitIdentity(name="Sentinel Eval", email="eval@sentinel.invalid")
DEFAULT_PROMPT_TEMPLATE = "{prompt}"
PASSED, FAILED, ERROR = "PASSED", "FAILED", "ERROR"


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """What is being measured: agent, model, prompt template, tool settings."""

    name: str
    agent: str = "mock"
    model: str | None = None
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE
    settings: tuple[tuple[str, str], ...] = ()

    def render(self, task: EvalTask) -> str:
        return self.prompt_template.replace("{prompt}", task.prompt).replace("{title}", task.title)

    def to_payload(self) -> dict[str, Any]:
        return {"name": self.name, "agent": self.agent, "model": self.model,
                "prompt_template": self.prompt_template, "settings": dict(self.settings)}


@dataclass(frozen=True, slots=True)
class AgentOutcome:
    status: str  # the agent run status as Sentinel recorded it
    wall_seconds: float
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    descendant_count: int | None = None
    policy_decision: str | None = None
    forbidden_refusals: int | None = None
    passport_id: str | None = None
    change_id: str | None = None
    run_id: str | None = None
    boundary: str | None = None
    detail: str | None = None
    actor_id: str | None = None


@dataclass(frozen=True, slots=True)
class HiddenTestResult:
    passed: bool
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    boundary: str  # where the hidden tests ran (LINUX_SANDBOX, APPCONTAINER, UNCONFINED)
    output_tail: str = ""


@dataclass(frozen=True, slots=True)
class AttemptResult:
    run_id: str
    task_id: str
    attempt: int
    status: str  # PASSED (hidden tests passed), FAILED, ERROR
    hidden_tests_absent: bool
    wall_seconds: float
    agent: AgentOutcome | None
    hidden: HiddenTestResult | None
    error: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def passed(self) -> bool:
        return self.status == PASSED

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


class AgentDriver(Protocol):
    def run(self, task: EvalTask, repository: Path, prompt: str, *,
            attempt: int) -> AgentOutcome:
        """Run the agent on ``repository`` (a fresh fixture clone) with ``prompt``."""


class HiddenTestRunner(Protocol):
    def run(self, task: EvalTask, tree: Path, *, outcome: AgentOutcome) -> HiddenTestResult:
        """Run ``task.hidden_test_command`` in a confined box over ``tree`` (hidden tests added)."""


# ---------------------------------------------------------------------- fixtures


def _git(repository: Path, args: list[str], **kwargs) -> None:
    result = run_git(repository, args, **kwargs)
    if result.returncode != 0 or result.timed_out:
        raise RuntimeError(f"git {args[0]} failed: "
                           f"{result.stderr.decode('utf-8', 'replace')[:300]}")


def materialize_fixture(task: EvalTask, destination: Path) -> str:
    """Create the fixture repository at ``destination``; returns its HEAD sha."""
    if task.git is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _git(destination.parent, ["clone", "-q", "--no-hardlinks", task.git.url, str(destination)],
             timeout=600.0)
        _git(destination, ["checkout", "-q", "--detach", task.git.sha])
    else:
        assert task.repo is not None
        shutil.copytree(task.repo, destination, symlinks=False,
                        ignore=shutil.ignore_patterns(".git"))
        _git(destination, ["init", "-q", "-b", "main"])
        _git(destination, ["add", "-A"])
        _git(destination, ["commit", "-q", "-m", f"eval fixture {task.id}"], identity=EVAL_IDENTITY)
    head = run_git(destination, ["rev-parse", "HEAD"])
    return head.stdout.decode("ascii").strip()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hidden_tests_absent(task: EvalTask, tree: Path) -> bool:
    """No file in ``tree`` has a hidden test's relative path or content digest."""
    hidden = task.hidden_files()
    names = {path.as_posix() for path in hidden}
    names |= {(Path("hidden_tests") / path).as_posix() for path in hidden}
    digests = {_digest(task.hidden_tests / path) for path in hidden}
    for path in tree.rglob("*"):
        if ".git" in path.relative_to(tree).parts or not path.is_file():
            continue
        relative = path.relative_to(tree).as_posix()
        if relative in names or _digest(path) in digests:
            return False
    return True


def prepare_hidden_tree(task: EvalTask, result: Path, destination: Path) -> Path:
    """A fresh copy of ``result`` (without ``.git``) plus ``hidden_tests/``."""
    from backend.app.execution.agent_staging import _is_reparse
    if _is_reparse(result) or os.path.lexists(result / "hidden_tests"):
        raise RuntimeError("agent result contains a reserved hidden_tests path or linked root")
    if _is_reparse(task.hidden_tests) or any(_is_reparse(path) for path in task.hidden_tests.rglob("*")):
        raise RuntimeError("hidden tests contain a link or reparse point")
    shutil.copytree(result, destination, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    shutil.copytree(task.hidden_tests, destination / "hidden_tests", symlinks=False,
                    dirs_exist_ok=False)
    return destination


# ---------------------------------------------------------------------- runner


@dataclass
class EvalRun:
    id: str
    suite: str
    config: AgentConfig
    k: int
    started_at: str
    results: list[AttemptResult] = field(default_factory=list)
    completed_at: str | None = None


class EvalRunner:
    def __init__(self, driver: AgentDriver, hidden_runner: HiddenTestRunner, *,
                 recorder: Callable[[EvalRun, AttemptResult], None] | None = None,
                 workdir: Path | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._driver = driver
        self._hidden = hidden_runner
        self._recorder = recorder
        self._workdir = workdir
        self._clock = clock

    @traced("eval.run")
    def run(self, tasks: Iterable[EvalTask], *, config: AgentConfig, k: int, suite: str,
            run_id: str | None = None) -> EvalRun:
        if k < 1:
            raise ValueError("k must be at least 1")
        run = EvalRun(id=run_id or str(uuid4()), suite=suite, config=config, k=k,
                      started_at=datetime.now(UTC).isoformat())
        for task in tasks:
            for attempt in range(1, k + 1):
                result = self.run_attempt(run, task, attempt)
                run.results.append(result)
                if self._recorder is not None:
                    self._recorder(run, result)
        run.completed_at = datetime.now(UTC).isoformat()
        return run

    @traced("eval.job")
    def run_attempt(self, run: EvalRun, task: EvalTask, attempt: int) -> AttemptResult:
        started = self._clock()
        with tempfile.TemporaryDirectory(prefix=f"sentinel-eval-{task.id}-",
                                         dir=self._workdir) as directory:
            root = Path(directory)
            outcome: AgentOutcome | None = None
            absent = False
            try:
                repository = root / "repo"
                materialize_fixture(task, repository)
                absent = hidden_tests_absent(task, repository)
                if not absent:
                    raise RuntimeError("hidden tests are present in the agent's tree")
                outcome = self._driver.run(task, repository, run.config.render(task),
                                           attempt=attempt)
                if outcome.status != "PASSED":
                    raise RuntimeError(f"agent did not finish successfully ({outcome.status})")
                if (task.budget_usd is not None and outcome.cost_usd is not None
                        and outcome.cost_usd > task.budget_usd):
                    raise RuntimeError("agent exceeded the task cost budget")
                hidden = self._hidden.run(
                    task, prepare_hidden_tree(task, repository, root / "hidden-run"),
                    outcome=outcome)
            except Exception as exc:  # one broken attempt never stops the run
                return AttemptResult(run.id, task.id, attempt, ERROR, absent,
                                     self._clock() - started, outcome, None,
                                     error=f"{type(exc).__name__}: {exc}"[:500])
        return AttemptResult(run.id, task.id, attempt, PASSED if hidden.passed else FAILED,
                             absent, self._clock() - started, outcome, hidden)


def run_uuid(value: str) -> UUID:
    return UUID(value)
