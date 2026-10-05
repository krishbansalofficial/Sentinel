"""Fixtures for the real Linux sandbox tests.

They need Linux, bubblewrap, unprivileged user namespaces and a delegated
cgroup v2 subtree named by ``SENTINEL_TEST_CGROUP_PARENT`` (CI creates one with
sudo; see .github/workflows/ci.yml). ``SENTINEL_TEST_CGROUP_CONTROLLERS=0``
marks a subtree without the pids/memory controllers (a hybrid-cgroup dev box):
membership, freeze and kill still work, and the limit tests skip.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from backend.app.execution.cgroups import CgroupHierarchy, RunCgroup, RunLimits

PARENT = os.environ.get("SENTINEL_TEST_CGROUP_PARENT", "")
CONTROLLERS = os.environ.get("SENTINEL_TEST_CGROUP_CONTROLLERS", "1") != "0"
# CI sets this so a broken environment fails loudly instead of skipping silently.
REQUIRED = os.environ.get("SENTINEL_REQUIRE_LINUX_SANDBOX") == "1"
AVAILABLE = (sys.platform.startswith("linux") and bool(PARENT)
             and shutil.which("bwrap", path="/usr/bin:/bin:/usr/local/bin") is not None)

requires_linux_sandbox = pytest.mark.skipif(
    not AVAILABLE and not REQUIRED,
    reason="needs Linux, bubblewrap and SENTINEL_TEST_CGROUP_PARENT (a delegated cgroup v2 subtree)",
)
requires_controllers = pytest.mark.skipif(
    not CONTROLLERS and not REQUIRED,
    reason="the delegated subtree has no pids/memory controllers here")


@pytest.fixture
def run_cgroup():
    """A fresh run cgroup under the delegated parent, removed afterwards."""
    created: list[RunCgroup] = []

    def make(limits: RunLimits | None = None) -> RunCgroup:
        if CONTROLLERS:
            cgroup = CgroupHierarchy.from_environment(
                {"SENTINEL_CGROUP_PARENT": PARENT}).create_run(uuid4(), limits or RunLimits())
        else:
            path = Path(PARENT) / f"sentinel-run-{uuid4().hex}"
            path.mkdir()
            cgroup = RunCgroup(path)
        created.append(cgroup)
        return cgroup

    yield make
    for cgroup in created:
        cgroup.remove()


class _NoControllerHierarchy(CgroupHierarchy):
    """A dev-box hierarchy without pids/memory: membership, freeze and kill only."""

    def prepare(self) -> tuple[str, ...]:
        return ()

    def create_run(self, run_id, limits) -> RunCgroup:
        path = self.parent / f"sentinel-run-{run_id.hex}"
        path.mkdir()
        return RunCgroup(path, self.root)


@pytest.fixture
def hierarchy_factory():
    """What AgentLauncher(cgroups=...) should use in this environment."""
    if CONTROLLERS:
        return lambda: CgroupHierarchy.from_environment({"SENTINEL_CGROUP_PARENT": PARENT})
    return lambda: _NoControllerHierarchy(Path(PARENT))


@pytest.fixture
def sandbox_dirs(tmp_path: Path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    return workspace, home
