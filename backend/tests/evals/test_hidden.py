import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.evals import hidden as hidden_module
from backend.app.evals.hidden import (
    MAX_VERIFICATION_TIMEOUT_SECONDS,
    SandboxHiddenTestRunner,
    VerificationHiddenTestRunner,
)
from backend.app.evals.runner import AgentConfig, AgentOutcome, hidden_tests_absent
from backend.app.evals.suite import load_task
from backend.app.execution.linux_sandbox import LinuxSandboxProcess


class VerifyClient:
    """The slice of ApiClient the Windows hidden runner uses, recording each call."""

    def __init__(self, repository: Path, change_id, verification: dict) -> None:
        self.repository = repository
        self.change_id = change_id
        self.verification = verification
        self.actors: list[tuple[str, str]] = []
        self.delegations: list[dict] = []
        self.requests: list[tuple[str, str, dict]] = []

    def get_change(self, identifier):
        assert identifier == self.change_id
        return {"repository_path": str(self.repository)}

    def create_actor(self, kind, display_name):
        self.actors.append((kind, display_name))
        return {"id": str(uuid4())}

    def create_delegation(self, **kwargs):
        self.delegations.append(kwargs)
        return {"id": str(uuid4())}

    def _request(self, method, path, *, json_body):
        assert (self.repository / "hidden_tests" / "check.py").exists()
        self.requests.append((method, path, json_body))
        return {"id": str(self.change_id), "verification": self.verification}


def _verify_setup(tmp_path: Path, verification: dict):
    source, repository = tmp_path / "result", tmp_path / "repository"
    (source / "hidden_tests").mkdir(parents=True)
    (source / "hidden_tests" / "check.py").write_text("assert True")
    repository.mkdir()
    change_id = uuid4()
    return source, VerifyClient(repository, change_id, verification), change_id


@pytest.mark.parametrize("boundary", [None, "APPCONTAINER"])
def test_windows_hidden_runner_never_invents_boundary(tmp_path, boundary):
    source, client, change_id = _verify_setup(
        tmp_path, {"status": "PASSED", "exit_code": 0, "boundary": boundary})
    result = VerificationHiddenTestRunner(client).run(
        SimpleNamespace(hidden_test_command=("python", "hidden_tests/check.py"), timeout_seconds=10),
        source, outcome=SimpleNamespace(change_id=str(change_id), actor_id="operator"))
    assert result.passed is (boundary == "APPCONTAINER")
    assert result.boundary == (boundary or "UNKNOWN")


def test_windows_hidden_runner_sends_the_contract_request_with_a_verify_delegation(tmp_path):
    """The route takes VerificationActionRequest: an actor plus the nested verification."""
    source, client, change_id = _verify_setup(
        tmp_path, {"status": "FAILED", "exit_code": 1, "boundary": "APPCONTAINER",
                   "stdout": "boom", "stderr": ""})
    result = VerificationHiddenTestRunner(client).run(
        SimpleNamespace(hidden_test_command=("python", "hidden_tests/check.py"),
                        timeout_seconds=3600),
        source, outcome=SimpleNamespace(change_id=str(change_id)))
    [(method, path, body)] = client.requests
    assert method == "POST" and path == f"/api/v1/changes/{change_id}/verify"
    [delegation] = client.delegations
    assert delegation["scopes"] == ["change.legacy_verify"]
    assert delegation["change_id"] == change_id
    assert str(delegation["grantee_id"]) == body["actor_id"]
    # The contract caps verification timeouts; a longer task timeout is clamped, not refused.
    assert body["verification"] == {"executable": "python", "args": ["hidden_tests/check.py"],
                                    "timeout_seconds": MAX_VERIFICATION_TIMEOUT_SECONDS}
    assert not result.passed and result.exit_code == 1 and result.output_tail == "boom"


def test_windows_hidden_runner_request_validates_against_the_contract(tmp_path):
    from backend.app.contracts.models import VerificationActionRequest

    source, client, change_id = _verify_setup(tmp_path, {"status": "PASSED", "exit_code": 0})
    VerificationHiddenTestRunner(client).run(
        SimpleNamespace(hidden_test_command=("python", "hidden_tests/check.py"), timeout_seconds=60),
        source, outcome=SimpleNamespace(change_id=str(change_id)))
    VerificationActionRequest.model_validate(client.requests[0][2])


def test_verification_hidden_runner_requires_change(tmp_path):
    with pytest.raises(RuntimeError, match="Change"):
        VerificationHiddenTestRunner(None).run(None, tmp_path, outcome=SimpleNamespace(change_id=None))


