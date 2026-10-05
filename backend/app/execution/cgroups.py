"""cgroup v2 run groups for the Linux agent sandbox: limits, freeze, kill, attribution.

Each sandboxed launch gets its own cgroup, ``<parent>/sentinel-run-<run id>``,
with ``pids.max``, ``memory.max`` and (when the controller is delegated)
``cpu.max``. The whole tree is paused with ``cgroup.freeze`` and stopped with
``cgroup.kill``, so a double-forked or ``setsid`` daemon cannot escape either:
cgroup membership is inherited and cannot be left without write access to an
ancestor's ``cgroup.procs``, which the sandbox does not have.

The parent must be a cgroup this process may manage: ``SENTINEL_CGROUP_PARENT``
names it, else this process's own cgroup is used. Two cgroup v2 rules shape the
preparation (`CgroupHierarchy.prepare`):

* A cgroup with processes of its own cannot enable controllers for its
  children ("no internal processes"). If this process sits directly in the
  parent, it first moves itself into ``<parent>/sentinel-supervisor``.
* Moving a process between cgroups needs write access to ``cgroup.procs`` of
  their common ancestor, so the parent has to be a delegated subtree (systemd
  ``Delegate=yes``, or a directory an administrator ``chown``-ed to this user).

Nothing here falls back: no cgroup v2, a missing ``pids`` or ``memory``
controller, or an unwritable parent raises ``LINUX_CGROUP_UNAVAILABLE`` and the
launch fails closed (D-01).
"""

from __future__ import annotations

import os
import re
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from backend.app.core.errors import AppError

CGROUP_ROOT = Path("/sys/fs/cgroup")
PARENT_ENV = "SENTINEL_CGROUP_PARENT"
RUN_PREFIX = "sentinel-run-"
SUPERVISOR_LEAF = "sentinel-supervisor"
REQUIRED_CONTROLLERS = ("pids", "memory")
OPTIONAL_CONTROLLERS = ("cpu",)
_RUN_NAME = re.compile(r"^sentinel-run-[0-9a-f]{32}$")


def cgroup_unavailable(reason: str) -> AppError:
    return AppError(
        "LINUX_CGROUP_UNAVAILABLE",
        f"A delegated cgroup v2 subtree is required for the Linux sandbox: {reason}",
        status_code=501, details={"reason": reason},
    )


@dataclass(frozen=True, slots=True)
class RunLimits:
    pids_max: int = 512
    memory_max_bytes: int = 4 * 1024 ** 3
    cpu_max_percent: int | None = 200  # percent of one CPU; None leaves cpu.max unset

    def __post_init__(self) -> None:
        if self.pids_max < 1 or self.memory_max_bytes < 16 * 1024 ** 2:
            raise ValueError("cgroup limits are too small to run anything")
        if self.cpu_max_percent is not None and self.cpu_max_percent < 1:
            raise ValueError("cpu_max_percent must be positive")


def own_cgroup(proc_root: Path = Path("/proc")) -> str:
    """This process's cgroup v2 path (the ``0::`` line of /proc/self/cgroup)."""
    for line in (proc_root / "self" / "cgroup").read_text(encoding="utf-8").splitlines():
        if line.startswith("0::"):
            return line[3:].strip() or "/"
    raise cgroup_unavailable("this process has no cgroup v2 membership")


def process_cgroup(pid: int, proc_root: Path = Path("/proc")) -> str | None:
    try:
        text = (proc_root / str(pid) / "cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            return line[3:].strip()
    return None


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY)
    try:
        os.write(descriptor, value.encode("ascii"))
    finally:
        os.close(descriptor)


