"""Adversarial scenario fuzzer for the Linux sandbox (HANDOFF Phase 5).

Each seeded scenario combines 3 to 5 hostile behaviors into one agent program:
writes outside the workspace (directly and through a planted symlink), reads of
mock secrets (raw and base64-encoded in the output), connections to a live host
listener standing in for the local API and to the internet, double-fork/setsid
daemons, rapid spawn-and-exit bursts, and namespace/ptrace syscalls. The host
then checks every invariant from outside: no file appeared, no secret in any
output, the listener saw no connection, the cgroup is empty after stop, and no
sandboxed PID survives. Every escape found becomes a named regression test.

``SENTINEL_FUZZ_SCENARIOS`` sets how many run (default 25); the seed is fixed so
a failure reproduces.
"""

from __future__ import annotations

import base64
import json
import os
import random
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from backend.app.execution.linux_sandbox import SandboxSpec, spawn_linux_sandbox
from backend.tests.execution.linux.conftest import requires_linux_sandbox

pytestmark = requires_linux_sandbox
PYTHON = os.path.realpath(sys.executable)
SCENARIOS = int(os.environ.get("SENTINEL_FUZZ_SCENARIOS", "25"))
SEED = 20261008


def _behaviors(ctx: dict) -> dict[str, str]:
    """Python snippets, each printing one line tagged with its name."""
    return {
        "write_outside": f"""
try:
    open({ctx['outside_file']!r}, 'w').write('escaped'); print('write_outside WROTE')
except OSError as e: print('write_outside', type(e).__name__)
""",
        "write_via_symlink": f"""
import os
try:
    os.symlink({ctx['outside_dir']!r}, 'link')
    open('link/through_link.txt', 'w').write('escaped'); print('write_via_symlink WROTE')
except OSError as e: print('write_via_symlink', type(e).__name__)
""",
        "read_secret": f"""
import base64
try:
    data = open({ctx['secret_file']!r}).read()
    print('read_secret', data, base64.b64encode(data.encode()).decode())
except OSError as e: print('read_secret', type(e).__name__)
""",
        "local_api": f"""
import socket
try:
    socket.create_connection(('127.0.0.1', {ctx['listener_port']}), timeout=2); print('local_api CONNECTED')
except OSError as e: print('local_api', type(e).__name__)
""",
        "internet": """
import socket
try:
    socket.create_connection(('1.1.1.1', 443), timeout=2); print('internet CONNECTED')
except OSError as e: print('internet', type(e).__name__)
""",
        "daemon": """
import os, time
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        time.sleep(600)
    os._exit(0)
print('daemon spawned')
""",
        "spawn_burst": """
import os
for _ in range(60):
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
print('spawn_burst done')
""",
        "namespaces": """
import ctypes, os
libc = ctypes.CDLL(None, use_errno=True)
results = [libc.unshare(0x10000000), libc.ptrace(16, 1, None, None),
           libc.mount(b'none', b'/tmp', b'tmpfs', 0, None)]
print('namespaces', 'ESCAPED' if 0 in results else 'denied')
""",
    }


class _Listener:
    """A host TCP listener standing in for Sentinel's local API."""

    def __init__(self) -> None:
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen()
        self.socket.settimeout(0.2)
        self.port = self.socket.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self.socket.accept()
                self.accepted += 1
                connection.close()
            except OSError:
                continue

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self.socket.close()


def test_generated_hostile_scenarios_never_escape(run_cgroup, tmp_path: Path) -> None:
    rng = random.Random(SEED)
    secret = f"mock-secret-{secrets.token_hex(12)}"
    secret_file = Path.home() / f".sentinel-fuzz-secret-{secrets.token_hex(4)}"
    secret_file.write_text(secret)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    listener = _Listener()
    # Positive controls: the host itself can do each of these.
    socket.create_connection(("127.0.0.1", listener.port), timeout=2).close()
    time.sleep(0.3)
    assert listener.accepted == 1 and secret_file.read_text() == secret
    listener.accepted = 0
    report = {"seed": SEED, "scenarios": 0, "behaviors_run": 0, "escapes": []}
    try:
        for index in range(SCENARIOS):
            workspace = tmp_path / f"ws{index}"
            workspace.mkdir()
            ctx = {"outside_file": str(outside_dir / f"f{index}.txt"),
                   "outside_dir": str(outside_dir), "secret_file": str(secret_file),
                   "listener_port": listener.port}
            catalog = _behaviors(ctx)
            chosen = rng.sample(sorted(catalog), rng.randint(3, 5))
            program = "\n".join(catalog[name] for name in chosen) + "\nimport time\ntime.sleep(0.5)\n"
            cgroup = run_cgroup()
            process = spawn_linux_sandbox(
                SandboxSpec(argv=(PYTHON, "-c", program), cwd=workspace,
                            env={"PATH": "/usr/local/bin:/usr/bin:/bin"}, writable=(workspace,)),
                cgroup, redact=lambda text: text)
            try:
                stdout, stderr = process._popen.communicate(timeout=60)
            finally:
                process.kill()
                process.wait(10)
                remaining = cgroup.pids()
                process.close()
            output = (stdout + stderr).decode("utf-8", "replace")
            escapes = []
            if list(outside_dir.iterdir()):
                escapes.append("file written outside the workspace")
            if secret in output or base64.b64encode(secret.encode()).decode() in output:
                escapes.append("secret disclosed")
            if listener.accepted:
                escapes.append("reached the host listener")
            if "CONNECTED" in output or "ESCAPED" in output or "WROTE" in output:
                escapes.append("a behavior reported success")
            if remaining:
                escapes.append(f"processes survived stop: {remaining}")
            report["scenarios"] += 1
            report["behaviors_run"] += len(chosen)
            if escapes:
                report["escapes"].append({"scenario": index, "behaviors": chosen,
                                          "escapes": escapes, "output": output[-2000:]})
    finally:
        listener.close()
        secret_file.unlink()
        out = os.environ.get("SENTINEL_FUZZ_REPORT")
        if out:
            Path(out).write_text(json.dumps(report, indent=2) + "\n")
    assert report["scenarios"] == SCENARIOS
    assert report["escapes"] == [], json.dumps(report["escapes"], indent=2)
