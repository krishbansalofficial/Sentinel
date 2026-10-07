"""Go checks in Linux confined check boxes, on a real kernel, through the real HTTP API.

The Linux twin of ``acceptance/test_confined_go.py``: ``go test`` runs offline from
the host GOROOT bound read-only, passes with a verified ``LINUX_SANDBOX`` boundary,
and an agent-authored Go test cannot write the repository, read the Sentinel store
or reach the local API. Each escape has a host positive control first.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.tests.acceptance.test_confined_go import CALC, CALC_TEST, GO_MOD
from backend.tests.execution.linux.conftest import requires_linux_sandbox
from backend.tests.execution.linux.test_linux_check_boxes import (  # noqa: F401 (fixture)
    LiveApi,
    _assert_linux_confined,
    _verified_check,
    live_api,
)
from backend.tests.support_kb import make_repo

GO = shutil.which("go")
pytestmark = [
    requires_linux_sandbox,
    pytest.mark.real_check_boxes,
    pytest.mark.skipif(GO is None, reason="Go is not installed"),
]


# The Windows probe reports a listing only as success/failure; in a mount namespace the
# store path may exist as the skeleton above the box's own tree, so this one prints the
# entries and the test judges them like the Python twin: nothing of the store may show.
ESCAPE_TEST = """package calc

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestEscapes(t *testing.T) {
	writeErr := os.WriteFile(filepath.Join(`{repo}`, "escaped.txt"), []byte("x"), 0o644)
	_, readErr := os.ReadFile(filepath.Join(`{store}`, "api_token"))
	entries, listErr := os.ReadDir(`{store}`)
	names := []string{}
	for _, entry := range entries {
		names = append(names, entry.Name())
	}
	conn, dialErr := net.DialTimeout("tcp", "127.0.0.1:{port}", 2*time.Second)
	if conn != nil {
		conn.Close()
	}
	fmt.Printf("SENTINEL_GO write=%v read=%v dial=%v list=%v:%s\\n", writeErr != nil,
		readErr != nil, dialErr != nil, listErr != nil, strings.Join(names, ","))
}
"""
STORE_SECRETS = ("api_token", "sentinel.sqlite3", "trusted_keys.json")


def _probe(stdout: str) -> dict[str, str]:
    line = next((item for item in stdout.splitlines() if item.startswith("SENTINEL_GO ")), "")
    return dict(part.split("=", 1) for part in line.split()[1:])


def _go_repo(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    files = {"go.mod": GO_MOD, "calc.go": CALC, "calc_test.go": CALC_TEST, **(extra or {})}
    return make_repo(tmp_path / "repo", files)


def test_go_test_passes_confined_with_a_verified_linux_boundary(live_api: LiveApi,
                                                                 tmp_path: Path) -> None:
    repo = _go_repo(tmp_path)
    before = sorted(path.relative_to(repo).as_posix() for path in repo.rglob("*")
                    if ".git" not in path.parts)
    change_id, verification = _verified_check(live_api, repo, "go", ["test", "-count=1", "./..."])
    assert verification["status"] == "PASSED", verification["stdout"] + verification["stderr"]
    assert "ok" in verification["stdout"]
    _assert_linux_confined(live_api, change_id, verification)
    # Nothing was built into, or left in, the user's repository.
    after = sorted(path.relative_to(repo).as_posix() for path in repo.rglob("*")
                   if ".git" not in path.parts)
    assert after == before


def test_go_test_escapes_are_denied(live_api: LiveApi, tmp_path: Path) -> None:
    repo = _go_repo(tmp_path)
    escape = (ESCAPE_TEST.replace("{repo}", str(repo)).replace("{store}", str(live_api.store))
              .replace("{port}", str(live_api.port)))
    (repo / "escape_test.go").write_text(escape, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@x", "-c",
                    "commit.gpgsign=false", "commit", "-qm", "escape"],
                   cwd=repo, check=True, capture_output=True)

    # Positive control: on the host the same test writes the repository, reads the API
    # token, lists the store and reaches the API.
    host = subprocess.run([GO, "test", "-count=1", "-run", "TestEscapes", "-v", "./..."],
                          cwd=repo, capture_output=True, text=True, timeout=300,
                          env={**os.environ, "GOFLAGS": "-mod=readonly", "GOTOOLCHAIN": "local"})
    control = _probe(host.stdout)
    assert control.get("write") == "false" and control["read"] == "false", host.stdout + host.stderr
    assert control["dial"] == "false" and "api_token" in control["list"], control
    (repo / "escaped.txt").unlink()
    subprocess.run(["git", "clean", "-fdq"], cwd=repo, check=True)

    change_id, verification = _verified_check(
        live_api, repo, "go", ["test", "-count=1", "-run", "TestEscapes", "-v", "./..."])
    probe = _probe(verification["stdout"])
    assert probe, verification["stdout"] + verification["stderr"]
    # A write either fails or lands on the box's private tmpfs; the host decides.
    assert not (repo / "escaped.txt").exists()
    assert probe["read"] == "true" and probe["dial"] == "true", probe
    assert probe["list"].startswith("true:") or not any(
        secret in probe["list"] for secret in STORE_SECRETS), probe
    _assert_linux_confined(live_api, change_id, verification)
