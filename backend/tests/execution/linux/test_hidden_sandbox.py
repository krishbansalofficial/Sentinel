"""``SandboxHiddenTestRunner`` and the eval runner against a real kernel.

Hidden tests run in the verified Linux sandbox: a passing check reports PASSED
under ``LINUX_SANDBOX``, a failing one FAILED, and the check can neither reach
the network nor read files beside its tree. The secret is generated mock data.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from backend.app.evals.agents import MockAgentDriver
from backend.app.evals.hidden import SandboxHiddenTestRunner
from backend.app.evals.runner import AgentConfig, AgentOutcome, EvalRunner
from backend.app.evals.suite import load_suite
from backend.tests.execution.linux.conftest import requires_linux_sandbox

pytestmark = requires_linux_sandbox
SEED_SUITE = Path(__file__).resolve().parents[4] / "evals" / "tasks"


class _Task:
    def __init__(self, timeout: int = 60) -> None:
        self.hidden_test_command = ("python", "hidden_tests/check.py")
        self.timeout_seconds = timeout


def _tree(tmp_path: Path, check: str) -> Path:
    tree = tmp_path / "tree"
    (tree / "hidden_tests").mkdir(parents=True)
    (tree / "hidden_tests" / "check.py").write_text(check, encoding="utf-8")
    return tree


def test_a_passing_hidden_check_passes_under_the_verified_sandbox(tmp_path, hierarchy_factory):
    tree = _tree(tmp_path, "print('hidden ok')\n")
    result = SandboxHiddenTestRunner(hierarchy_factory).run(
        _Task(), tree, outcome=AgentOutcome("PASSED", 0.0))
    assert result.passed and result.exit_code == 0, result.output_tail
    assert result.boundary == "LINUX_SANDBOX"
    assert "hidden ok" in result.output_tail


def test_a_failing_hidden_check_fails(tmp_path, hierarchy_factory):
    tree = _tree(tmp_path, "raise SystemExit(4)\n")
    result = SandboxHiddenTestRunner(hierarchy_factory).run(
        _Task(), tree, outcome=AgentOutcome("PASSED", 0.0))
    assert not result.passed and result.exit_code == 4
    assert result.boundary == "LINUX_SANDBOX"


def test_a_hidden_check_cannot_read_beside_its_tree_or_reach_the_network(tmp_path,
                                                                          hierarchy_factory):
    secret = tmp_path / "operator-secret.txt"
    token = secrets.token_hex(16)
    secret.write_text(token, encoding="utf-8")
    assert secret.read_text(encoding="utf-8") == token  # host positive control
    tree = _tree(tmp_path, (
        "import socket, sys\n"
        "try:\n"
        f"    print('LEAK', open({str(secret)!r}).read())\n"
        "except OSError:\n"
        "    print('read refused')\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
        "    print('NET OPEN')\n"
        "except OSError:\n"
        "    print('net refused')\n"))
    result = SandboxHiddenTestRunner(hierarchy_factory).run(
        _Task(), tree, outcome=AgentOutcome("PASSED", 0.0))
    assert result.passed, result.output_tail
    assert "read refused" in result.output_tail and "net refused" in result.output_tail
    assert token not in result.output_tail and "NET OPEN" not in result.output_tail


def test_a_hidden_check_is_killed_at_the_task_timeout(tmp_path, hierarchy_factory):
    tree = _tree(tmp_path, "import time\ntime.sleep(60)\n")
    result = SandboxHiddenTestRunner(hierarchy_factory).run(
        _Task(timeout=1), tree, outcome=AgentOutcome("PASSED", 0.0))
    assert result.timed_out and not result.passed and result.exit_code is None


def test_the_eval_runner_scores_seed_tasks_with_sandboxed_hidden_tests(tmp_path,
                                                                       hierarchy_factory):
    """Mock agent at skill 1 solves every task; a template without {prompt} solves none."""
    tasks = load_suite(SEED_SUITE, only={"op-add", "clamp-0", "str-reverse"})
    runner = EvalRunner(MockAgentDriver(skill=1.0), SandboxHiddenTestRunner(hierarchy_factory),
                        workdir=tmp_path)
    good = runner.run(tasks, config=AgentConfig("good"), k=1, suite="seed")
    assert [result.status for result in good.results] == ["PASSED"] * 3, [
        (result.task_id, result.error, result.hidden and result.hidden.output_tail)
        for result in good.results]
    assert {result.hidden.boundary for result in good.results} == {"LINUX_SANDBOX"}
    assert all(result.hidden_tests_absent for result in good.results)
    broken = runner.run(tasks, config=AgentConfig("broken", prompt_template="{title}"), k=1,
                        suite="seed")
    assert [result.status for result in broken.results] == ["FAILED"] * 3
