"""The eval runner on generated mock tasks with the mock agent (no model, no network).

Hidden tests here run as a plain host child (``HostHiddenTestRunner``), clearly
labelled UNCONFINED: this proves the runner's logic, not containment. The real
sandboxed hidden-test runner is exercised in tests/execution/linux.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.app.evals.agents import MockAgentDriver, parse_claude_json
from backend.app.evals.runner import (
    ERROR,
    AgentConfig,
    AgentOutcome,
    EvalRunner,
    HiddenTestResult,
    hidden_tests_absent,
    materialize_fixture,
)
from backend.app.evals.stats import is_regression, paired_bootstrap
from backend.app.evals.suite import TaskSuiteError, load_suite, load_task


class HostHiddenTestRunner:
    """Test-only: hidden tests as a host child process (no boundary; labelled so)."""

    def __init__(self) -> None:
        self.seen_trees: list[list[str]] = []

    def run(self, task, tree: Path, *, outcome: AgentOutcome) -> HiddenTestResult:
        self.seen_trees.append(sorted(p.relative_to(tree).as_posix()
                                      for p in tree.rglob("*") if p.is_file()))
        command = [sys.executable if part in ("python", "python3") else part
                   for part in task.hidden_test_command]
        started = time.monotonic()
        result = subprocess.run(command, cwd=tree, capture_output=True, timeout=120)
        return HiddenTestResult(result.returncode == 0, result.returncode, False,
                                time.monotonic() - started, "UNCONFINED",
                                (result.stdout + result.stderr)[-2000:].decode("utf-8", "replace"))


def make_task(root: Path, task_id: str, *, a: int, b: int) -> Path:
    """A tiny Python bug-fix task: add() is wrong; hidden tests check it; solution fixes it."""
    directory = root / task_id
    (directory / "repo").mkdir(parents=True)
    (directory / "repo" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (directory / "hidden_tests").mkdir()
    (directory / "hidden_tests" / "check_add.py").write_text(
        "import sys\nsys.path.insert(0, '.')\nfrom calc import add\n"
        f"assert add({a}, {b}) == {a + b}, add({a}, {b})\nprint('ok')\n")
    (directory / "solution").mkdir()
    (directory / "solution" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (directory / "task.toml").write_text(
        f'id = "{task_id}"\ntitle = "Fix add"\nprompt = "add() in calc.py must return a + b; '
        f'task {task_id}."\ntimeout_seconds = 60\n'
        'hidden_test_command = ["python", "hidden_tests/check_add.py"]\ntags = ["python"]\n')
    return directory


@pytest.fixture
def suite(tmp_path: Path) -> Path:
    root = tmp_path / "suite"
    for index in range(12):
        make_task(root, f"add-{index:02d}", a=index, b=index * 3 + 1)
    return root


def _rates(run) -> dict[str, float]:
    by_task: dict[str, list[bool]] = {}
    for result in run.results:
        by_task.setdefault(result.task_id, []).append(result.passed)
    return {task: sum(values) / len(values) for task, values in by_task.items()}


def test_every_mock_task_fails_unfixed_and_passes_with_its_solution(suite: Path, tmp_path) -> None:
    hidden = HostHiddenTestRunner()
    for task in load_suite(suite):
        broken = tmp_path / f"broken-{task.id}"
        materialize_fixture(task, broken)
        assert hidden_tests_absent(task, broken)
        from backend.app.evals.runner import prepare_hidden_tree
        assert not hidden.run(task, prepare_hidden_tree(task, broken, tmp_path / f"b-{task.id}"),
                              outcome=None).passed
        import shutil
        shutil.copytree(task.directory / "solution", broken, dirs_exist_ok=True)
        assert hidden.run(task, prepare_hidden_tree(task, broken, tmp_path / f"s-{task.id}"),
                          outcome=None).passed


def test_a_run_records_k_attempts_per_task_and_hidden_tests_stay_hidden(suite: Path) -> None:
    hidden = HostHiddenTestRunner()
    recorded = []
    runner = EvalRunner(MockAgentDriver(skill=0.8, seed=1), hidden,
                        recorder=lambda run, result: recorded.append(result))
    run = runner.run(load_suite(suite), config=AgentConfig("mock-good"), k=3, suite=str(suite))
    assert len(run.results) == 36 and recorded == run.results
    assert all(result.hidden_tests_absent for result in run.results)
    assert {result.status for result in run.results} <= {"PASSED", "FAILED"}
    assert all(result.agent.cost_usd and result.agent.input_tokens for result in run.results)
    # The hidden tests appear only in the post-agent tree, alongside the agent's result.
    assert all("hidden_tests/check_add.py" in tree for tree in hidden.seen_trees)
    passes = sum(result.passed for result in run.results)
    assert 18 <= passes <= 36  # skill 0.8 over 36 seeded attempts


def test_the_run_is_reproducible_with_the_same_seed(suite: Path) -> None:
    outcomes = []
    for _ in range(2):
        run = EvalRunner(MockAgentDriver(skill=0.6, seed=42), HostHiddenTestRunner()).run(
            load_suite(suite), config=AgentConfig("mock"), k=2, suite="s")
        outcomes.append([(r.task_id, r.attempt, r.status) for r in run.results])
    assert outcomes[0] == outcomes[1]


def test_a_broken_prompt_is_detected_and_a_rerun_is_not(suite: Path) -> None:
    tasks = load_suite(suite)
    good = AgentConfig("good", prompt_template="{prompt}")
    broken = AgentConfig("broken", prompt_template="Please help with this repository.")
    run_a = EvalRunner(MockAgentDriver(skill=0.9, seed=1), HostHiddenTestRunner()).run(
        tasks, config=good, k=3, suite="s")
    run_b = EvalRunner(MockAgentDriver(skill=0.9, seed=2), HostHiddenTestRunner()).run(
        tasks, config=good, k=3, suite="s")
    run_c = EvalRunner(MockAgentDriver(skill=0.9, seed=3), HostHiddenTestRunner()).run(
        tasks, config=broken, k=3, suite="s")

    def totals(run):
        return (sum(r.passed for r in run.results), len(run.results))

    same = paired_bootstrap(_rates(run_a), _rates(run_b), resamples=4000)
    assert not is_regression(totals(run_a), totals(run_b), same)
    injected = paired_bootstrap(_rates(run_a), _rates(run_c), resamples=4000)
    assert totals(run_c)[0] == 0
    assert is_regression(totals(run_a), totals(run_c), injected)


def test_a_failing_attempt_becomes_an_error_result_and_the_run_continues(suite: Path) -> None:
    class ExplodingDriver:
        def run(self, task, repository, prompt, *, attempt):
            if attempt == 1:
                raise RuntimeError("agent crashed")
            return MockAgentDriver(skill=1.0).run(task, repository, prompt, attempt=attempt)

    run = EvalRunner(ExplodingDriver(), HostHiddenTestRunner()).run(
        load_suite(suite, only={"add-00", "add-01"}), config=AgentConfig("x"), k=2, suite="s")
    statuses = [(r.task_id, r.attempt, r.status) for r in run.results]
    assert statuses == [("add-00", 1, ERROR), ("add-00", 2, "PASSED"),
                        ("add-01", 1, ERROR), ("add-01", 2, "PASSED")]
    assert "agent crashed" in run.results[0].error


def test_a_fixture_that_leaks_a_hidden_test_is_refused(tmp_path: Path) -> None:
    directory = make_task(tmp_path / "suite", "leaky", a=1, b=2)
    leaked = directory / "repo" / "copied.py"
    leaked.write_bytes((directory / "hidden_tests" / "check_add.py").read_bytes())
    run = EvalRunner(MockAgentDriver(skill=1.0), HostHiddenTestRunner()).run(
        [load_task(directory)], config=AgentConfig("x"), k=1, suite="s")
    assert run.results[0].status == ERROR and not run.results[0].hidden_tests_absent
    assert "hidden tests are present" in run.results[0].error


@pytest.mark.parametrize(("edit", "match"), [
    (lambda d: (d / "task.toml").write_text((d / "task.toml").read_text() + 'surprise = 1\n'),
     "unknown keys"),
    (lambda d: (d / "task.toml").write_text((d / "task.toml").read_text().replace(
        'id = "t"', 'id = "other"')), "directory name"),
    (lambda d: [p.unlink() for p in (d / "hidden_tests").iterdir()], "missing or empty"),
    (lambda d: (d / "task.toml").write_text((d / "task.toml").read_text().replace(
        "timeout_seconds = 60", "timeout_seconds = 0")), "timeout_seconds"),
    (lambda d: (d / "task.toml").write_text((d / "task.toml").read_text() +
                                            'git = { url = "x", sha = "abc" }\n'), "40-hex sha"),
    (lambda d: (d / "task.toml").write_text((d / "task.toml").read_text() + 'repo = "repo"\n'
                                            'git = { url = "x", sha = "' + "a" * 40 + '" }\n'),
     "either repo or git"),
])
def test_malformed_tasks_are_refused(tmp_path: Path, edit, match) -> None:
    directory = make_task(tmp_path / "suite", "t", a=1, b=2)
    edit(directory)
    with pytest.raises(TaskSuiteError, match=match):
        load_task(directory)


@pytest.mark.parametrize(("stdout", "expected"), [
    ('{"type":"result","total_cost_usd":0.0123,"usage":{"input_tokens":100,'
     '"cache_read_input_tokens":50,"output_tokens":20}}', (0.0123, 150, 20)),
    ('{"type":"system"}\n{"type":"result","total_cost_usd":1,"usage":{"input_tokens":3,'
     '"output_tokens":4}}', (1.0, 3, 4)),
    ("not json at all", (None, None, None)),
    ("", (None, None, None)),
    ('{"type":"result"}', (None, None, None)),
    ('{"type":"result","total_cost_usd":true}', (None, None, None)),
])
def test_claude_json_costs_and_tokens_or_unknown(stdout, expected) -> None:
    assert parse_claude_json(stdout) == expected



@pytest.mark.parametrize("agent_status,cost", [("FAILED", None), ("TIMED_OUT", None), ("UNKNOWN", None), ("PASSED", 1.0)])
def test_unsuccessful_or_over_budget_agent_cannot_pass_hidden_tests(tmp_path, agent_status, cost):
    from dataclasses import replace
    directory = make_task(tmp_path / "suite", "budget", a=1, b=2)
    task = replace(load_task(directory), budget_usd=0.25)
    class Driver:
        def run(self, *_args, **_kwargs): return AgentOutcome(agent_status, 1.0, cost_usd=cost)
    class Hidden:
        def run(self, *_args, **_kwargs): pytest.fail("hidden tests must not run after failed agent or budget breach")
    result = EvalRunner(Driver(), Hidden()).run([task], config=AgentConfig("x"), k=1, suite="s").results[0]
    assert result.status == ERROR
    assert result.hidden is None
