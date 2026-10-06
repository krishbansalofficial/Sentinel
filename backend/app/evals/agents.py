"""Agent drivers for ``sentinel eval``: the real pipeline, and a mock for offline runs.

* :class:`ApiAgentDriver` runs the agent exactly as ``sentinel run`` does,
  through the Sentinel API: a Change on the fixture repository, an agent actor
  with a ``agent.launch`` delegation, baseline, launch, preview, apply-back of a
  refusal-free preview (the eval harness is the operator and confirms), current
  evidence, and a Passport v2. Cost and tokens come from the Claude Code JSON
  result when the adapter printed one (``claude -p ... --output-format json``);
  otherwise they stay ``None`` and are reported as UNKNOWN.
* :class:`MockAgentDriver` needs no model or network. It applies the task's
  reference ``solution/`` with a seeded probability, and only when the prompt it
  was given actually contains the task's instructions: a broken prompt template
  that drops ``{prompt}`` gives it nothing to act on, as it would a real agent.
  Cost and tokens are synthetic and labelled as such.
"""

from __future__ import annotations

import json
import random
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from backend.app.evals.runner import AgentOutcome
from backend.app.evals.suite import EvalTask

SOLUTION_DIRECTORY = "solution"


def parse_claude_json(stdout: str) -> tuple[float | None, int | None, int | None]:
    """(cost_usd, input_tokens, output_tokens) from Claude Code's JSON result, else Nones.

    Accepts the single JSON object of ``--output-format json`` or the last
    ``{"type": "result", ...}`` line of ``stream-json``. Missing fields stay None.
    """
    candidates: list[Any] = []
    text = stdout.strip()
    if not text:
        return None, None, None
    try:
        candidates.append(json.loads(text))
    except ValueError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    candidates.append(json.loads(line))
                except ValueError:
                    continue
    for item in candidates:
        if not isinstance(item, dict) or item.get("type", "result") != "result":
            continue
        cost = item.get("total_cost_usd", item.get("cost_usd"))
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if isinstance(input_tokens, int):
            for extra in ("cache_creation_input_tokens", "cache_read_input_tokens"):
                if isinstance(usage.get(extra), int):
                    input_tokens += usage[extra]
        return (float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                else None,
                input_tokens if isinstance(input_tokens, int) else None,
                output_tokens if isinstance(output_tokens, int) else None)
    return None, None, None


class MockAgentDriver:
    """A seeded stand-in agent that solves a task only if the prompt says what it is."""

    def __init__(self, *, skill: float = 0.85, seed: int = 0,
                 sleep: Callable[[float], None] = lambda _seconds: None) -> None:
        if not 0.0 <= skill <= 1.0:
            raise ValueError("skill must be in [0, 1]")
        self._skill = skill
        self._seed = seed
        self._sleep = sleep

    def run(self, task: EvalTask, repository: Path, prompt: str, *, attempt: int) -> AgentOutcome:
        started = time.monotonic()
        generator = random.Random(f"{self._seed}:{task.id}:{attempt}")
        understood = task.prompt in prompt
        solution = task.directory / SOLUTION_DIRECTORY
        solved = understood and solution.is_dir() and generator.random() < self._skill
        if solved:
            shutil.copytree(solution, repository, dirs_exist_ok=True)
        elif understood:
            # A plausible but wrong attempt: touch a file without fixing anything.
            (repository / "NOTES.md").write_text(f"attempted {task.id}\n", encoding="utf-8")
        self._sleep(generator.uniform(0.0, 0.01))
        input_tokens = len(prompt) // 4 + generator.randrange(800, 1600)
        output_tokens = generator.randrange(100, 900)
        return AgentOutcome(
            status="PASSED", wall_seconds=time.monotonic() - started,
            cost_usd=round((input_tokens * 3 + output_tokens * 15) / 1_000_000, 6),
            input_tokens=input_tokens, output_tokens=output_tokens, descendant_count=0,
            policy_decision=None, forbidden_refusals=0, boundary="UNCONFINED",
            detail="mock agent (synthetic cost and tokens)",
        )


class ApiAgentDriver:
    """The real pipeline: the same steps ``sentinel run`` takes, through the API."""

    def __init__(self, client: Any, *, adapter: str, executable: str,
                 args_for: Callable[[str], list[str]], timeout_seconds: int | None = None,
                 environment_keys: list[str] | None = None, issue_passport: bool = True) -> None:
        self._client = client
        self._adapter = adapter
        self._executable = executable
        self._args_for = args_for
        self._timeout = timeout_seconds
        self._environment_keys = environment_keys or []
        self._issue_passport = issue_passport

    def run(self, task: EvalTask, repository: Path, prompt: str, *, attempt: int) -> AgentOutcome:
        from backend.app.cli.run_flow import RunFlow, RunOptions

        started = time.monotonic()
        change = self._client.create_change(f"eval {task.id} #{attempt}", task.prompt,
                                            str(repository))
        change_id = UUID(change["id"])
        human = self._client.create_actor("HUMAN", "Sentinel eval operator")
        agent = self._client.create_actor("AGENT", f"eval {self._adapter}")
        self._client.create_delegation(
            grantor_id=UUID(human["id"]), grantee_id=UUID(agent["id"]), change_id=change_id,
            scopes=["agent.launch"], ttl_seconds=max(600, (self._timeout or task.timeout_seconds) * 2))
        report = RunFlow(self._client, confirm=lambda _preview: True).run(
            change_id, UUID(agent["id"]), RunOptions(
                executable=self._executable, args=self._args_for(prompt), adapter=self._adapter,
                environment_keys=self._environment_keys,
                timeout_seconds=self._timeout or task.timeout_seconds, apply=True,
                issue_passport=self._issue_passport))
        steps = {step.name: step for step in report.steps}
        launch = steps.get("launch")
        run_id = launch.detail.get("run_id") if launch else None
        runs = self._client.list_agent_runs(change_id) if run_id else {"items": []}
        record = next((item for item in runs.get("items", []) if item.get("id") == run_id), {})
        cost, input_tokens, output_tokens = parse_claude_json(record.get("stdout") or "")
        preview = steps.get("preview")
        # A preview refuses with one reason (forbidden paths and other apply-back refusals);
        # no preview (the adapter ran in the repository) leaves the count UNKNOWN.
        refusals = (int(bool(preview.detail.get("refusal_reason")))
                    if preview is not None and preview.status != "skipped" else None)
        passport = steps.get("passport")
        return AgentOutcome(
            status=record.get("status") or (launch.detail.get("agent_status") if launch else "ERROR"),
            wall_seconds=time.monotonic() - started, cost_usd=cost, input_tokens=input_tokens,
            output_tokens=output_tokens,
            descendant_count=len(record.get("descendant_processes") or []) if record else None,
            policy_decision=(passport.detail.get("policy_decision") if passport else None),
            forbidden_refusals=refusals,
            passport_id=(passport.detail.get("payload_digest") if passport else None),
            change_id=str(change_id), run_id=run_id,
            boundary=(passport.detail.get("execution_boundary") if passport else None),
            detail=f"run outcome {report.outcome}",
        )
