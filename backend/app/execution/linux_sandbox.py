"""The verified Linux agent boundary: bubblewrap + seccomp + a cgroup v2 run group.

Launch flow (mirrors the Windows "start suspended, verify, then resume"):

1. A run cgroup is created with its limits (`cgroups.CgroupHierarchy`).
2. ``/bin/sh -c 'echo $$ > <run>/cgroup.procs && exec bwrap ...'`` puts the
   launcher into the cgroup *before* bwrap forks, so every sandbox process is
   born inside it (no move race, no thread-unsafe ``preexec_fn``).
3. bwrap unshares the user, mount, PID, IPC, UTS and cgroup namespaces (and the
   network namespace unless the profile has network), drops all capabilities,
   sets ``no_new_privs``, installs the `seccomp` filter, and runs a tiny shim
   that blocks reading its stdin, a pipe only the supervisor can write.
4. The supervisor verifies the live shim process from outside: every namespace
   differs from its own, ``Seccomp: 2`` with more filters than the supervisor
   has, ``NoNewPrivs: 1``, a one-line ``uid_map`` mapping only this user, and
   membership of exactly the run cgroup.
5. Only then does it write to the pipe; the shim ``exec``s the agent (same PID,
   same filter, same cgroup). Any mismatch kills the cgroup and fails closed.

What the agent can see: read-only ``/usr`` (and the ``/bin``, ``/lib*`` links),
a short list of ``/etc`` files needed for name resolution and TLS, private
``/proc``, ``/dev`` and ``/tmp``, the read-only tool directories, and
read-write only the workspace clone and staged home. Not the user's home,
``/run`` (no D-Bus or other sockets), ``/sys``, the Sentinel store or the host
``/tmp``.

Nothing falls back: missing bwrap, disabled user namespaces, no delegated
cgroup v2 subtree or a failed verification raise or return an ERROR run; no
unconfined retry exists (D-01).
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.app.contracts.models import DescendantProcess
from backend.app.core.errors import AppError
from backend.app.execution import seccomp
from backend.app.execution.cgroups import RunCgroup, process_cgroup

TRUSTED_BIN_DIRS = ("/usr/bin", "/bin", "/usr/local/bin")
BWRAP_ENV = "SENTINEL_BWRAP"
NAMESPACES = ("user", "mnt", "pid", "ipc", "uts", "cgroup", "net")
SHIM = 'read -r _sentinel_go || exit 125; exec "$@" </dev/null'
SHIM_NAME = "sentinel-shim"
SANDBOX_HOSTNAME = "sentinel-sandbox"
VERIFY_TIMEOUT_SECONDS = 10.0
_ETC_FILES = (
    "/etc/ssl", "/etc/ca-certificates", "/etc/pki", "/etc/resolv.conf", "/etc/hosts",
    "/etc/nsswitch.conf", "/etc/passwd", "/etc/group", "/etc/localtime",
    "/etc/alternatives", "/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d",
)
_ROOT_LINKS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")


def sandbox_unavailable(reason: str) -> AppError:
    return AppError("LINUX_SANDBOX_UNAVAILABLE",
                    f"The Linux sandbox cannot be established: {reason}",
                    status_code=501, details={"reason": reason})


def verification_failed(reason: str, facts: Mapping[str, Any] | None = None) -> AppError:
    return AppError("LINUX_SANDBOX_VERIFICATION_FAILED",
                    f"The Linux sandbox did not verify before the agent ran: {reason}",
                    status_code=500, details={"reason": reason, **({"facts": dict(facts)} if facts else {})})


# ---------------------------------------------------------------------- discovery


def find_bwrap(environ: Mapping[str, str] | None = None) -> Path:
    """bwrap from a trusted directory (never the caller's PATH), or the override."""
    source = os.environ if environ is None else environ
    override = source.get(BWRAP_ENV, "").strip()
    if override:
        path = Path(override)
        if not path.is_absolute() or not os.access(path, os.X_OK):
            raise sandbox_unavailable(f"{BWRAP_ENV}={override!r} is not an executable absolute path")
        return path
    for directory in TRUSTED_BIN_DIRS:  # absolute trusted locations only, never PATH
        candidate = Path(directory) / "bwrap"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise sandbox_unavailable("bubblewrap (bwrap) is not installed; install the 'bubblewrap' "
                              "package")


_PROBES: dict[str, tuple[bool, str, bool]] = {}
_PROBE_LOCK = threading.Lock()


def probe_bwrap(bwrap: Path) -> tuple[bool, str, bool]:
    """(works, detail, supports --disable-userns), cached per binary."""
    key = str(bwrap)
    with _PROBE_LOCK:
        if key in _PROBES:
            return _PROBES[key]
        try:
            help_text = subprocess.run([key, "--help"], capture_output=True, timeout=10,
                                       check=False).stdout.decode("utf-8", "replace")
            result = subprocess.run(
                [key, "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/",
                 "--proc", "/proc", "--dev", "/dev", "--", "/bin/true"],
                capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            outcome = (False, f"bwrap could not run ({exc})", False)
        else:
            detail = result.stderr.decode("utf-8", "replace").strip()[:500]
            outcome = (result.returncode == 0,
                       detail or f"exit {result.returncode}", "--disable-userns" in help_text)
        _PROBES[key] = outcome
        return outcome


def require_sandbox(environ: Mapping[str, str] | None = None) -> tuple[Path, bool]:
    """The bwrap binary and whether it supports --disable-userns, or fail closed."""
    if not sys.platform.startswith("linux"):
        raise sandbox_unavailable("the Linux sandbox runs only on Linux")
    try:
        seccomp.host_arch()
    except seccomp.SeccompUnsupported as exc:
        raise sandbox_unavailable(str(exc)) from exc
    bwrap = find_bwrap(environ)
    works, detail, disable_userns = probe_bwrap(bwrap)
    if not works:
        raise sandbox_unavailable(
            f"bwrap cannot create unprivileged namespaces here ({detail}); user namespaces "
            "may be disabled (kernel.unprivileged_userns_clone, AppArmor userns restriction)")
    return bwrap, disable_userns


# ---------------------------------------------------------------------- spec and argv


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    writable: tuple[Path, ...]  # workspace clone, staged home
    readonly: tuple[Path, ...] = ()  # tool snapshot directories
    network: bool = False

    def __post_init__(self) -> None:
        if not self.argv or not os.path.isabs(self.argv[0]):
            raise ValueError("the sandboxed executable must be an absolute path")
        for path in (self.cwd, *self.writable, *self.readonly):
            if not path.is_absolute():
                raise ValueError(f"sandbox paths must be absolute: {path}")
        if not any(self.cwd == item or item in self.cwd.parents for item in self.writable):
            raise ValueError("the working directory must be inside a writable bind")
        for key, value in self.env.items():
            if not key or "=" in key or "\0" in key or "\0" in value:
                raise ValueError(f"invalid environment entry {key!r}")


def bwrap_arguments(spec: SandboxSpec, *, bwrap: Path, seccomp_fd: int, info_fd: int,
                    disable_userns: bool) -> list[str]:
    # --unshare-all only *tries* the user namespace; demand it so a host that
    # cannot provide one fails here instead of running with the host's.
    arguments = [str(bwrap), "--unshare-all", "--unshare-user"]
    if spec.network:
        arguments.append("--share-net")
    arguments += ["--die-with-parent", "--new-session", "--cap-drop", "ALL",
                  "--hostname", SANDBOX_HOSTNAME]
    if disable_userns:
        arguments.append("--disable-userns")
    arguments += ["--ro-bind", "/usr", "/usr"]
    for link in _ROOT_LINKS:
        if os.path.islink(link):
            arguments += ["--symlink", os.readlink(link), link]
        elif os.path.isdir(link):
            arguments += ["--ro-bind", link, link]
    for entry in _ETC_FILES:
        arguments += ["--ro-bind-try", entry, entry]
    arguments += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for path in spec.readonly:
        arguments += ["--ro-bind", str(path), str(path)]
    for path in spec.writable:
        arguments += ["--bind", str(path), str(path)]
    arguments += ["--chdir", str(spec.cwd), "--clearenv"]
    for key, value in sorted(spec.env.items()):
        arguments += ["--setenv", key, value]
    arguments += ["--seccomp", str(seccomp_fd), "--info-fd", str(info_fd),
                  "--", "/bin/sh", "-c", SHIM, SHIM_NAME, *spec.argv]
    return arguments


# ---------------------------------------------------------------------- /proc facts


def _status(pid: int, proc_root: Path = Path("/proc")) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in (proc_root / str(pid) / "status").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition(":")
        result[key.strip()] = value.strip()
    return result


def namespace_ids(pid: int | str, proc_root: Path = Path("/proc")) -> dict[str, str]:
    ids: dict[str, str] = {}
    for name in NAMESPACES:
        try:
            ids[name] = os.readlink(proc_root / str(pid) / "ns" / name)
        except OSError:
            ids[name] = ""
    return ids


def children_of(pid: int, proc_root: Path = Path("/proc")) -> list[int]:
    found: list[int] = []
    try:
        for task in (proc_root / str(pid) / "task").iterdir():
            text = (task / "children").read_text(encoding="utf-8")
            found.extend(int(item) for item in text.split())
    except (OSError, ValueError):
        return []
    return sorted(set(found))


@dataclass(frozen=True, slots=True)
class LinuxSandboxFacts:
    """What was observed on the live shim process before it was released."""

    pid: int
    namespaces: dict[str, str]
    host_namespaces: dict[str, str]
    seccomp_mode: str
    seccomp_filters: int
    supervisor_seccomp_filters: int
    no_new_privs: str
    uid_map: tuple[str, ...]
    cgroup: str | None
    expected_cgroup: str
    network_isolated: bool
    verified: bool
    failures: tuple[str, ...] = field(default_factory=tuple)
    sandbox_kind: str = "linux_sandbox"

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["uid_map"] = list(self.uid_map)
        payload["failures"] = list(self.failures)
        return payload


def observe_facts(pid: int, *, expected_cgroup: str, network: bool,
                  proc_root: Path = Path("/proc")) -> LinuxSandboxFacts:
    """Read and judge the boundary facts of ``pid`` (never raises for a mismatch)."""
    failures: list[str] = []
    own = namespace_ids("self", proc_root)
    theirs = namespace_ids(pid, proc_root)
    required = [name for name in NAMESPACES if name != "net" or not network]
    for name in required:
        if not theirs.get(name) or theirs[name] == own.get(name):
            failures.append(f"{name} namespace is not separate")
    if network and theirs.get("net") and theirs["net"] != own.get("net"):
        failures.append("net namespace is separate although the profile has network")
    try:
        status = _status(pid, proc_root)
        mine = _status(os.getpid(), proc_root)
    except OSError as exc:
        status, mine = {}, {}
        failures.append(f"status unreadable ({exc.strerror})")
    seccomp_mode = status.get("Seccomp", "")
    filters = int(status.get("Seccomp_filters", "0") or 0)
    own_filters = int(mine.get("Seccomp_filters", "0") or 0)
    if seccomp_mode != "2":
        failures.append(f"seccomp mode is {seccomp_mode or 'unknown'}, not 2 (filter)")
    if filters <= own_filters:
        failures.append("no seccomp filter was added by the sandbox")
    no_new_privs = status.get("NoNewPrivs", "")
    if no_new_privs != "1":
        failures.append("no_new_privs is not set")
    try:
        uid_map = tuple(" ".join(line.split()) for line in
                        (proc_root / str(pid) / "uid_map").read_text(encoding="utf-8").splitlines()
                        if line.strip())
    except OSError:
        uid_map = ()
    uid = os.getuid()
    if len(uid_map) != 1 or uid_map[0].split()[1:] != [str(uid), "1"]:
        failures.append("uid_map does not map exactly this user")
    cgroup = process_cgroup(pid, proc_root)
    if cgroup != expected_cgroup:
        failures.append("the process is not in the run cgroup")
    return LinuxSandboxFacts(
        pid=pid, namespaces=theirs, host_namespaces=own, seccomp_mode=seccomp_mode,
        seccomp_filters=filters, supervisor_seccomp_filters=own_filters,
        no_new_privs=no_new_privs, uid_map=uid_map, cgroup=cgroup,
        expected_cgroup=expected_cgroup, network_isolated=not network,
        verified=not failures, failures=tuple(failures),
    )


def verified_linux_facts(facts: Any) -> bool:
    """True only for a recorded payload whose own judgement says verified, re-checked."""
    if not isinstance(facts, Mapping) or facts.get("sandbox_kind") != "linux_sandbox":
        return False
    if facts.get("verified") is not True or facts.get("failures"):
        return False
    namespaces, host = facts.get("namespaces"), facts.get("host_namespaces")
    if not isinstance(namespaces, Mapping) or not isinstance(host, Mapping):
        return False
    required = [name for name in NAMESPACES
                if name != "net" or facts.get("network_isolated") is True]
    if any(not namespaces.get(name) or namespaces.get(name) == host.get(name) for name in required):
        return False
    try:
        filters_ok = int(facts.get("seccomp_filters")) > int(facts.get("supervisor_seccomp_filters"))
    except (TypeError, ValueError):
        return False
    return (facts.get("seccomp_mode") == "2" and filters_ok and facts.get("no_new_privs") == "1"
            and isinstance(facts.get("cgroup"), str) and facts.get("cgroup") == facts.get("expected_cgroup"))


# ---------------------------------------------------------------------- descendants


def _proc_identity(pid: int, proc_root: Path) -> tuple[int | None, str | None, str | None]:
    parent: int | None = None
    try:
        stat_text = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        parent = int(stat_text.rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        exe: str | None = os.readlink(proc_root / str(pid) / "exe")
    except OSError:
        exe = None
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
        cmdline = " ".join(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part) or None
    except OSError:
        cmdline = None
    return (parent if parent and parent > 0 else None), exe, cmdline


class CgroupSession:
    """Descendant attribution from cgroup membership (the Job Object session's twin)."""

    def __init__(self, cgroup: RunCgroup, top_level_pid: int, *, internal_pids: Sequence[int],
                 redact: Callable[[str], str], proc_root: Path = Path("/proc")) -> None:
        self.cgroup = cgroup
        self.job = cgroup  # truthy marker the shared launcher code checks
        self.top_level_pid = top_level_pid
        self._internal = set(internal_pids)
        self._redact = redact
        self._proc_root = proc_root
        self._observed: dict[int, DescendantProcess] = {}

    def observe(self) -> list[DescendantProcess]:
        now = datetime.now(UTC)
        try:
            current = set(self.cgroup.pids())
        except OSError:
            return self.records
        for pid in current:
            if pid == self.top_level_pid or pid in self._internal or pid in self._observed:
                continue
            parent, exe, cmdline = _proc_identity(pid, self._proc_root)
            attributed = exe is not None
            self._observed[pid] = DescendantProcess(
                pid=pid, parent_pid=parent, executable_path=exe,
                command_line=self._redact(cmdline) if cmdline else None,
                started_at=now, attributed=attributed,
                attribution_reason=None if attributed else
                "process exited before identity could be resolved",
            )
        for pid, record in list(self._observed.items()):
            if record.terminated_at is None and pid not in current:
                self._observed[pid] = record.model_copy(update={"terminated_at": now})
        return self.records

    @property
    def records(self) -> list[DescendantProcess]:
        return [self._observed[pid] for pid in sorted(self._observed)]

    def live_pids(self) -> list[int]:
        try:
            return self.cgroup.pids()
        except OSError:
            return []

    def terminate(self) -> int:
        self.observe()
        try:
            count = self.cgroup.kill()
        except (AppError, OSError):
            count = 0
        self.observe()
        return count

    def close(self) -> None:
        now = datetime.now(UTC)
        self.terminate()
        for pid, record in list(self._observed.items()):
            if record.terminated_at is None:
                self._observed[pid] = record.model_copy(update={"terminated_at": now})
        self.cgroup.remove()
        self.job = None


# ---------------------------------------------------------------------- process


class LinuxSandboxProcess:
    """``subprocess.Popen``-compatible handle for a verified sandboxed agent."""

    restricted_token_applied = False

    def __init__(self, popen: subprocess.Popen, *, pid: int, session: CgroupSession,
                 facts: LinuxSandboxFacts) -> None:
        self._popen = popen
        self.pid = pid
        self.stdout = popen.stdout
        self.stderr = popen.stderr
        self.session = session
        self.linux_sandbox = facts
        self.returncode: int | None = None

    def poll(self) -> int | None:
        code = self._popen.poll()
        if code is not None:
            self.returncode = code
        return code

    def wait(self, timeout: float | None = None) -> int:
        code = self._popen.wait(timeout)
        self.returncode = code
        return code

    def kill(self) -> None:
        self.session.terminate()
        if self._popen.poll() is None:
            try:
                self._popen.kill()
            except ProcessLookupError:
                pass

    def suspend(self) -> None:
        self.session.cgroup.freeze()

    def resume(self) -> None:
        self.session.cgroup.thaw()

    def close(self) -> None:
        self.session.observe()
        self.session.close()
        for stream in (self.stdout, self.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def _seccomp_fd() -> int:
    program = seccomp.build_filter()
    if hasattr(os, "memfd_create"):
        descriptor = os.memfd_create("sentinel-seccomp", 0)
    else:  # pragma: no cover - Linux always has memfd_create on supported kernels
        import tempfile

        descriptor, name = tempfile.mkstemp()
        os.unlink(name)
    os.write(descriptor, program)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return descriptor


def _read_info(descriptor: int, deadline: float) -> dict[str, Any]:
    buffer = b""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([descriptor], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        chunk = os.read(descriptor, 4096)
        if not chunk:
            break
        buffer += chunk
        try:
            return json.loads(buffer.decode("utf-8"))
        except ValueError:
            continue
    raise verification_failed("bwrap did not report the sandbox process")


def spawn_linux_sandbox(spec: SandboxSpec, cgroup: RunCgroup, *, redact: Callable[[str], str],
                        environ: Mapping[str, str] | None = None,
                        verify_timeout: float = VERIFY_TIMEOUT_SECONDS) -> LinuxSandboxProcess:
    """Start ``spec`` blocked, verify the live boundary, then release it; else fail closed."""
    bwrap, disable_userns = require_sandbox(environ)
    seccomp_fd = _seccomp_fd()
    info_read, info_write = os.pipe()
    block_read, block_write = os.pipe()
    popen: subprocess.Popen | None = None
    try:
        inner = bwrap_arguments(spec, bwrap=bwrap, seccomp_fd=seccomp_fd, info_fd=info_write,
                                disable_userns=disable_userns)
        launcher = ["/bin/sh", "-c", 'echo $$ > "$0" && exec "$@"', str(cgroup.procs_file), *inner]
        popen = subprocess.Popen(
            launcher, stdin=block_read, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(seccomp_fd, info_write), close_fds=True, start_new_session=True,
            cwd="/", env={"PATH": "/usr/bin:/bin"},
        )
    except OSError as exc:
        for descriptor in (seccomp_fd, info_read, info_write, block_read, block_write):
            os.close(descriptor)
        cgroup.remove()
        raise sandbox_unavailable(f"the sandbox launcher could not start ({exc.strerror})") from exc
    os.close(info_write)
    os.close(block_read)
    os.close(seccomp_fd)
    released = False
    try:
        deadline = time.monotonic() + verify_timeout
        try:
            info = _read_info(info_read, deadline)
        except AppError:
            if popen.poll() is None:
                raise
            stderr = popen.stderr.read().decode("utf-8", "replace")[:500] if popen.stderr else ""
            raise verification_failed(
                f"the sandbox exited during setup (exit {popen.returncode}: {stderr.strip()})")
        init_pid = int(info.get("child-pid", 0))
        if init_pid <= 0:
            raise verification_failed("bwrap reported no child pid")
        shim = None
        while time.monotonic() < deadline:
            if popen.poll() is not None:
                stderr = popen.stderr.read().decode("utf-8", "replace")[:500] if popen.stderr else ""
                raise verification_failed(f"the sandbox exited during setup ({stderr.strip()})")
            candidates = children_of(init_pid)
            if len(candidates) == 1:
                status = _status(candidates[0])
                if status.get("Seccomp") == "2" and status.get("State", "").startswith("S"):
                    shim = candidates[0]
                    break
            time.sleep(0.005)
        if shim is None:
            raise verification_failed("the blocked sandbox process did not appear")
        facts = observe_facts(shim, expected_cgroup=cgroup.relative, network=spec.network)
        if not facts.verified:
            raise verification_failed("; ".join(facts.failures), facts.to_payload())
        session = CgroupSession(cgroup, shim, internal_pids=(popen.pid, init_pid), redact=redact)
        os.write(block_write, b"go\n")
        released = True
        return LinuxSandboxProcess(popen, pid=shim, session=session, facts=facts)
    except BaseException:
        try:
            cgroup.kill(timeout=5.0)
        except (AppError, OSError):
            pass
        if popen.poll() is None:
            try:
                os.killpg(popen.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        popen.wait(5)
        for stream in (popen.stdout, popen.stderr):
            if stream is not None:
                stream.close()
        cgroup.remove()
        raise
    finally:
        os.close(info_read)
        # Released: the shim already read its line. Not released: closing makes
        # its read hit EOF, so it exits 125 without ever running the agent.
        os.close(block_write)
