"""Eval task suites: ``evals/tasks/<task_id>/{task.toml, repo/, hidden_tests/}``.

``task.toml`` (strictly validated; unknown keys are refused so a typo cannot
silently drop a field)::

    id = "py-fix-off-by-one"          # must equal the directory name
    title = "Fix the off-by-one in paginate()"
    prompt = "..."                    # what the agent is asked to do
    timeout_seconds = 600
    budget_usd = 0.50                 # optional; Claude receives a cap, runner rejects known overruns
    allowed_paths = ["src/**"]        # optional Change Contract allowed paths
    required_checks = ["pytest"]      # optional
    hidden_test_command = ["python", "-m", "pytest", "-q", "hidden_tests"]
    tags = ["python", "bugfix"]
    # either a fixture directory (default "repo") or a pinned git source:
    # git = { url = "https://...", sha = "<40 hex>" }

``hidden_tests/`` is never copied into the agent's tree; the runner adds it to a
fresh copy of the result only after the agent has exited.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TASK_FILE = "task.toml"
REPO_DIRECTORY = "repo"
HIDDEN_DIRECTORY = "hidden_tests"
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_KNOWN_KEYS = {"id", "title", "prompt", "timeout_seconds", "budget_usd", "allowed_paths",
               "required_checks", "hidden_test_command", "tags", "repo", "git"}
MAX_TIMEOUT_SECONDS = 86_400


class TaskSuiteError(ValueError):
    """A task definition is missing, malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class GitSource:
    url: str
    sha: str


@dataclass(frozen=True, slots=True)
class EvalTask:
    id: str
    title: str
    prompt: str
    directory: Path
    hidden_test_command: tuple[str, ...]
    timeout_seconds: int = 600
    budget_usd: float | None = None
    allowed_paths: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    repo: Path | None = None
    git: GitSource | None = None
    hidden_tests: Path = field(default=Path())

    def hidden_files(self) -> list[Path]:
        """Every hidden test file, relative to ``hidden_tests/``."""
        return sorted(path.relative_to(self.hidden_tests)
                      for path in self.hidden_tests.rglob("*") if path.is_file())


def _strings(value: Any, name: str, task_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise TaskSuiteError(f"{task_id}: {name} must be a list of non-empty strings")
    return tuple(value)


def load_task(directory: Path) -> EvalTask:
    directory = Path(directory)
    path = directory / TASK_FILE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TaskSuiteError(f"{directory.name}: {TASK_FILE} is missing") from exc
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise TaskSuiteError(f"{directory.name}: {TASK_FILE} is not valid TOML ({exc})") from exc
    unknown = set(data) - _KNOWN_KEYS
    if unknown:
        raise TaskSuiteError(f"{directory.name}: unknown keys {sorted(unknown)}")
    task_id = data.get("id")
    if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
        raise TaskSuiteError(f"{directory.name}: id must match {_ID.pattern}")
    if task_id != directory.name:
        raise TaskSuiteError(f"{directory.name}: id {task_id!r} must equal the directory name")
    for key in ("title", "prompt"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise TaskSuiteError(f"{task_id}: {key} is required")
    timeout = data.get("timeout_seconds", 600)
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise TaskSuiteError(f"{task_id}: timeout_seconds must be 1..{MAX_TIMEOUT_SECONDS}")
    budget = data.get("budget_usd")
    if budget is not None and (not isinstance(budget, (int, float)) or isinstance(budget, bool)
                               or budget < 0):
        raise TaskSuiteError(f"{task_id}: budget_usd must be a non-negative number")
    command = _strings(data.get("hidden_test_command"), "hidden_test_command", task_id)
    git = None
    repo = None
    if "git" in data:
        if "repo" in data:
            raise TaskSuiteError(f"{task_id}: give either repo or git, not both")
        source = data["git"]
        if (not isinstance(source, dict) or set(source) != {"url", "sha"}
                or not isinstance(source["url"], str) or not source["url"]
                or not isinstance(source["sha"], str) or not _SHA.fullmatch(source["sha"])):
            raise TaskSuiteError(f"{task_id}: git must be {{url, sha}} with a full 40-hex sha")
        git = GitSource(source["url"], source["sha"])
    else:
        name = data.get("repo", REPO_DIRECTORY)
        if not isinstance(name, str) or "/" in name or "\\" in name or name in ("", ".", ".."):
            raise TaskSuiteError(f"{task_id}: repo must name a subdirectory")
        repo = directory / name
        if not repo.is_dir() or repo.is_symlink():
            raise TaskSuiteError(f"{task_id}: fixture directory {name}/ is missing")
    hidden = directory / HIDDEN_DIRECTORY
    if not hidden.is_dir() or hidden.is_symlink() or not any(p.is_file() for p in hidden.rglob("*")):
        raise TaskSuiteError(f"{task_id}: {HIDDEN_DIRECTORY}/ is missing or empty")
    if repo is not None and (repo == hidden or hidden in repo.parents or repo in hidden.parents):
        raise TaskSuiteError(f"{task_id}: hidden tests must live outside the fixture")
    for link in [*directory.rglob("*")]:
        if link.is_symlink():
            raise TaskSuiteError(f"{task_id}: links are not allowed in a task ({link.name})")
    return EvalTask(
        id=task_id, title=data["title"].strip(), prompt=data["prompt"].strip(),
        directory=directory, hidden_test_command=command, timeout_seconds=timeout,
        budget_usd=None if budget is None else float(budget),
        allowed_paths=_strings(data.get("allowed_paths", []), "allowed_paths", task_id)
        if data.get("allowed_paths") else (),
        required_checks=_strings(data.get("required_checks", []), "required_checks", task_id)
        if data.get("required_checks") else (),
        tags=_strings(data.get("tags", []), "tags", task_id) if data.get("tags") else (),
        repo=repo, git=git, hidden_tests=hidden,
    )


def load_suite(root: Path, *, only: set[str] | None = None) -> list[EvalTask]:
    """Every task under ``root`` (sorted by id); ``only`` restricts to those ids."""
    root = Path(root)
    if not root.is_dir():
        raise TaskSuiteError(f"suite {root} does not exist")
    tasks = [load_task(entry) for entry in sorted(root.iterdir())
             if entry.is_dir() and (entry / TASK_FILE).exists()]
    if only is not None:
        missing = only - {task.id for task in tasks}
        if missing:
            raise TaskSuiteError(f"unknown task ids {sorted(missing)}")
        tasks = [task for task in tasks if task.id in only]
    if not tasks:
        raise TaskSuiteError(f"suite {root} has no tasks")
    return tasks
