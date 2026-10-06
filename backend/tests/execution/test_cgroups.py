"""cgroup v2 handling against a fake cgroupfs (any OS); the real kernel is in tests/execution/linux."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from backend.app.core.errors import AppError
from backend.app.execution.cgroups import (
    PARENT_ENV,
    SUPERVISOR_LEAF,
    CgroupHierarchy,
    RunCgroup,
    RunLimits,
    own_cgroup,
    parse_keyed,
    process_cgroup,
)


def _fake_root(tmp_path: Path, *, controllers: str = "cpu memory pids io",
               procs: str = "") -> tuple[Path, Path]:
    root = tmp_path / "cgroup"
    parent = root / "user.slice" / "sentinel"
    parent.mkdir(parents=True)
    (root / "cgroup.controllers").write_text(controllers)
    (parent / "cgroup.controllers").write_text(controllers)
    (parent / "cgroup.procs").write_text(procs)
    (parent / "cgroup.subtree_control").write_text("")
    return root, parent


def test_parse_keyed() -> None:
    assert parse_keyed("populated 1\nfrozen 0\n") == {"populated": "1", "frozen": "0"}
    assert parse_keyed("") == {}
    assert parse_keyed("max 3\n\nweird\n") == {"max": "3", "weird": ""}


def test_own_and_process_cgroup_read_the_unified_line(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "cgroup").write_text("12:pids:/x\n0::/user.slice/app.scope\n")
    (proc / "77").mkdir()
    (proc / "77" / "cgroup").write_text("0::/sentinel/sentinel-run-ab\n")
    assert own_cgroup(proc) == "/user.slice/app.scope"
    assert process_cgroup(77, proc) == "/sentinel/sentinel-run-ab"
    assert process_cgroup(78, proc) is None
    (proc / "self" / "cgroup").write_text("12:pids:/x\n")
    with pytest.raises(AppError, match="no cgroup v2 membership"):
        own_cgroup(proc)


def test_a_v1_or_hybrid_mount_is_refused(tmp_path: Path) -> None:
    (tmp_path / "cgroup").mkdir()
    with pytest.raises(AppError) as raised:
        CgroupHierarchy.from_environment({}, root=tmp_path / "cgroup")
    assert raised.value.code == "LINUX_CGROUP_UNAVAILABLE"
    assert "unified" in raised.value.message


def test_the_configured_parent_must_be_absolute_and_inside_the_mount(tmp_path: Path) -> None:
    root, _parent = _fake_root(tmp_path)
    with pytest.raises(AppError, match="absolute"):
        CgroupHierarchy.from_environment({PARENT_ENV: "relative/path"}, root=root)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(AppError, match="not inside"):
        CgroupHierarchy.from_environment({PARENT_ENV: str(outside)}, root=root)
    escape = root / "user.slice" / ".." / ".." / "elsewhere"  # resolved before the check
    with pytest.raises(AppError, match="not inside"):
        CgroupHierarchy.from_environment({PARENT_ENV: str(escape)}, root=root)


def test_the_parent_defaults_to_this_process_cgroup(tmp_path: Path) -> None:
    root, parent = _fake_root(tmp_path)
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "cgroup").write_text("0::/user.slice/sentinel\n")
    hierarchy = CgroupHierarchy.from_environment({}, root=root, proc_root=proc)
    assert hierarchy.parent == Path(os.path.realpath(parent))


def test_missing_required_controllers_fail_closed(tmp_path: Path) -> None:
    root, parent = _fake_root(tmp_path, controllers="cpu io")
    hierarchy = CgroupHierarchy(parent, root=root)
    with pytest.raises(AppError, match="pids, memory"):
        hierarchy.check()
    with pytest.raises(AppError, match="pids, memory"):
        hierarchy.prepare()


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="POSIX permissions (root ignores them)")
def test_an_unwritable_parent_fails_closed(tmp_path: Path) -> None:
    root, parent = _fake_root(tmp_path)
    (parent / "cgroup.subtree_control").chmod(0o444)
    with pytest.raises(AppError, match="not writable"):
        CgroupHierarchy(parent, root=root).check()


def _cgroupfs_semantics(monkeypatch, parent: Path) -> list[tuple[Path, str]]:
    """Emulate what the plain filesystem lacks: moved procs leave the parent,
    enabled controllers appear, and a new cgroup directory has interface files."""
    import backend.app.execution.cgroups as module

    moved: list[tuple[Path, str]] = []
    real_write = module._write

    def fake_write(path: Path, value: str) -> None:
        if path.name == "cgroup.procs" and path.parent.name == SUPERVISOR_LEAF:
            moved.append((path, value))
            (parent / "cgroup.procs").write_text("")
            return
        if path.name == "cgroup.subtree_control":
            enabled = set(path.read_text().split())
            enabled |= {item[1:] for item in value.split() if item.startswith("+")}
            path.write_text(" ".join(sorted(enabled)))
            return
        real_write(path, value)

    real_mkdir = Path.mkdir

    def fake_mkdir(self, *args, **kwargs):
        real_mkdir(self, *args, **kwargs)
        if self.name.startswith("sentinel-run-"):
            for name in ("pids.max", "memory.max", "cpu.max", "cgroup.procs"):
                (self / name).write_text("")

    monkeypatch.setattr(module, "_write", fake_write)
    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    return moved


def test_prepare_moves_resident_processes_into_a_leaf_and_enables_controllers(
        tmp_path: Path, monkeypatch) -> None:
    root, parent = _fake_root(tmp_path, procs="101\n")
    moved = _cgroupfs_semantics(monkeypatch, parent)
    controllers = CgroupHierarchy(parent, root=root).prepare()
    assert moved == [(parent / SUPERVISOR_LEAF / "cgroup.procs", "101")]
    assert controllers == ("pids", "memory", "cpu")
    assert set((parent / "cgroup.subtree_control").read_text().split()) == {"pids", "memory", "cpu"}


def test_limits_are_written_to_the_run_cgroup(tmp_path: Path, monkeypatch) -> None:
    root, parent = _fake_root(tmp_path)
    _cgroupfs_semantics(monkeypatch, parent)
    hierarchy = CgroupHierarchy(parent, root=root)
    run_id = uuid4()
    run = hierarchy.create_run(run_id, RunLimits(pids_max=64, memory_max_bytes=256 * 1024 ** 2,
                                                 cpu_max_percent=150))
    assert run.path == parent / f"sentinel-run-{run_id.hex}"
    assert (run.path / "pids.max").read_text() == "64"
    assert (run.path / "memory.max").read_text() == str(256 * 1024 ** 2)
    assert (run.path / "cpu.max").read_text() == "150000 100000"
    assert run.relative == f"/user.slice/sentinel/sentinel-run-{run_id.hex}"
    with pytest.raises(AppError, match="already exists"):
        hierarchy.create_run(run_id, RunLimits())


def test_cpu_max_is_skipped_when_the_cpu_controller_is_not_delegated(
        tmp_path: Path, monkeypatch) -> None:
    root, parent = _fake_root(tmp_path, controllers="memory pids")
    _cgroupfs_semantics(monkeypatch, parent)
    run = CgroupHierarchy(parent, root=root).create_run(uuid4(), RunLimits())
    assert (run.path / "cpu.max").read_text() == ""


@pytest.mark.parametrize("kwargs", [{"pids_max": 0}, {"memory_max_bytes": 1024},
                                    {"cpu_max_percent": 0}])
def test_unusable_limits_are_refused(kwargs) -> None:
    with pytest.raises(ValueError):
        RunLimits(**kwargs)


def test_leftover_runs_are_found_for_the_crash_sweep(tmp_path: Path) -> None:
    root, parent = _fake_root(tmp_path)
    keep = [parent / f"sentinel-run-{uuid4().hex}" for _ in range(3)]
    for path in keep:
        path.mkdir()
    (parent / "sentinel-supervisor").mkdir()
    (parent / "sentinel-run-not-hex").mkdir()
    (parent / "other").mkdir()
    found = CgroupHierarchy(parent, root=root).leftover_runs()
    assert sorted(run.path for run in found) == sorted(keep)


def test_pids_walks_child_cgroups(tmp_path: Path) -> None:
    run = tmp_path / "sentinel-run-x"
    (run / "child").mkdir(parents=True)
    (run / "cgroup.procs").write_text("10\n11\n")
    (run / "child" / "cgroup.procs").write_text("12\n10\n")
    (run / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    cgroup = RunCgroup(run, tmp_path)
    assert cgroup.pids() == [10, 11, 12]
    assert cgroup.populated() and not cgroup.frozen()


def test_an_apparmor_userns_restriction_is_named_with_its_remedy(tmp_path: Path, monkeypatch) -> None:
    import sys

    from backend.app.execution import linux_sandbox

    fake_bwrap = tmp_path / "bwrap"
    fake_bwrap.write_text("")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(linux_sandbox.seccomp, "host_arch", lambda machine=None: "x86_64")
    monkeypatch.setattr(linux_sandbox, "find_bwrap", lambda environ=None: fake_bwrap)
    monkeypatch.setattr(linux_sandbox, "probe_bwrap", lambda bwrap: (
        False, "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted", True))
    sysctl = tmp_path / "apparmor_restrict_unprivileged_userns"
    monkeypatch.setattr(linux_sandbox, "APPARMOR_USERNS_SYSCTL", sysctl)
    sysctl.write_text("1\n")
    monkeypatch.setattr(linux_sandbox, "_apparmor_restricts_userns",
                        lambda path=sysctl: path.read_text().strip() == "1")
    with pytest.raises(AppError) as raised:
        linux_sandbox.require_sandbox()
    assert raised.value.code == "LINUX_SANDBOX_UNAVAILABLE"
    assert "AppArmor" in raised.value.message and "userns" in raised.value.message
    sysctl.write_text("0\n")
    with pytest.raises(AppError) as raised:
        linux_sandbox.require_sandbox()
    assert "unprivileged_userns_clone" in raised.value.message
