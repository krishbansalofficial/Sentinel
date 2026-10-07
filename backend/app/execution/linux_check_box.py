"""Linux confined check boxes: the check-box seam filled with the verified Linux sandbox.

``CheckBoxes`` owns the row, the tree copy, the journal and cleanup on every
platform; this module supplies the Linux half of ``check_box.BoxPlatform``:

* Identity: a box ``sentinel.check.<run-id>`` is ``linux-sandbox:<name>`` (the
  same prefix a Linux workspace records), never an AppContainer SID.
* Storage: ``<store>/linux-checks/Packages/<name>/AC`` (0700), so the
  manager's layout and its DB-driven sweep are the same as on Windows.
* Grants: none. On Windows a box can read a runtime only after an ACE names
  its package SID, so runtimes are copied into a cache Sentinel grants on. In
  a mount namespace a path the box was not given simply does not exist, so the
  runtime is bound read-only (``BoxRuntime.readonly_binds``) and nothing is
  granted, revoked or cached.
* Spawn: one ``SandboxSpec`` per run: the box ``tree`` (cwd) and ``scratch``
  writable, the runtime binds read-only, network only when the box declared it
  (``internetClient``), all in a fresh run cgroup; ``spawn_linux_sandbox``
  verifies the live process before releasing it and returns its facts.

The resolver binds the host toolchain, never the repository's own: Python
binds the interpreter's venv and base prefix, Node its install prefix plus the
project's ``node_modules`` at ``tree/node_modules``, Go its GOROOT (offline,
caches in scratch). A bind that would expose ``/``, the user's home, the
repository or the Sentinel store is refused. Cargo and .NET bind their complete
SDK installs read-only and keep caches in scratch; uv remains refused. Nothing falls back to the host (D-01).
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.app.execution import appcontainer
from backend.app.execution.cgroups import CgroupHierarchy, RunLimits
from backend.app.execution.check_box import BoxPlatform, BoxRuntime, ReadonlyBind
from backend.app.execution.check_toolchains import (
    _PACKAGE_MANAGER_ENTRIES,
    CONFINED_TOOLCHAINS,
    GO_BOX_ENV,
    GO_SCRATCH_ENV,
    GO_TOOLCHAIN,
    NODE_TOOLCHAIN,
    PYTHON_TOOLCHAIN,
    ResolvedCheckRuntime,
    _find_host_node,
    _find_host_tool,
    _find_real_cargo,
    _sdk_runtime,
    RuntimeBuilders,
    check_runtime_unavailable,
    check_toolchain_unconfined,
)
from backend.app.execution.check_runtime import RuntimeSnapshot
from backend.app.execution.linux_sandbox import (
    LINUX_IDENTITY_PREFIX,
    SandboxSpec,
    require_sandbox,
    spawn_linux_sandbox,
    verification_failed,
)

LINUX_CHECKS_DIRECTORY = "linux-checks"
NETWORK_CAPABILITY = "internetClient"
TRUSTED_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
# bwrap already binds /usr read-only into every sandbox.
SYSTEM_PREFIX = Path("/usr")
HOME_FOLDER = ".home"
TMP_FOLDER = ".tmp"
PYTHON_RUNTIME_LIMITATION = (
    "The Python runtime is the host interpreter's own install, bound read-only into the "
    "box (not a digest-pinned snapshot).")
NODE_RUNTIME_LIMITATION = (
    "The Node runtime is the host install, bound read-only; the project's node_modules is "
    "bound read-only at tree/node_modules.")
GO_RUNTIME_LIMITATION = (
    "Go runs offline from the host GOROOT, bound read-only: module dependencies must be "
    "vendored or already in the module cache inside the box; cgo is disabled.")


@dataclass(frozen=True, slots=True)
class LinuxBoxProfile:
    """What ``ensure_profile`` returns: the shape ``CheckBoxes.open`` checks."""

    name: str
    package_sid: str
    container_path: Path


def linux_identity(name: str) -> str:
    return LINUX_IDENTITY_PREFIX + appcontainer.validate_profile_name(name)


# ---------------------------------------------------------------------- removal


def remove_tree(path: str | Path) -> None:
    """Delete ``path`` without following a link; repair modes a box run left behind.

    A check may ``chmod 000`` a folder in its own tree or scratch. Every entry
    there belongs to this user (the box's only uid maps to it), so the mode is
    restored on the real directory (lstat, never a link) and the removal
    retried once per entry. The box's processes are gone before this runs.
    """

    target = Path(path)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(target)
        return
    retried: set[str] = set()

    def onexc(_function: Any, failed: str, exc: BaseException) -> None:
        if isinstance(exc, FileNotFoundError):
            return
        if not isinstance(exc, PermissionError) or failed in retried:
            raise exc
        retried.add(failed)
        for item in (os.path.dirname(failed), failed):
            try:
                if stat.S_ISDIR(os.lstat(item).st_mode):
                    os.chmod(item, 0o700)
            except FileNotFoundError:
                pass
        try:
            mode = os.lstat(failed).st_mode
        except FileNotFoundError:
            return
        if stat.S_ISDIR(mode):
            shutil.rmtree(failed, onexc=onexc)
        else:
            os.unlink(failed)

    shutil.rmtree(target, onexc=onexc)


# ---------------------------------------------------------------------- platform


class LinuxBoxHost:
    """The Linux operations behind a check box: storage, environment and the sandbox."""

    def __init__(
        self, store_directory: str | Path, *,
        cgroups: Callable[[], CgroupHierarchy] | None = None,
        limits: RunLimits | None = None,
        spawn_sandbox: Callable[..., Any] = spawn_linux_sandbox,
        require: Callable[[], Any] = require_sandbox,
    ) -> None:
        self._root = Path(store_directory) / LINUX_CHECKS_DIRECTORY
        self._cgroups_factory = cgroups or CgroupHierarchy.from_environment
        self._hierarchy: CgroupHierarchy | None = None
        self._lock = threading.Lock()
        self._limits = limits or RunLimits()
        self._spawn = spawn_sandbox
        self._require = require

    @property
    def root(self) -> Path:
        return self._root

    def _checked_root(self) -> Path:
        if self._root.is_symlink():
            raise OSError(f"{self._root} is a symlink")
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self._root.lstat()
        if not stat.S_ISDIR(info.st_mode) or (
                hasattr(os, "geteuid") and info.st_uid != os.geteuid()):
            raise OSError(f"{self._root} is not a directory owned by this user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(self._root, 0o700)
        return self._root

    # -- identity and storage --------------------------------------------------

    def derive_package_sid(self, name: str) -> str:
        return linux_identity(name)

    def local_appdata(self) -> Path:
        return self._root

    def container_folder(self, package_sid: str) -> Path:
        if not package_sid.startswith(LINUX_IDENTITY_PREFIX):
            raise ValueError("not a Linux check box identity")
        name = appcontainer.validate_profile_name(package_sid[len(LINUX_IDENTITY_PREFIX):])
        return self._root / "Packages" / name / "AC"

    def ensure_profile(self, name: str, *, display_name: str = "") -> tuple[LinuxBoxProfile, bool]:
        appcontainer.validate_profile_name(name)
        packages = self._checked_root() / "Packages"
        if packages.is_symlink():
            raise OSError(f"{packages} is a symlink")
        packages.mkdir(mode=0o700, exist_ok=True)
        storage = packages / name
        if storage.is_symlink():
            raise OSError(f"{storage} is a symlink")
        created = not storage.exists()
        container = storage / "AC"
        container.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(storage, 0o700)
        os.chmod(container, 0o700)
        return LinuxBoxProfile(name, linux_identity(name), container), created

    def delete_profile(self, name: str) -> None:
        appcontainer.validate_profile_name(name)  # the folder is removed by the manager

    def profile_exists(self, name: str) -> bool:
        appcontainer.validate_profile_name(name)
        return os.path.lexists(self._root / "Packages" / name)

    def grant(self, path: str | Path, package_sid: str, *,
              allowed_root: str | Path | None = None) -> None:
        """Nothing to grant: the box sees a runtime only through its read-only bind."""

    def revoke(self, path: str | Path, package_sid: str, *,
               allowed_root: str | Path | None = None) -> None:
        """Nothing to revoke (see ``grant``)."""

    # -- environment -----------------------------------------------------------

    def base_environment(self, container: str | Path, *,
                         path_entries: Sequence[str | Path] = ()) -> dict[str, str]:
        """The complete environment of a box run; no host variable is copied.

        HOME and TMPDIR are folders of the box's own scratch (the sandbox's
        ``/tmp`` is a private tmpfs as well).
        """

        scratch = Path(container) / "scratch"
        home = scratch / HOME_FOLDER
        temp = scratch / TMP_FOLDER
        home.mkdir(exist_ok=True)
        temp.mkdir(exist_ok=True)
        path = [str(entry) for entry in path_entries] + list(TRUSTED_PATH)
        return {"PATH": ":".join(path), "HOME": str(home), "TMPDIR": str(temp),
                "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}

    # -- spawn -----------------------------------------------------------------

    def _hierarchy_ready(self) -> CgroupHierarchy:
        with self._lock:
            if self._hierarchy is None:
                hierarchy = self._cgroups_factory()
                hierarchy.prepare()
                self._hierarchy = hierarchy
            return self._hierarchy

    def spawn(
        self, argv: Sequence[str], *, cwd: str | Path, env: Mapping[str, str],
        redact: Callable[[str], str], profile_name: str, expected_package_sid: str,
        capabilities: Iterable[str] = (),
        readonly_binds: Sequence[tuple[str | Path, str | Path]] = (),
    ) -> Any:
        """Start ``argv`` in the box's sandbox, verified before it runs; else fail closed.

        Writable: only the box's ``tree`` (the cwd) and ``scratch``. Read-only:
        the runtime binds. The returned process carries ``.linux_sandbox``.
        """

        if expected_package_sid != linux_identity(profile_name):
            raise verification_failed("the box identity does not match its profile")
        container = self.container_folder(expected_package_sid)
        tree, scratch = container / "tree", container / "scratch"
        if Path(cwd) != tree:
            raise verification_failed("a box run must start in the box's tree")
        spec = SandboxSpec(
            argv=tuple(argv), cwd=tree, env=dict(env), writable=(tree, scratch),
            mounts=tuple((Path(source), Path(destination))
                         for source, destination in readonly_binds),
            network=NETWORK_CAPABILITY in tuple(capabilities),
        )
        self._require()  # no bwrap or user namespaces: refuse before a cgroup exists
        cgroup = self._hierarchy_ready().create_run(uuid4(), self._limits)
        return self._spawn(spec, cgroup, redact=redact)

    def platform(self) -> BoxPlatform:
        return BoxPlatform(
            derive_package_sid=self.derive_package_sid, ensure_profile=self.ensure_profile,
            delete_profile=self.delete_profile, profile_exists=self.profile_exists,
            container_folder=self.container_folder, local_appdata=self.local_appdata,
            base_environment=self.base_environment, spawn=self.spawn,
            grant=self.grant, revoke=self.revoke, remove_tree=remove_tree,
            runtime_cache=False,
        )


def linux_box_platform(store_directory: str | Path, **kwargs: Any) -> BoxPlatform:
    """The ``BoxPlatform`` of Linux check boxes stored under ``store_directory``."""

    return LinuxBoxHost(store_directory, **kwargs).platform()


# ---------------------------------------------------------------------- runtimes


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def runtime_bind(directory: str | Path, *, executable: str, exposes: Iterable[Path] = (),
                 protected: Iterable[Path] = ()) -> ReadonlyBind | None:
    """A read-only bind of the real ``directory``; None when /usr already provides it.

    Refused (``CHECK_RUNTIME_UNAVAILABLE``) for ``/``, a folder holding any of
    ``exposes`` (the user's home, the repository) and any folder overlapping
    ``protected`` (the Sentinel store) in either direction.
    """

    real = Path(os.path.realpath(directory))
    if not real.is_dir():
        raise check_runtime_unavailable(executable, "the runtime folder does not exist")
    if _within(real, SYSTEM_PREFIX):
        return None
    if real == Path(real.anchor):
        raise check_runtime_unavailable(executable, "the runtime is installed at /")
    for item in exposes:
        if _within(Path(os.path.realpath(item)), real):
            raise check_runtime_unavailable(
                executable, "binding the runtime would expose the home folder or repository")
    for item in protected:
        guarded = Path(os.path.realpath(item))
        if _within(guarded, real) or _within(real, guarded):
            raise check_runtime_unavailable(
                executable, "the runtime overlaps the Sentinel store")
    return ReadonlyBind(real)


def _unique(binds: Iterable[ReadonlyBind | None]) -> tuple[ReadonlyBind, ...]:
    seen: dict[tuple[Path, str | None], ReadonlyBind] = {}
    for bind in binds:
        if bind is not None:
            seen.setdefault((bind.source, bind.tree_relative), bind)
    return tuple(seen.values())


def _venv_home(cfg: Path) -> Path | None:
    try:
        lines = cfg.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip().lower() == "home" and value.strip():
            return Path(value.strip())
    return None


@dataclass(frozen=True, slots=True)
class LinuxToolFinders:
    """Host tool lookup; unit tests substitute fakes."""

    node: Callable[[Path | None], Path | None] = _find_host_node
    go: Callable[[Path | None], Path | None] = lambda root: _find_host_tool("go", root)
    cargo: Callable[[Path | None], Path | None] = _find_real_cargo
    dotnet: Callable[[Path | None], Path | None] = lambda root: _find_host_tool("dotnet", root)


def linux_resolve_check_runtime(
    executable: str, *, interpreter: str | Path | None = None,
    source_root: str | Path | None = None, protected: Iterable[str | Path] = (),
    finders: LinuxToolFinders | None = None,
) -> ResolvedCheckRuntime:
    """The Linux box runtime and argv prefix for ``executable``, or a named refusal.

    Same allowlist and refusals as ``resolve_check_runtime``; only the runtime
    differs (read-only binds instead of granted snapshots).
    """

    toolchain = CONFINED_TOOLCHAINS.get(executable)
    if toolchain is None:
        raise check_toolchain_unconfined(executable)
    finders = finders or LinuxToolFinders()
    source = Path(source_root).resolve() if source_root is not None else None
    exposes = [Path.home(), *([source] if source is not None else [])]
    guarded = [Path(item) for item in protected]

    def bind(directory: Path) -> ReadonlyBind | None:
        return runtime_bind(directory, executable=executable, exposes=exposes,
                            protected=guarded)

    if toolchain == PYTHON_TOOLCHAIN:
        return _python_runtime(executable, interpreter, bind)
    if toolchain in {"cargo", "dotnet"}:
        def install(host: Path) -> RuntimeSnapshot:
            directory = host.parent.parent if toolchain == "cargo" else host.parent
            required = ("bin/rustc", "lib/rustlib") if toolchain == "cargo" else ("host", "sdk", "shared", "packs")
            if not all((directory / name).exists() for name in required):
                raise check_runtime_unavailable(executable, "the executable is not inside a complete SDK")
            return RuntimeSnapshot(directory, "", 0)
        resolved = _sdk_runtime(toolchain, source, RuntimeBuilders(
            cargo=install, dotnet=install, find_cargo=finders.cargo, find_dotnet=finders.dotnet), windows=False)
        directory = resolved.runtime.snapshots[0].path
        return replace(resolved, runtime=replace(
            resolved.runtime, snapshots=(), readonly_binds=_unique([bind(directory)]),
            limitations=(*resolved.runtime.limitations, "The SDK is bound read-only from the host installation.")))
    if toolchain == GO_TOOLCHAIN:
        host_go = finders.go(source)
        if host_go is None:
            raise check_runtime_unavailable(executable, "go was not found outside the repository")
        go = Path(os.path.realpath(host_go))
        goroot = go.parent.parent
        if go.parent.name != "bin" or not (goroot / "src").is_dir():
            raise check_runtime_unavailable(executable, "go is not inside a GOROOT")
        runtime = BoxRuntime(
            env={**GO_BOX_ENV, "GOROOT": str(goroot)}, path_entries=(go.parent,),
            executable=go, scratch_env=GO_SCRATCH_ENV,
            readonly_binds=_unique([bind(goroot)]), limitations=(GO_RUNTIME_LIMITATION,))
        return ResolvedCheckRuntime(GO_TOOLCHAIN, runtime, (str(go),))
    return _node_runtime(executable, source, finders, bind)


def _python_runtime(executable: str, interpreter: str | Path | None,
                    bind: Callable[[Path], ReadonlyBind | None]) -> ResolvedCheckRuntime:
    requested = Path(interpreter) if interpreter is not None else Path(sys.executable)
    if not requested.is_absolute() or not requested.is_file() or not os.access(requested, os.X_OK):
        raise check_runtime_unavailable(executable, "the interpreter is not an executable file")
    # The venv (found next to the unresolved path, as Python itself finds it) and
    # the base install it points at; the real binary's own prefix as well.
    prefixes: list[Path] = []
    venv = requested.parent.parent
    home = _venv_home(venv / "pyvenv.cfg") if (venv / "pyvenv.cfg").is_file() else None
    if home is not None:
        prefixes += [venv, home.parent]
    prefixes.append(Path(os.path.realpath(requested)).parent.parent)
    runtime = BoxRuntime(
        env={"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        executable=requested, readonly_binds=_unique(bind(prefix) for prefix in prefixes),
        limitations=(PYTHON_RUNTIME_LIMITATION,))
    python = str(requested)
    prefix = (python, "-m", "pytest") if executable == "pytest" else (python,)
    return ResolvedCheckRuntime(PYTHON_TOOLCHAIN, runtime, prefix)


def _node_runtime(executable: str, source: Path | None, finders: LinuxToolFinders,
                  bind: Callable[[Path], ReadonlyBind | None]) -> ResolvedCheckRuntime:
    host_node = finders.node(source)
    if host_node is None:
        raise check_runtime_unavailable(executable, "node was not found outside the repository")
    node = Path(os.path.realpath(host_node))
    binds: list[ReadonlyBind | None] = [bind(node.parent.parent)]
    modules: Path | None = None
    if source is not None:
        candidate = source / "node_modules"
        if os.path.lexists(candidate):
            if candidate.is_symlink() or not candidate.is_dir():
                raise check_runtime_unavailable(
                    executable, "the project's node_modules is a link or not a folder")
            modules = candidate
            binds.append(ReadonlyBind(candidate, "node_modules"))
    runtime = BoxRuntime(path_entries=(node.parent,), executable=node,
                         readonly_binds=_unique(binds), limitations=(NODE_RUNTIME_LIMITATION,))
    base = executable.lower().removesuffix(".cmd")
    if base == "node":
        return ResolvedCheckRuntime(NODE_TOOLCHAIN, runtime, (str(node),))
    if base == "npm":
        for entry in (node.parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
                      node.parent.parent / "share" / "nodejs" / "npm" / "bin" / "npm-cli.js",
                      node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"):
            if entry.is_file():
                return ResolvedCheckRuntime(NODE_TOOLCHAIN, runtime, (str(node), str(entry)))
        raise check_runtime_unavailable(executable, "the node install has no npm-cli.js")
    if modules is None:
        raise check_runtime_unavailable(executable, "the project has no node_modules")
    for parts in _PACKAGE_MANAGER_ENTRIES[base]:
        if modules.joinpath(*parts).is_file():
            # Relative to the box's cwd (its tree), where node_modules is bound.
            return ResolvedCheckRuntime(NODE_TOOLCHAIN, runtime,
                                        (str(node), "/".join(("node_modules", *parts))))
    raise check_runtime_unavailable(executable, f"node_modules has no {base} JS entry point")
