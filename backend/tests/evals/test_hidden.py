from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.evals.hidden import VerificationHiddenTestRunner


@pytest.mark.parametrize("boundary", [None, "APPCONTAINER"])
def test_windows_hidden_runner_never_invents_boundary(tmp_path, boundary):
    source, repository = tmp_path / "result", tmp_path / "repository"
    (source / "hidden_tests").mkdir(parents=True)
    (source / "hidden_tests" / "check.py").write_text("assert True")
    repository.mkdir()
    change_id = uuid4()

    class Client:
        def get_change(self, identifier):
            assert identifier == change_id
            return {"repository_path": str(repository)}

        def _request(self, method, path, *, json_body):
            assert (repository / "hidden_tests" / "check.py").exists()
            assert method == "POST" and str(change_id) in path
            assert json_body == {"actor_id": "operator", "verification": {"executable": "python", "args": ["hidden_tests/check.py"], "timeout_seconds": 10}}
            return {"verification": {"status": "PASSED", "exit_code": 0, "boundary": boundary}}

    result = VerificationHiddenTestRunner(Client()).run(
        SimpleNamespace(hidden_test_command=("python", "hidden_tests/check.py"), timeout_seconds=10),
        source, outcome=SimpleNamespace(change_id=str(change_id), actor_id="operator"))
    assert result.passed is (boundary == "APPCONTAINER")
    assert result.boundary == (boundary or "UNKNOWN")


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
