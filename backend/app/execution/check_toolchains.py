"""Which confined runtime an allowlisted check executable runs under (Phase 5).

This is the single source of the verification allowlist. Every allowlisted name
maps either to a confined runtime (a cached snapshot granted to one check box)
or to a refusal:

- ``python``, ``python3`` and ``pytest`` run the snapshot of the requested
  (already trusted) interpreter: its stdlib plus its ``site-packages``.
  ``pytest`` becomes ``<snapshot python> -m pytest``.
- ``node`` runs the ``node.exe`` snapshot. ``npm`` is always
  ``<node snapshot> <npm-cli.js inside the node snapshot>``; ``pnpm`` and
  ``yarn`` are the JS entry inside the project's ``node_modules`` snapshot.
  A ``.cmd`` shim is never run (it needs ``cmd.exe``). A missing entry is
  ``CHECK_RUNTIME_UNAVAILABLE``; Sentinel never falls back to the host.
- ``go`` runs the snapshot of the host's whole ``GOROOT`` with its build cache
  and GOPATH in the box's scratch, offline (``GOPROXY=off``), local toolchain
  only, without cgo: a module whose dependencies are not vendored or already
  in the module is refused by Go itself rather than fetched.
- ``cargo`` runs the real Rust toolchain snapshot, with private caches and
  offline dependencies. Windows x64 MSVC uses a separate snapshot of its
  linker and Windows SDK libraries. Rustup proxies never run inside the box.
- ``dotnet`` runs its SDK, targeting packs and runtime snapshots with private
  CLI/NuGet directories, no diagnostics or reusable MSBuild nodes, and no network.
- ``uv`` and anything else have no confined runtime: ``CHECK_TOOLCHAIN_UNCONFINED`` (409). They run only on the existing
  restricted path, and only for an actor holding the ``checks.unconfined``
  delegation for that Change; such a run is recorded with boundary
  ``UNCONFINED``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.app.core.errors import AppError
from backend.app.execution._process import capture, minimal_environment
from backend.app.execution.check_box import BoxRuntime, python_box_runtime
from backend.app.execution.check_runtime import (
    PythonRuntime,
    RuntimeSnapshot,
    node_modules_snapshot,
    go_runtime,
    cargo_runtime,
    dotnet_runtime,
    windows_rust_libraries,
    node_runtime,
    python_runtime,
)
from backend.app.execution.resolve import find_executable

CHECKS_UNCONFINED_SCOPE = "checks.unconfined"
BOUNDARY_APPCONTAINER = "APPCONTAINER"
BOUNDARY_UNCONFINED = "UNCONFINED"

PYTHON_TOOLCHAIN = "python"
NODE_TOOLCHAIN = "node"
GO_TOOLCHAIN = "go"

# Allowlisted name -> confined toolchain.
CONFINED_TOOLCHAINS: dict[str, str] = {
    "python": PYTHON_TOOLCHAIN,
    "python3": PYTHON_TOOLCHAIN,
    "pytest": PYTHON_TOOLCHAIN,
    "node": NODE_TOOLCHAIN,
    "npm": NODE_TOOLCHAIN,
    "npm.cmd": NODE_TOOLCHAIN,
    "pnpm": NODE_TOOLCHAIN,
    "pnpm.cmd": NODE_TOOLCHAIN,
    "yarn": NODE_TOOLCHAIN,
    "yarn.cmd": NODE_TOOLCHAIN,
    "go": GO_TOOLCHAIN,
    "cargo": "cargo",
    "dotnet": "dotnet",
}
# Allowlisted, but with no confined runtime (opt-in only).
UNCONFINED_TOOLCHAINS = frozenset({"uv"})
ALLOWED_EXECUTABLES = frozenset(CONFINED_TOOLCHAINS) | UNCONFINED_TOOLCHAINS

# JS entry points, relative to the snapshot that holds them.
_NPM_ENTRY = ("node_modules", "npm", "bin", "npm-cli.js")
NODE_BOX_OPTIONS = "--preserve-symlinks --preserve-symlinks-main"
_PACKAGE_MANAGER_ENTRIES: dict[str, tuple[tuple[str, ...], ...]] = {
    "pnpm": (("pnpm", "bin", "pnpm.cjs"), ("pnpm", "bin", "pnpm.js")),
    "yarn": (("yarn", "bin", "yarn.js"),),
}


def check_toolchain_unconfined(executable: str) -> AppError:
    return AppError(
        "CHECK_TOOLCHAIN_UNCONFINED",
        "This toolchain has no confined check runtime. It runs only on the restricted "
        "unconfined path, and only with a delegated checks.unconfined authority for the Change.",
        status_code=409,
        details={"executable": executable, "required_scope": CHECKS_UNCONFINED_SCOPE},
    )


def check_runtime_unavailable(executable: str, reason: str) -> AppError:
    return AppError(
        "CHECK_RUNTIME_UNAVAILABLE",
        "The confined check runtime for this toolchain is unavailable; Sentinel does not "
        "fall back to the host.",
        status_code=409,
        details={"executable": executable, "reason": reason},
    )


def toolchain_of(executable: str) -> str | None:
    """The confined toolchain of an allowlisted name; None when it has none."""

    return CONFINED_TOOLCHAINS.get(executable)


def is_unconfined_toolchain(executable: str) -> bool:
    """True for a name that is allowlisted but has no confined runtime."""

    return executable in UNCONFINED_TOOLCHAINS


@dataclass(frozen=True, slots=True)
class ResolvedCheckRuntime:
    """A confined runtime plus the absolute argv prefix that starts the tool in the box."""

    toolchain: str
    runtime: BoxRuntime
    argv_prefix: tuple[str, ...]


def _find_host_tool(name: str, source_root: Path | None) -> Path | None:
    root = (Path(source_root).resolve() if source_root is not None
            else Path(sys.executable).resolve())
    return find_executable(name, minimal_environment(), root)


def _find_host_node(source_root: Path | None) -> Path | None:
    # Without a repository there is nothing to exclude; a file is never a PATH parent.
    root = (Path(source_root).resolve() if source_root is not None
            else Path(sys.executable).resolve())
    found = find_executable("node", minimal_environment(), root)
    if found is not None and os.name != "nt":
        # A POSIX `node` is often a link (/usr/local/bin/node -> .../bin/node); the
        # snapshot copies the real binary and refuses links by design.
        found = Path(os.path.realpath(found))
    return found


@dataclass(frozen=True, slots=True)
class RuntimeBuilders:
    """The snapshot builders; unit tests substitute fakes (real ones hash and copy)."""

    python: Callable[[Path], PythonRuntime] = python_runtime
    node: Callable[[Path], RuntimeSnapshot] = node_runtime
    node_modules: Callable[[Path], RuntimeSnapshot | None] = node_modules_snapshot
    find_node: Callable[[Path | None], Path | None] = _find_host_node
    go: Callable[[Path], RuntimeSnapshot] = go_runtime
    find_go: Callable[[Path | None], Path | None] = lambda root: _find_host_tool("go", root)
    cargo: Callable[[Path], RuntimeSnapshot] = cargo_runtime
    find_cargo: Callable[[Path | None], Path | None] = lambda root: _find_real_cargo(root)
    dotnet: Callable[[Path], RuntimeSnapshot] = dotnet_runtime
    find_dotnet: Callable[[Path | None], Path | None] = lambda root: _find_host_tool("dotnet", root)
    rust_libraries: Callable[[Path], RuntimeSnapshot | None] = windows_rust_libraries

    @classmethod
    def for_root(cls, root: str | Path | None) -> RuntimeBuilders:
        """Builders that write snapshots under ``root`` (None: the default cache)."""

        if root is None:
            return cls()
        cache = Path(root)
        return cls(
            python=lambda interpreter: python_runtime(interpreter, root=cache),
            node=lambda node: node_runtime(node, root=cache),
            node_modules=lambda repo: node_modules_snapshot(repo, root=cache),
            go=lambda go: go_runtime(go, root=cache),
            cargo=lambda cargo: cargo_runtime(cargo, root=cache),
            dotnet=lambda dotnet: dotnet_runtime(dotnet, root=cache),
            rust_libraries=lambda cargo: windows_rust_libraries(cargo, root=cache),
        )


def _node_entry(base: Path, parts: tuple[str, ...]) -> Path | None:
    candidate = base.joinpath(*parts)
    return candidate if candidate.is_file() else None


def resolve_check_runtime(
    executable: str, *, interpreter: str | Path | None = None,
    source_root: str | Path | None = None, builders: RuntimeBuilders | None = None,
) -> ResolvedCheckRuntime:
    """The confined runtime and argv prefix for ``executable``, or a named refusal.

    ``interpreter`` is the requested Python (default: this process's own
    interpreter); the caller must already have accepted it as trusted.
    ``source_root`` is the user repository, read only for its ``node_modules``.
    """

    builders = builders or RuntimeBuilders()
    toolchain = CONFINED_TOOLCHAINS.get(executable)
    if toolchain is None:
        raise check_toolchain_unconfined(executable)
    if toolchain == PYTHON_TOOLCHAIN:
        requested = Path(interpreter) if interpreter is not None else Path(sys.executable)
        runtime = python_box_runtime(builders.python(requested))
        python = str(runtime.executable)
        prefix = (python, "-m", "pytest") if executable == "pytest" else (python,)
        return ResolvedCheckRuntime(toolchain, runtime, prefix)
    if toolchain == GO_TOOLCHAIN:
        return _go_runtime(executable, source_root, builders)
    if toolchain in {"cargo", "dotnet"}:
        return _sdk_runtime(toolchain, source_root, builders)

    source = Path(source_root) if source_root is not None else None
    host_node = builders.find_node(source)
    if host_node is None:
        raise check_runtime_unavailable(executable, "node was not found outside the repository")
    node = builders.node(Path(host_node))
    modules = builders.node_modules(source) if source is not None else None
    node_exe = node.path / Path(host_node).name
    snapshots = (node,) if modules is None else (node, modules)
    path_entries = (node.path,) if modules is None else (node.path, modules.path / ".bin")
    # Node's JS realpath walks every path component from the drive root, and an
    # AppContainer is denied lstat('C:\\') (EPERM before any user code runs). The
    # check tree and both snapshots refuse links and reparse points, so a path is
    # already its real path; preserving symlinks skips that walk without changing
    # module identity. NODE_OPTIONS reaches the node children npm spawns too.
    env = {"NODE_OPTIONS": NODE_BOX_OPTIONS}
    if modules is not None:
        env["NODE_PATH"] = str(modules.path)
    limitations = (() if modules is None else (
        "node_modules is a snapshot outside the check tree; it is reachable through "
        "NODE_PATH (CommonJS) only, not ES module resolution",))
    runtime = BoxRuntime(snapshots=snapshots, env=env, path_entries=path_entries,
                         executable=node_exe, limitations=limitations)
    base = executable.lower().removesuffix(".cmd")
    if base == "node":
        return ResolvedCheckRuntime(toolchain, runtime, (str(node_exe),))
    if base == "npm":
        entry = _node_entry(node.path, _NPM_ENTRY)
        if entry is None:
            raise check_runtime_unavailable(executable, "the node snapshot has no npm-cli.js")
        return ResolvedCheckRuntime(toolchain, runtime, (str(node_exe), str(entry)))
    if modules is None:
        raise check_runtime_unavailable(executable, "the project has no node_modules snapshot")
    for parts in _PACKAGE_MANAGER_ENTRIES[base]:
        entry = _node_entry(modules.path, parts)
        if entry is not None:
            return ResolvedCheckRuntime(toolchain, runtime, (str(node_exe), str(entry)))
    raise check_runtime_unavailable(
        executable, f"node_modules has no {base} JS entry point")


# Go in a box: offline, the snapshot toolchain only, caches in the box's scratch.
GO_BOX_ENV = {
    "GOTOOLCHAIN": "local", "GOPROXY": "off", "GOSUMDB": "off", "GOTELEMETRY": "off",
    "CGO_ENABLED": "0", "GOWORK": "off", "GOFLAGS": "-mod=readonly",
}
GO_SCRATCH_ENV = {"GOCACHE": "go-cache", "GOPATH": "go-path", "GOTMPDIR": "go-tmp"}


def _go_runtime(executable: str, source_root: str | Path | None,
                builders: RuntimeBuilders) -> ResolvedCheckRuntime:
    host_go = builders.find_go(Path(source_root) if source_root is not None else None)
    if host_go is None:
        raise check_runtime_unavailable(executable, "go was not found outside the repository")
    goroot = builders.go(Path(host_go))
    go_exe = goroot.path / "bin" / Path(host_go).name
    runtime = BoxRuntime(
        snapshots=(goroot,), env={**GO_BOX_ENV, "GOROOT": str(goroot.path)},
        path_entries=(goroot.path / "bin",), executable=go_exe, scratch_env=GO_SCRATCH_ENV,
        limitations=("Go runs offline: module dependencies must be vendored or already in the "
                     "module cache inside the box; cgo is disabled.",))
    return ResolvedCheckRuntime(GO_TOOLCHAIN, runtime, (str(go_exe),))


def _find_real_cargo(source_root: Path | None) -> Path | None:
    cargo = _find_host_tool("cargo", source_root)
    if cargo is None or cargo.suffix.lower() in {".bat", ".cmd"}:
        return None
    if (cargo.parent.parent / "lib" / "rustlib").is_dir():
        return cargo
    # Resolve rustup outside the repository: its rust-toolchain file must never
    # select or install a toolchain at host authority.
    rustup = _find_host_tool("rustup", source_root)
    if rustup is None:
        return None
    env = minimal_environment()
    env["RUSTUP_HOME"] = str(Path.home() / ".rustup")
    with tempfile.TemporaryDirectory(prefix="sentinel-rustup-") as scratch:
        result = capture([str(rustup), "which", "cargo"], cwd=scratch, env=env,
                         timeout=30, limit=65536)
    if result.returncode != 0 or result.truncated or result.timed_out or result.incomplete:
        return None
    resolved = Path(os.fsdecode(result.stdout).strip())
    if not resolved.is_absolute() or not resolved.is_file():
        return None
    resolved = resolved.resolve()
    if source_root is not None and (resolved == source_root.resolve()
                                   or source_root.resolve() in resolved.parents):
        return None
    return resolved


def _sdk_runtime(toolchain: str, source_root: str | Path | None,
                 builders: RuntimeBuilders, *, windows: bool | None = None) -> ResolvedCheckRuntime:
    windows = os.name == "nt" if windows is None else windows
    source = Path(source_root) if source_root is not None else None
    finder = builders.find_cargo if toolchain == "cargo" else builders.find_dotnet
    host = finder(source)
    if host is None:
        raise check_runtime_unavailable(toolchain, f"{toolchain} was not found outside the repository")
    snapshot = (builders.cargo if toolchain == "cargo" else builders.dotnet)(Path(host))
    snapshots = (snapshot,)
    if toolchain == "cargo":
        executable = snapshot.path / "bin" / Path(host).name
        suffix = Path(host).suffix
        env = {"CARGO_NET_OFFLINE": "true", "CARGO_INCREMENTAL": "0",
               "RUSTC": str(snapshot.path / "bin" / f"rustc{suffix}"),
               "RUSTDOC": str(snapshot.path / "bin" / f"rustdoc{suffix}")}
        scratch_env = {"CARGO_HOME": "cargo-home", "CARGO_TARGET_DIR": "cargo-target"}
        paths = (snapshot.path / "bin",)
        if windows:
            libraries = builders.rust_libraries(Path(host))
            if libraries is not None:
                linker = libraries.path / "bin/link.exe"
                if not linker.is_file():
                    raise check_runtime_unavailable(toolchain, "the native-library snapshot has no MSVC linker")
                env["CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER"] = str(linker)
                env["LIB"] = os.pathsep.join(str(libraries.path / name) for name in ("vc", "um", "ucrt"))
                snapshots += (libraries,)
                paths += (libraries.path / "bin",)
        limitations = ("Cargo runs offline; dependencies must be vendored. Native linking requires "
                       "a linker and libraries accessible inside the boundary.",)
    else:
        executable = snapshot.path / Path(host).name
        env = {"DOTNET_ROOT": str(snapshot.path), "DOTNET_MULTILEVEL_LOOKUP": "0",
               "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1",
               "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "DOTNET_EnableDiagnostics": "0",
               "DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE": "true",
               "DOTNET_ADD_GLOBAL_TOOLS_TO_PATH": "0", "MSBuildEnableWorkloadResolver": "false",
               "MSBUILDDISABLENODEREUSE": "1", "DOTNET_CLI_UI_LANGUAGE": "en"}
        scratch_env = {"DOTNET_CLI_HOME": "dotnet-home", "NUGET_PACKAGES": "nuget-packages",
                       "NUGET_HTTP_CACHE_PATH": "nuget-http", "NUGET_PLUGINS_CACHE_PATH": "nuget-plugins"}
        if windows:
            scratch_env.update({"USERPROFILE": "dotnet-home", "APPDATA": "dotnet-appdata",
                                "PROGRAMFILES": "dotnet-system", "PROGRAMFILES(X86)": "dotnet-system",
                                "PROGRAMDATA": "dotnet-data", "ALLUSERSPROFILE": "dotnet-data"})
        paths = (snapshot.path,)
        limitations = (".NET runs without network; restore requires a repository-local feed or "
                       "SDK-only dependencies. Host NuGet caches are not exposed.",)
    runtime = BoxRuntime(snapshots=snapshots, executable=executable, env=env,
                         path_entries=paths, scratch_env=scratch_env, limitations=limitations)
    return ResolvedCheckRuntime(toolchain, runtime, (str(executable),))