@pytest.mark.parametrize("reserved", ["directory", "link"])
def test_hidden_injection_refuses_agent_owned_destination(tmp_path, reserved):
    from backend.app.evals.runner import prepare_hidden_tree
    source = tmp_path / "source"
    source.mkdir()
    hidden = tmp_path / "private-tests"
    hidden.mkdir()
    (hidden / "test.py").write_text("assert True")
    outside = tmp_path / "outside"
    outside.mkdir()
    if reserved == "link":
        (source / "hidden_tests").symlink_to(outside, target_is_directory=True)
    else:
        (source / "hidden_tests").mkdir()
    with pytest.raises(RuntimeError, match="reserved"):
        prepare_hidden_tree(SimpleNamespace(hidden_tests=hidden), source, tmp_path / "fresh")
    assert not list(outside.iterdir())
    assert not (tmp_path / "fresh").exists()


def test_windows_hidden_runner_refuses_existing_agent_hidden_directory(tmp_path):
    repository = tmp_path / "repo"
    (repository / "hidden_tests").mkdir(parents=True)
    class Client:
        def get_change(self, _): return {"repository_path": str(repository)}
        def _request(self, *_args, **_kwargs): pytest.fail("must refuse before API execution")
    with pytest.raises(RuntimeError, match="reserved"):
        VerificationHiddenTestRunner(Client()).run(None, tmp_path, outcome=SimpleNamespace(change_id=str(uuid4()), actor_id="operator"))


@pytest.mark.parametrize("failure", ["spawn", "timeout", "flood", "unverified", "success"])
def test_linux_hidden_runner_bounds_output_and_cleans_up(tmp_path, monkeypatch, failure):
    from backend.app.evals.hidden import SandboxHiddenTestRunner, OUTPUT_TAIL_BYTES
    from backend.app.execution import linux_sandbox, _process
    from backend.app.execution._process import CapturedProcess
    calls = []
    class Group:
        def remove(self): calls.append("remove")
    class Hierarchy:
        def prepare(self): calls.append("prepare")
        def create_run(self, *_): return Group()
    class Process:
        _popen = object()
        linux_sandbox = SimpleNamespace(verified=failure != "unverified")
        def close(self): calls.append("close")
    def spawn(*args, **kwargs):
        if failure == "spawn": raise RuntimeError("boundary unavailable")
        assert args[0].network is False
        return Process()
    def capture(*args, **kwargs):
        assert kwargs["limit"] == OUTPUT_TAIL_BYTES
        assert kwargs["timeout"] == 10
        assert kwargs["process_factory"](None) is Process._popen
        return CapturedProcess(0, b"ok", b"", failure == "flood", failure == "timeout", False, "a" * 64)
    monkeypatch.setattr(linux_sandbox, "spawn_linux_sandbox", spawn)
    monkeypatch.setattr(_process, "capture", capture)
    runner = SandboxHiddenTestRunner(Hierarchy)
    task = SimpleNamespace(hidden_test_command=("python", "hidden_tests/check.py"), timeout_seconds=10)
    if failure == "spawn":
        with pytest.raises(RuntimeError, match="boundary unavailable"):
            runner.run(task, tmp_path, outcome=None)
        assert calls == ["prepare", "remove"]
    else:
        result = runner.run(task, tmp_path, outcome=None)
        assert result.passed is (failure in {"success", "flood"})
        assert result.boundary == ("UNKNOWN" if failure == "unverified" else "LINUX_SANDBOX")
        assert result.timed_out is (failure == "timeout")
        assert calls == ["prepare", "close"]



def test_hidden_runner_refuses_missing_actor(tmp_path):
    with pytest.raises(RuntimeError, match="delegated actor"):
        VerificationHiddenTestRunner(None).run(None, tmp_path, outcome=SimpleNamespace(change_id=str(uuid4())))


