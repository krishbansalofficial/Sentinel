"""The Linux sandbox against a real kernel: every escape has a host positive control.

Secrets here are generated mock data written by the test itself.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.app.core.errors import AppError
from backend.app.execution import linux_sandbox
from backend.app.execution.cgroups import RunLimits
from backend.app.execution.linux_sandbox import (
    SandboxSpec,
    observe_facts,
    spawn_linux_sandbox,
    verified_linux_facts,
)
from backend.tests.execution.linux.conftest import requires_controllers, requires_linux_sandbox

pytestmark = requires_linux_sandbox
PYTHON = os.path.realpath(sys.executable)


def _spec(workspace: Path, home: Path, code: str, *, network: bool = False,
          extra_writable: tuple[Path, ...] = ()) -> SandboxSpec:
    return SandboxSpec(
        argv=(PYTHON, "-c", code), cwd=workspace,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8"},
        writable=(workspace, home, *extra_writable), network=network,
    )


def _run(spec: SandboxSpec, cgroup, timeout: float = 60.0) -> tuple[int, str, str, object]:
    process = spawn_linux_sandbox(spec, cgroup, redact=lambda text: text)
    try:
        stdout, stderr = process._popen.communicate(timeout=timeout)
        return process._popen.returncode, stdout.decode(), stderr.decode(), process
    finally:
        process.close()


def test_a_legitimate_program_runs_and_its_boundary_verified(run_cgroup, sandbox_dirs) -> None:
    workspace, home = sandbox_dirs
    code = ("import os, pathlib; pathlib.Path('result.txt').write_text('ok');"
            "print(os.getcwd(), open('/proc/self/status').read().count('Seccomp:\t2'))")
    returncode, stdout, stderr, process = _run(_spec(workspace, home, code), run_cgroup())
    assert returncode == 0, stderr
    assert stdout.split() == [str(workspace), "1"]
    assert (workspace / "result.txt").read_text() == "ok"
    facts = process.linux_sandbox
    assert facts.verified and facts.failures == ()
    assert verified_linux_facts(facts.to_payload())
    for name in ("user", "mnt", "pid", "ipc", "uts", "cgroup", "net"):
        assert facts.namespaces[name] != facts.host_namespaces[name], name
    assert facts.seccomp_filters > facts.supervisor_seccomp_filters
    assert facts.no_new_privs == "1"


def test_writes_outside_the_workspace_and_home_are_impossible(run_cgroup, sandbox_dirs, tmp_path) -> None:
    workspace, home = sandbox_dirs
    outside = tmp_path / "outside"
    outside.mkdir()
    targets = [outside / "planted.txt", Path.home() / f"sentinel-escape-{secrets.token_hex(4)}",
               Path("/tmp") / f"sentinel-escape-{secrets.token_hex(4)}"]
    code = ("import sys\nresults = []\n"
            f"for target in {[str(t) for t in targets]!r}:\n"
            "    try:\n        open(target, 'w').write('x'); results.append('WROTE')\n"
            "    except OSError as exc:\n        results.append(type(exc).__name__)\n"
            "print(' '.join(results))")
    returncode, stdout, stderr, _ = _run(_spec(workspace, home, code), run_cgroup())
    assert returncode == 0, stderr
    assert "WROTE" not in stdout.split()[:2], stdout
    for target in targets:
        assert not target.exists(), target  # /tmp inside is a private tmpfs
    # Positive control: the host can write every one of those targets.
    for target in targets:
        target.write_text("host")
        assert target.read_text() == "host"
        target.unlink()


def test_user_secrets_store_and_token_are_unreadable(run_cgroup, sandbox_dirs, tmp_path) -> None:
    workspace, home = sandbox_dirs
    secret = f"mock-secret-{secrets.token_hex(16)}"
    planted = []
    for relative in (".ssh/id_ed25519", ".aws/credentials",
                     f".local/share/sentinel-test-{secrets.token_hex(3)}/api_token"):
        path = Path.home() / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret)
        planted.append(path)
    try:
        code = ("found = []\n"
                f"for target in {[str(p) for p in planted]!r}:\n"
                "    try:\n        found.append(open(target).read())\n"
                "    except OSError as exc:\n        found.append(type(exc).__name__)\n"
                "print('|'.join(found))")
        returncode, stdout, stderr, _ = _run(_spec(workspace, home, code), run_cgroup())
        assert returncode == 0, stderr
        assert secret not in stdout and secret not in stderr
        assert stdout.strip().split("|") == ["FileNotFoundError"] * 3
        # Positive control: the host reads them.
        assert all(path.read_text() == secret for path in planted)
    finally:
        for path in planted:
            path.unlink()


def _host_can_connect() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=5).close()
        return True
    except OSError:
        return False


def test_no_network_without_the_capability(run_cgroup, sandbox_dirs) -> None:
    workspace, home = sandbox_dirs
    code = ("import socket\ntry:\n    socket.create_connection(('1.1.1.1', 443), timeout=5)\n"
            "    print('CONNECTED')\nexcept OSError as exc:\n    print(type(exc).__name__, exc.errno)")
    returncode, stdout, stderr, process = _run(_spec(workspace, home, code), run_cgroup())
    assert returncode == 0, stderr
    assert "CONNECTED" not in stdout
    assert process.linux_sandbox.network_isolated
    if not _host_can_connect():
        pytest.skip("positive control unavailable: the host itself is offline")


def test_network_is_shared_only_when_the_profile_declares_it(run_cgroup, sandbox_dirs) -> None:
    workspace, home = sandbox_dirs
    returncode, stdout, stderr, process = _run(
        _spec(workspace, home, "print('ran')", network=True), run_cgroup())
    assert returncode == 0, stderr
    facts = process.linux_sandbox
    assert facts.namespaces["net"] == facts.host_namespaces["net"]
    assert not facts.network_isolated and facts.verified


def test_seccomp_denies_ptrace_mount_and_new_namespaces(run_cgroup, sandbox_dirs) -> None:
    workspace, home = sandbox_dirs
    code = (
        "import ctypes, os\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "def call(name, *args):\n"
        "    result = getattr(libc, name)(*args)\n"
        "    return 'OK' if result == 0 else os.strerror(ctypes.get_errno())\n"
        "print('unshare', call('unshare', 0x10000000))\n"
        "print('mount', call('mount', b'none', b'/tmp', b'tmpfs', 0, None))\n"
        "print('ptrace', call('ptrace', 16, 1, None, None))\n"
        "print('keyctl', call('syscall', 250 if os.uname().machine == 'x86_64' else 219, 0, 0))\n"
    )
    returncode, stdout, stderr, _ = _run(_spec(workspace, home, code), run_cgroup())
    assert returncode == 0, stderr
    lines = dict(line.split(" ", 1) for line in stdout.strip().splitlines())
    assert lines == {name: "Operation not permitted"
                     for name in ("unshare", "mount", "ptrace", "keyctl")}, stdout


def test_daemons_that_double_fork_and_setsid_are_attributed_and_killed(
        run_cgroup, sandbox_dirs) -> None:
    workspace, home = sandbox_dirs
    marker = workspace / "daemon.pid"
    code = (
        "import os, time, sys\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    if os.fork() == 0:\n"
        f"        open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
        "        time.sleep(600)\n"
        "    os._exit(0)\n"
        "time.sleep(600)\n"
    )
    cgroup = run_cgroup()
    process = spawn_linux_sandbox(_spec(workspace, home, code), cgroup, redact=lambda t: t)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.05)
        assert marker.exists()
        records = []
        while time.monotonic() < deadline:
            records = [r for r in process.session.observe() if r.terminated_at is None]
            if records:
                break
            time.sleep(0.05)
        assert any(r.attributed and r.executable_path and "python" in r.executable_path
                   for r in records)
        assert len(cgroup.pids()) >= 2
        process.kill()
        process.wait(10)
        assert cgroup.pids() == []
        assert all(r.terminated_at is not None for r in process.session.observe())
    finally:
        process.close()
    assert not cgroup.path.exists()


def _cpu_ticks(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[11]) + int(fields[12])


def test_pause_freezes_the_whole_tree(run_cgroup, sandbox_dirs) -> None:
    workspace, home = sandbox_dirs
    code = ("import os\nif os.fork() == 0:\n    while True: pass\nwhile True: pass\n")
    cgroup = run_cgroup()
    process = spawn_linux_sandbox(_spec(workspace, home, code), cgroup, redact=lambda t: t)
    try:
        time.sleep(0.5)
        pids = [pid for pid in cgroup.pids() if pid not in process.session._internal]
        assert len(pids) >= 2
        process.suspend()
        assert cgroup.frozen()
        before = {pid: _cpu_ticks(pid) for pid in pids}
        time.sleep(1.0)
        assert {pid: _cpu_ticks(pid) for pid in pids} == before
        process.resume()
        time.sleep(1.0)
        assert all(_cpu_ticks(pid) > before[pid] for pid in pids)
    finally:
        process.kill()
        process.close()


@requires_controllers
def test_a_fork_bomb_is_stopped_by_pids_max(run_cgroup, sandbox_dirs) -> None:
    workspace, home = sandbox_dirs
    code = ("import os, time\ncount = 0\nwhile True:\n    try:\n"
            "        if os.fork() == 0:\n            time.sleep(60); os._exit(0)\n"
            "        count += 1\n    except OSError:\n        print('LIMIT', count, flush=True); break\n"
            "time.sleep(60)\n")
    cgroup = run_cgroup(RunLimits(pids_max=64))
    process = spawn_linux_sandbox(_spec(workspace, home, code), cgroup, redact=lambda t: t)
    try:
        line = process.stdout.readline().decode()
        assert line.startswith("LIMIT"), line
        assert len(cgroup.pids()) <= 64
        assert int(cgroup.pids_events().get("max", "0")) >= 1
        started = time.monotonic()
        assert subprocess.run(["true"], timeout=10).returncode == 0  # host stays responsive
        assert time.monotonic() - started < 5
    finally:
        process.kill()
        process.close()


def test_missing_bwrap_fails_closed_before_anything_runs(run_cgroup, sandbox_dirs, monkeypatch) -> None:
    workspace, home = sandbox_dirs
    monkeypatch.setenv("SENTINEL_BWRAP", "/nonexistent/bwrap")
    with pytest.raises(AppError) as raised:
        spawn_linux_sandbox(_spec(workspace, home, "open('ran','w')"), run_cgroup(),
                            redact=lambda t: t)
    assert raised.value.code == "LINUX_SANDBOX_UNAVAILABLE"
    assert not (workspace / "ran").exists()


def test_disabled_user_namespaces_fail_closed(run_cgroup, sandbox_dirs, tmp_path, monkeypatch) -> None:
    workspace, home = sandbox_dirs
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\necho 'bwrap: No permissions to create new namespace' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("SENTINEL_BWRAP", str(fake))
    with pytest.raises(AppError) as raised:
        spawn_linux_sandbox(_spec(workspace, home, "open('ran','w')"), run_cgroup(),
                            redact=lambda t: t)
    assert raised.value.code == "LINUX_SANDBOX_UNAVAILABLE"
    assert "No permissions to create new namespace" in raised.value.message
    assert not (workspace / "ran").exists()


def test_a_failed_verification_never_releases_the_agent(run_cgroup, sandbox_dirs, monkeypatch) -> None:
    workspace, home = sandbox_dirs

    def forged(pid, *, expected_cgroup, network, proc_root=Path("/proc")):
        real = observe_facts(pid, expected_cgroup=expected_cgroup, network=network)
        return type(real)(**{**{f: getattr(real, f) for f in real.__dataclass_fields__},
                             "verified": False, "failures": ("simulated mismatch",)})

    monkeypatch.setattr(linux_sandbox, "observe_facts", forged)
    cgroup = run_cgroup()
    with pytest.raises(AppError) as raised:
        spawn_linux_sandbox(_spec(workspace, home, "open('ran','w')"), cgroup, redact=lambda t: t)
    assert raised.value.code == "LINUX_SANDBOX_VERIFICATION_FAILED"
    assert "simulated mismatch" in raised.value.message
    time.sleep(0.2)
    assert not (workspace / "ran").exists()
    assert not cgroup.path.exists()


def test_tampered_fact_payloads_never_verify(run_cgroup, sandbox_dirs) -> None:
    workspace, home = sandbox_dirs
    _, _, _, process = _run(_spec(workspace, home, "pass"), run_cgroup())
    good = process.linux_sandbox.to_payload()
    assert verified_linux_facts(good)
    tampered = [
        {**good, "verified": False},
        {**good, "failures": ["x"]},
        {**good, "seccomp_mode": "0"},
        {**good, "seccomp_filters": good["supervisor_seccomp_filters"]},
        {**good, "no_new_privs": "0"},
        {**good, "cgroup": "/elsewhere"},
        {**good, "namespaces": {**good["namespaces"], "mnt": good["host_namespaces"]["mnt"]}},
        {**good, "sandbox_kind": "appcontainer"},
        {**good, "namespaces": "not a mapping"},
        {**good, "seccomp_filters": "many"},
    ]
    for payload in tampered:
        assert not verified_linux_facts(payload), payload