def parse_keyed(text: str) -> dict[str, str]:
    """``key value`` lines (cgroup.events, memory.events, cpu.stat)."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key:
            result[key] = value.strip()
    return result


class CgroupHierarchy:
    """The delegated parent under which run cgroups are created."""

    def __init__(self, parent: Path, *, root: Path = CGROUP_ROOT,
                 proc_root: Path = Path("/proc")) -> None:
        self.root = root
        self.parent = parent
        self._proc_root = proc_root
        self.controllers: tuple[str, ...] = ()
        self._prepared = False

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None, *,
                         root: Path = CGROUP_ROOT,
                         proc_root: Path = Path("/proc")) -> CgroupHierarchy:
        source = os.environ if environ is None else environ
        if not (root / "cgroup.controllers").is_file():
            raise cgroup_unavailable(f"{root} is not a cgroup v2 (unified) mount")
        configured = source.get(PARENT_ENV, "").strip()
        if configured:
            parent = Path(configured)
            if not parent.is_absolute():
                raise cgroup_unavailable(f"{PARENT_ENV} must be an absolute path")
        else:
            parent = root / own_cgroup(proc_root).lstrip("/")
        resolved = Path(os.path.realpath(parent))
        if resolved != Path(os.path.realpath(root)) and Path(os.path.realpath(root)) not in resolved.parents:
            raise cgroup_unavailable(f"{parent} is not inside {root}")
        return cls(resolved, root=root, proc_root=proc_root)

    def check(self) -> None:
        """Read-only feasibility check (for capability reporting): never moves or writes."""
        for name in ("cgroup.procs", "cgroup.subtree_control"):
            if not os.access(self.parent / name, os.W_OK):
                raise cgroup_unavailable(f"{self.parent / name} is not writable by this user")
        available = _read(self.parent / "cgroup.controllers").split()
        missing = [name for name in REQUIRED_CONTROLLERS if name not in available]
        if missing:
            raise cgroup_unavailable(f"controllers {', '.join(missing)} are not delegated to "
                                     f"{self.parent}")

    def prepare(self) -> tuple[str, ...]:
        """Move out of the parent if needed and enable the controllers (idempotent)."""
        if self._prepared:
            return self.controllers
        for name in ("cgroup.procs", "cgroup.subtree_control"):
            if not os.access(self.parent / name, os.W_OK):
                raise cgroup_unavailable(f"{self.parent / name} is not writable by this user")
        available = _read(self.parent / "cgroup.controllers").split()
        missing = [name for name in REQUIRED_CONTROLLERS if name not in available]
        if missing:
            raise cgroup_unavailable(f"controllers {', '.join(missing)} are not delegated to "
                                     f"{self.parent}")
        wanted = [*REQUIRED_CONTROLLERS,
                  *(name for name in OPTIONAL_CONTROLLERS if name in available)]
        if _read(self.parent / "cgroup.procs").split():
            leaf = self.parent / SUPERVISOR_LEAF
            leaf.mkdir(exist_ok=True)
            # Every process directly in the parent moves into the leaf; only our
            # own can be expected here, but any other one would block enabling.
            for pid in _read(self.parent / "cgroup.procs").split():
                try:
                    _write(leaf / "cgroup.procs", pid)
                except OSError as exc:
                    raise cgroup_unavailable(
                        f"process {pid} could not leave {self.parent} ({exc.strerror})") from exc
        enabled = set(_read(self.parent / "cgroup.subtree_control").split())
        to_enable = [name for name in wanted if name not in enabled]
        if to_enable:
            try:
                _write(self.parent / "cgroup.subtree_control",
                       " ".join(f"+{name}" for name in to_enable))
            except OSError as exc:
                raise cgroup_unavailable(
                    f"controllers could not be enabled under {self.parent} ({exc.strerror})") from exc
        enabled = set(_read(self.parent / "cgroup.subtree_control").split())
        missing = [name for name in REQUIRED_CONTROLLERS if name not in enabled]
        if missing:
            raise cgroup_unavailable(f"controllers {', '.join(missing)} did not enable")
        self.controllers = tuple(name for name in wanted if name in enabled)
        self._prepared = True
        return self.controllers

    def create_run(self, run_id: UUID, limits: RunLimits) -> RunCgroup:
        self.prepare()
        path = self.parent / f"{RUN_PREFIX}{run_id.hex}"
        try:
            path.mkdir()
        except FileExistsError as exc:
            raise cgroup_unavailable(f"{path} already exists") from exc
        run = RunCgroup(path, self.root, proc_root=self._proc_root)
        try:
            _write(path / "pids.max", str(limits.pids_max))
            _write(path / "memory.max", str(limits.memory_max_bytes))
            if limits.cpu_max_percent is not None and "cpu" in self.controllers:
                period = 100_000
                _write(path / "cpu.max", f"{limits.cpu_max_percent * period // 100} {period}")
        except OSError as exc:
            run.remove(timeout=2.0)
            raise cgroup_unavailable(f"limits could not be written to {path} ({exc.strerror})") from exc
        return run

    def leftover_runs(self) -> list[RunCgroup]:
        """Run cgroups from earlier processes (crash recovery sweeps these)."""
        try:
            entries = sorted(self.parent.iterdir())
        except OSError:
            return []
        return [RunCgroup(entry, self.root, proc_root=self._proc_root)
                for entry in entries if entry.is_dir() and _RUN_NAME.fullmatch(entry.name)]


class RunCgroup:
    """One run's cgroup: membership, limits, freeze, kill, removal."""

    def __init__(self, path: Path, root: Path = CGROUP_ROOT, *,
                 proc_root: Path = Path("/proc")) -> None:
        self.path = path
        self.root = root
        self._proc_root = proc_root

    @property
    def relative(self) -> str:
        """The path as /proc/<pid>/cgroup reports it (``/a/b``)."""
        return "/" + self.path.relative_to(self.root).as_posix()

    @property
    def procs_file(self) -> Path:
        return self.path / "cgroup.procs"

    def pids(self) -> list[int]:
        """Every PID in this cgroup and any child cgroup."""
        found: list[int] = []
        for directory, _subdirs, files in os.walk(self.path):
            if "cgroup.procs" in files:
                try:
                    found.extend(int(item) for item in _read(Path(directory) / "cgroup.procs").split())
                except (OSError, ValueError):
                    continue
        return sorted(set(found))

    def events(self) -> dict[str, str]:
        try:
            return parse_keyed(_read(self.path / "cgroup.events"))
        except OSError:
            return {}

    def populated(self) -> bool:
        return self.events().get("populated") == "1" or bool(self.pids())

    def frozen(self) -> bool:
        return self.events().get("frozen") == "1"

    def pids_events(self) -> dict[str, str]:
        try:
            return parse_keyed(_read(self.path / "pids.events"))
        except OSError:
            return {}

    def _wait(self, predicate, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def freeze(self, timeout: float = 5.0) -> None:
        _write(self.path / "cgroup.freeze", "1")
        if not self._wait(self.frozen, timeout):
            raise AppError("LINUX_CGROUP_FREEZE_FAILED",
                           "The sandbox process tree did not freeze in time.", status_code=500)

    def thaw(self, timeout: float = 5.0) -> None:
        _write(self.path / "cgroup.freeze", "0")
        if not self._wait(lambda: not self.frozen(), timeout):
            raise AppError("LINUX_CGROUP_FREEZE_FAILED",
                           "The sandbox process tree did not resume in time.", status_code=500)

    def kill(self, timeout: float = 10.0) -> int:
        """SIGKILL every process in the tree; returns how many were present."""
        present = len(self.pids())
        if (self.path / "cgroup.kill").exists():
            _write(self.path / "cgroup.kill", "1")
        else:  # kernels before 5.14: freeze so nothing forks, kill each, thaw
            try:
                self.freeze(timeout=timeout)
            except AppError:
                pass
            for pid in self.pids():
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            _write(self.path / "cgroup.freeze", "0")
        if not self._wait(lambda: not self.pids(), timeout):
            raise AppError("LINUX_CGROUP_KILL_FAILED",
                           "Processes remained in the sandbox cgroup after kill.", status_code=500)
        return present

    def remove(self, timeout: float = 10.0) -> bool:
        """Kill anything left, then rmdir (children first). True when gone."""
        if not self.path.exists():
            return True
        try:
            if self.pids():
                self.kill(timeout=timeout)
        except (AppError, OSError):
            pass
        for directory, _subdirs, _files in sorted(os.walk(self.path), key=lambda item: -len(item[0])):
            try:
                os.rmdir(directory)
            except OSError:
                pass
        return not self.path.exists()