@pytest.mark.parametrize("flow_outcome", ["ok", "stopped", "failed"])
def test_api_agent_delegates_full_flow_and_binds_budget_and_contract(tmp_path, monkeypatch, flow_outcome):
    from backend.app.evals.agents import ApiAgentDriver
    from backend.app.cli.run_flow import RunFlow
    from backend.app.contracts.models import VerificationActionRequest
    ids = [str(uuid4()) for _ in range(4)]
    calls = []
    class Client:
        def create_change(self, *_): return {"id": ids[0], "revision": 1}
        def update_change_contract(self, change_id, **kwargs):
            assert str(change_id) == ids[0]
            assert kwargs["allowed_paths"] == ["mathops.py"]
            assert kwargs["required_checks"] == ["python"]
            calls.append("contract")
        def create_actor(self, kind, _): return {"id": ids[1] if kind == "HUMAN" else ids[2]}
        def create_delegation(self, **kwargs):
            assert kwargs["scopes"] == ["agent.launch", "workspace.apply", "change.legacy_verify"]
            calls.append("delegation")
        def list_agent_runs(self, _):
            return {"items": [{"id": ids[3], "status": "PASSED", "stdout": "{}"}]}
    def run(_self, change, actor, options):
        assert str(actor) == ids[2]
        assert options.args[-2:] == ["--max-budget-usd", "0.25"]
        calls.append("run")
        return SimpleNamespace(steps=[SimpleNamespace(name="launch", detail={"run_id": ids[3]})], outcome=flow_outcome)
    monkeypatch.setattr(RunFlow, "run", run)
    task = SimpleNamespace(id="addition", prompt="fix add", timeout_seconds=60,
                           budget_usd=0.25, allowed_paths=("mathops.py",), required_checks=("python",))
    outcome = ApiAgentDriver(Client(), adapter="claude", executable="claude", args_for=lambda p: ["-p", p]).run(task, tmp_path, "fix", attempt=1)
    assert outcome.actor_id == ids[2]
    assert outcome.status == ("PASSED" if flow_outcome == "ok" else "ERROR")
    assert calls == ["contract", "delegation", "run"]
    request = VerificationActionRequest.model_validate({"actor_id": outcome.actor_id, "verification": {"executable": "python", "args": [], "timeout_seconds": 60}})
    assert str(request.actor_id) == ids[2]



