"""SDK runtimes never expose user caches or use host fallbacks."""
from pathlib import Path
from dataclasses import replace
import pytest
from backend.app.core.errors import AppError
from backend.app.execution.check_runtime import RuntimeSnapshot, cargo_runtime, dotnet_runtime
from backend.app.execution.check_toolchains import RuntimeBuilders, resolve_check_runtime

@pytest.mark.parametrize("tool", ["cargo", "dotnet"])
def test_sdk_runtime_uses_snapshot_and_private_caches(tmp_path, tool):
    host = tmp_path / "host" / ("bin/cargo.exe" if tool == "cargo" else "dotnet.exe")
    snapshot = RuntimeSnapshot(tmp_path / "snapshot", "a" * 64, 1)
    builders = replace(RuntimeBuilders(), **{tool: lambda _: snapshot, "find_" + tool: lambda _: host, "rust_libraries": lambda _: None})
    resolved = resolve_check_runtime(tool, source_root=tmp_path / "repo", builders=builders)
    assert resolved.runtime.snapshots == (snapshot,)
    assert str(snapshot.path) in resolved.argv_prefix[0]
    assert resolved.runtime.scratch_env
    assert "RUSTUP_HOME" not in resolved.runtime.env
    assert "USERPROFILE" not in resolved.runtime.env
    assert "PATH" not in resolved.runtime.env
    assert all("/" not in folder and "\\" not in folder for folder in resolved.runtime.scratch_env.values())
    if tool == "cargo":
        assert resolved.runtime.env["CARGO_NET_OFFLINE"] == "true"
        assert resolved.runtime.env["RUSTC"].startswith(str(snapshot.path))
    else:
        assert resolved.runtime.env["DOTNET_MULTILEVEL_LOOKUP"] == "0"
        assert resolved.runtime.env["DOTNET_EnableDiagnostics"] == "0"

@pytest.mark.parametrize("tool", ["cargo", "dotnet"])
def test_missing_sdk_never_falls_back(tmp_path, tool):
    builders = replace(RuntimeBuilders(), **{"find_" + tool: lambda _: None})
    with pytest.raises(AppError, match="unavailable") as error:
        resolve_check_runtime(tool, builders=builders)
    assert error.value.code == "CHECK_RUNTIME_UNAVAILABLE"

@pytest.mark.parametrize("builder,name", [(cargo_runtime, "cargo.exe"), (dotnet_runtime, "dotnet.exe")])
def test_proxy_or_incomplete_install_is_refused(tmp_path, builder, name):
    executable = tmp_path / "bin" / name
    executable.parent.mkdir()
    executable.write_bytes(b"proxy")
    with pytest.raises(AppError) as error:
        builder(executable, root=tmp_path / "cache")
    assert error.value.code == "CHECK_RUNTIME_FAILED"


@pytest.mark.parametrize("tool", ["cargo", "dotnet"])
def test_linux_sdk_is_bound_readonly_with_private_caches(tmp_path, tool):
    from backend.app.execution.linux_check_box import LinuxToolFinders, linux_resolve_check_runtime
    install = tmp_path / "sdk"
    required = ("bin", "lib/rustlib") if tool == "cargo" else ("host", "sdk", "shared", "packs")
    for directory in required: (install / directory).mkdir(parents=True, exist_ok=True)
    host = install / ("bin/cargo" if tool == "cargo" else "dotnet")
    host.write_text("executable")
    if tool == "cargo": (install / "bin/rustc").write_text("compiler")
    finders = replace(LinuxToolFinders(), **{tool: lambda _: host})
    resolved = linux_resolve_check_runtime(tool, source_root=tmp_path / "repo", finders=finders)
    assert not resolved.runtime.snapshots
    assert resolved.runtime.readonly_binds[0].source == install
    assert resolved.runtime.executable == host
    assert resolved.runtime.scratch_env
    assert "PROGRAMFILES" not in resolved.runtime.scratch_env
    assert "USERPROFILE" not in resolved.runtime.scratch_env

@pytest.mark.parametrize("tool", ["cargo", "dotnet"])
def test_linux_sdk_cannot_expose_evidence_store(tmp_path, tool):
    from backend.app.execution.linux_check_box import LinuxToolFinders, linux_resolve_check_runtime
    install = tmp_path / "sdk"
    for name in (["bin", "lib/rustlib"] if tool == "cargo" else ["host", "sdk", "shared", "packs"]):
        (install / name).mkdir(parents=True, exist_ok=True)
    host = install / ("bin/cargo" if tool == "cargo" else "dotnet")
    host.write_text("binary")
    if tool == "cargo": (install / "bin/rustc").write_text("compiler")
    finders = replace(LinuxToolFinders(), **{tool: lambda _: host})
    with pytest.raises(AppError) as error:
        linux_resolve_check_runtime(tool, protected=[install / "store"], finders=finders)
    assert error.value.code == "CHECK_RUNTIME_UNAVAILABLE"
