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
            assert json_body == {"executable": "python", "args": ["hidden_tests/check.py"], "timeout_seconds": 10}
            return {"verification": {"status": "PASSED", "exit_code": 0, "boundary": boundary}}

    result = VerificationHiddenTestRunner(Client()).run(
        SimpleNamespace(hidden_test_command=("python", "hidden_tests/check.py"), timeout_seconds=10),
        source, outcome=SimpleNamespace(change_id=str(change_id)))
    assert result.passed
    assert result.boundary == (boundary or "UNKNOWN")


def test_verification_hidden_runner_requires_change(tmp_path):
    with pytest.raises(RuntimeError, match="Change"):
        VerificationHiddenTestRunner(None).run(None, tmp_path, outcome=SimpleNamespace(change_id=None))