def test_linux_hidden_capture_keeps_bounded_tail_of_real_noisy_child(tmp_path, monkeypatch):
    import subprocess
    import sys
    from backend.app.evals.hidden import SandboxHiddenTestRunner, OUTPUT_TAIL_BYTES
    from backend.app.execution import linux_sandbox
    closed = []
    class Group:
        def remove(self): pass
    class Hierarchy:
        def prepare(self): pass
        def create_run(self, *_): return Group()
    class Process:
        linux_sandbox = SimpleNamespace(verified=True)
        def __init__(self):
            self._popen = subprocess.Popen([sys.executable, "-c", "print('x' * 200000); print('FINAL-MARKER')"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def close(self):
            assert self._popen.poll() is not None
            closed.append(True)
    monkeypatch.setattr(linux_sandbox, "spawn_linux_sandbox", lambda *_args, **_kwargs: Process())
    task = SimpleNamespace(hidden_test_command=("python", "hidden_tests/check.py"), timeout_seconds=10)
    result = SandboxHiddenTestRunner(Hierarchy).run(task, tmp_path, outcome=None)
    assert result.passed
    assert len(result.output_tail.encode()) <= OUTPUT_TAIL_BYTES
    assert "FINAL-MARKER" in result.output_tail
    assert closed == [True]
# ------------------------------------------------------------ Linux sandbox runner (faked)


class FakeHierarchy:
    prepares = 0

    def __init__(self) -> None:
        self.created = []

    def prepare(self):
        FakeHierarchy.prepares += 1
        return ()

    def create_run(self, run_id, limits):
        self.created.append(run_id)
        return SimpleNamespace(run_id=run_id)


class FakeSandboxProcess(LinuxSandboxProcess):
    """The real wrapper (its own wait and returncode logic) over a plain host child."""

    def __init__(self, argv, cwd, *, verified=True) -> None:
        popen = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        session = SimpleNamespace(terminate=popen.kill, observe=lambda: None, close=lambda: None)
        super().__init__(popen, pid=popen.pid, session=session,
                         facts=SimpleNamespace(verified=verified))
        self.killed = self.closed = False

    def kill(self):
        self.killed = True
        super().kill()

    def close(self):
        self.closed = True
        super().close()


def _python_task(tmp_path: Path, body: str, *, timeout: int = 30):
    tree = tmp_path / "tree"
    (tree / "hidden_tests").mkdir(parents=True)
    (tree / "hidden_tests" / "check.py").write_text(body)
    return SimpleNamespace(hidden_test_command=("python", "hidden_tests/check.py"),
                           timeout_seconds=timeout), tree


@pytest.fixture
def fake_spawn(monkeypatch):
    import backend.app.execution.linux_sandbox as linux_sandbox

    spawned: list[FakeSandboxProcess] = []
    state = {"verified": True}

    def spawn(spec, cgroup, *, redact):
        assert spec.network is False and spec.cwd in spec.writable
        process = FakeSandboxProcess([*spec.argv], spec.cwd, verified=state["verified"])
        spawned.append(process)
        return process

    monkeypatch.setattr(linux_sandbox, "spawn_linux_sandbox", spawn)
    return spawned, state


def test_sandbox_runner_reports_pass_from_the_inner_exit_code(tmp_path, fake_spawn):
    spawned, _state = fake_spawn
    task, tree = _python_task(tmp_path, "print('ok')\n")
    result = SandboxHiddenTestRunner(FakeHierarchy).run(task, tree, outcome=AgentOutcome("PASSED", 0))
    assert result.passed and result.exit_code == 0 and not result.timed_out
    assert result.boundary == "LINUX_SANDBOX" and "ok" in result.output_tail
    assert spawned[0].closed


def test_sandbox_runner_reports_failure_and_unverified_boundary(tmp_path, fake_spawn):
    _spawned, state = fake_spawn
    state["verified"] = False
    task, tree = _python_task(tmp_path, "raise SystemExit(3)\n")
    result = SandboxHiddenTestRunner(FakeHierarchy).run(task, tree, outcome=AgentOutcome("PASSED", 0))
    assert not result.passed and result.exit_code == 3
    assert result.boundary == "UNKNOWN"


def test_sandbox_runner_kills_on_timeout(tmp_path, fake_spawn):
    spawned, _state = fake_spawn
    task, tree = _python_task(tmp_path, "import time\ntime.sleep(30)\n", timeout=1)
    result = SandboxHiddenTestRunner(FakeHierarchy).run(task, tree, outcome=AgentOutcome("PASSED", 0))
    assert result.timed_out and not result.passed and result.exit_code is None
    assert spawned[0]._popen.poll() is not None and spawned[0].closed


def test_sandbox_runner_prepares_the_hierarchy_once_across_workers():
    FakeHierarchy.prepares = 0

    class SlowHierarchy(FakeHierarchy):
        def prepare(self):
            time.sleep(0.05)  # widen the window an unlocked check-then-set would race in
            return super().prepare()

    runner = SandboxHiddenTestRunner(SlowHierarchy)
    threads = [threading.Thread(target=runner._hierarchy) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert FakeHierarchy.prepares == 1


# ------------------------------------------------------------ runner helpers


def _task(tmp_path: Path, *, hidden: dict[str, str], repo: dict[str, str], prompt="Fix it."):
    directory = tmp_path / "t1"
    for relative, content in repo.items():
        (directory / "repo" / relative).parent.mkdir(parents=True, exist_ok=True)
        (directory / "repo" / relative).write_text(content)
    for relative, content in hidden.items():
        (directory / "hidden_tests" / relative).parent.mkdir(parents=True, exist_ok=True)
        (directory / "hidden_tests" / relative).write_text(content)
    (directory / "task.toml").write_text(
        f'id = "t1"\ntitle = "Title {{prompt}}"\nprompt = "{prompt}"\n'
        'hidden_test_command = ["python", "hidden_tests/check.py"]\n')
    return load_task(directory)


def test_empty_hidden_files_do_not_match_empty_fixture_files(tmp_path):
    task = _task(tmp_path, hidden={"__init__.py": "", "check.py": "assert 1\n"},
                 repo={"pkg/__init__.py": "", "app.py": "x = 1\n"})
    assert hidden_tests_absent(task, task.repo)


def test_hidden_files_are_still_found_by_path_and_content(tmp_path):
    task = _task(tmp_path, hidden={"__init__.py": "", "check.py": "assert 1\n"},
                 repo={"app.py": "x = 1\n"})
    (task.repo / "hidden_tests").mkdir()
    (task.repo / "hidden_tests" / "__init__.py").write_text("")
    assert not hidden_tests_absent(task, task.repo)
    (task.repo / "hidden_tests" / "__init__.py").unlink()
    (task.repo / "copied.py").write_text("assert 1\n")
    assert not hidden_tests_absent(task, task.repo)


def test_prompt_template_substitutes_in_one_pass(tmp_path):
    task = _task(tmp_path, hidden={"check.py": "assert 1\n"}, repo={"app.py": ""},
                 prompt="Keep the literal {title} token in README.")
    rendered = AgentConfig("c", prompt_template="[{title}] {prompt}").render(task)
    assert rendered == "[Title {prompt}] Keep the literal {title} token in README."


def test_resolve_hidden_command_refuses_unknown_tools():
    with pytest.raises(RuntimeError, match="not installed"):
        hidden_module.resolve_hidden_command(("definitely-not-a-tool-xyz",))
