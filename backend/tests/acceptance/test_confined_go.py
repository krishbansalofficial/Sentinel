"""Go checks run confined: a real `go test` passes in its box, and escapes are denied.

Real AppContainer boxes through the live HTTP API, like the Python and Node
acceptance tests. Skipped when Go is not installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.tests.acceptance.test_confined_checks import (  # noqa: F401 (fixtures)
    LiveApi, _actor, _check_runs, _create_change, _verify, live_api, runtime_cache,
)
from backend.tests.support_kb import make_repo

GO = shutil.which("go")
pytestmark = [
    pytest.mark.skipif(GO is None or os.name != "nt", reason="Go on Windows is required"),
    # Like the other real-boundary tests: hosted Windows runners deny an AppContainer the NUL
    # device (Go needs it for build IDs), so these run locally or with the opt-in variable.
    pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") == "true"
                       and os.environ.get("SENTINEL_CI_REAL_APPCONTAINER") != "1",
                       reason="hosted runners cannot host real AppContainer boundary tests"),
]

GO_MOD = "module example.com/calc\n\ngo 1.21\n"
CALC = "package calc\n\nfunc Add(a, b int) int { return a + b }\n"
CALC_TEST = """package calc

import "testing"

func TestAdd(t *testing.T) {
	if Add(2, 3) != 5 {
		t.Fatal("bad sum")
	}
}
"""

ESCAPE_TEST = """package calc

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestEscapes(t *testing.T) {
	writeErr := os.WriteFile(filepath.Join(`{repo}`, "escaped.txt"), []byte("x"), 0o644)
	_, readErr := os.ReadDir(`{store}`)
	conn, dialErr := net.DialTimeout("tcp", "127.0.0.1:{port}", 2*time.Second)
	if conn != nil {
		conn.Close()
	}
	fmt.Printf("SENTINEL_GO write=%v read=%v dial=%v\\n", writeErr != nil, readErr != nil, dialErr != nil)
}
"""


def _go_repo(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    files = {"go.mod": GO_MOD, "calc.go": CALC, "calc_test.go": CALC_TEST, **(extra or {})}
    return make_repo(tmp_path / "repo", files)


def test_go_test_passes_confined_with_a_verified_boundary(live_api: LiveApi, tmp_path: Path) -> None:
    api = live_api
    repo = _go_repo(tmp_path)
    change_id = _create_change(api, repo)
    actor = _actor(api, change_id, ["change.legacy_verify"])
    response = _verify(api, change_id, actor, "go", ["test", "-count=1", "./..."])
    assert response.status_code == 200, response.text
    verification = response.json()["verification"]
    assert verification["status"] == "PASSED", verification["stdout"] + verification["stderr"]
    assert "ok" in verification["stdout"]
    assert verification["boundary"] == "APPCONTAINER"
    assert [run["boundary"] for run in _check_runs(api, change_id)] == ["APPCONTAINER"]
    # Nothing was built into, or left in, the user's repository.
    assert not any(path.suffix == ".exe" for path in repo.rglob("*"))


def test_go_test_escapes_are_denied(live_api: LiveApi, tmp_path: Path) -> None:
    api = live_api
    store = api.store  # the store this API uses (token, database, trust registry)
    port = str(api.port)
    repo = _go_repo(tmp_path)
    escape = ESCAPE_TEST.replace("{repo}", str(repo)).replace("{store}", str(store)).replace("{port}", port)
    (repo / "escape_test.go").write_text(escape, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@x", "commit", "-qm", "escape"],
                   cwd=repo, check=True, capture_output=True)

    # Positive control: on the host the same test can write into the repository.
    host = subprocess.run([GO, "test", "-count=1", "-run", "TestEscapes", "-v", "./..."], cwd=repo,
                          capture_output=True, text=True, timeout=300,
                          env={**os.environ, "GOFLAGS": "-mod=readonly", "GOTOOLCHAIN": "local"})
    assert "SENTINEL_GO write=false" in host.stdout, host.stdout + host.stderr
    (repo / "escaped.txt").unlink()
    subprocess.run(["git", "clean", "-fdq"], cwd=repo, check=True)

    change_id = _create_change(api, repo)
    actor = _actor(api, change_id, ["change.legacy_verify"])
    response = _verify(api, change_id, actor, "go", ["test", "-count=1", "-run", "TestEscapes", "-v", "./..."])
    assert response.status_code == 200, response.text
    verification = response.json()["verification"]
    line = next((item for item in verification["stdout"].splitlines() if item.startswith("SENTINEL_GO")), "")
    assert line == "SENTINEL_GO write=true read=true dial=true", verification["stdout"] + verification["stderr"]
    assert verification["boundary"] == "APPCONTAINER"
    assert not (repo / "escaped.txt").exists()
