"""Real SDK compilation and hostile checks through the live API, with host controls."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from pathlib import Path
import pytest
from backend.tests.acceptance.test_confined_checks import (
    LiveApi, _actor, _check_runs, _create_change, _verify, live_api, runtime_cache,
    HOSTED_RUNNER_CHECK_RUNTIME_GAP,
)
from backend.tests.support_kb import make_repo

pytestmark = [pytest.mark.skipif(os.name != "nt", reason="real Windows AppContainer required"),
              pytest.mark.real_check_boxes, HOSTED_RUNNER_CHECK_RUNTIME_GAP]

@pytest.mark.parametrize("tool", ["cargo", "dotnet"])
def test_sdk_compiles_checks_and_denies_escapes(live_api: LiveApi, tmp_path: Path, tool):
    executable = shutil.which(tool)
    if executable is None:
        pytest.skip(f"{tool} SDK not installed")
    api = live_api
    canary = tmp_path / "host-canary.txt"
    secret = api.store / "sdk-secret.txt"
    secret.write_text("private", encoding="utf-8")
    if tool == "cargo":
        code = r'''#[test]
fn checks() {
    assert_eq!(2 + 3, 5);
    let write = std::fs::write(CANARY, "escape").is_err();
    let read = std::fs::read(SECRET).is_err();
    let dial = std::net::TcpStream::connect_timeout(&"127.0.0.1:PORT".parse().unwrap(), std::time::Duration::from_secs(2)).is_err();
    println!("SENTINEL_SDK write={} read={} dial={}", write, read, dial);
}
'''.replace("CANARY", json.dumps(str(canary))).replace("SECRET", json.dumps(str(secret))).replace("PORT", str(api.port))
        files = {"Cargo.toml": '[package]\nname="sentinel-sdk-check"\nversion="0.1.0"\nedition="2021"\n', "src/lib.rs": code}
        args = ["test", "--offline", "--", "--nocapture"]
    else:
        code = r'''using System;
using System.IO;
using System.Net.Sockets;
if (2 + 3 != 5) throw new Exception("bad sum");
bool Denied(Action action) { try { action(); return false; } catch { return true; } }
var write = Denied(() => File.WriteAllText(CANARY, "escape"));
var read = Denied(() => File.ReadAllText(SECRET));
var dial = Denied(() => { using var c = new TcpClient(); c.ConnectAsync("127.0.0.1", PORT).Wait(TimeSpan.FromSeconds(2)); if (!c.Connected) throw new Exception("timeout"); });
Console.WriteLine($"SENTINEL_SDK write={write.ToString().ToLowerInvariant()} read={read.ToString().ToLowerInvariant()} dial={dial.ToString().ToLowerInvariant()}");
'''.replace("CANARY", json.dumps(str(canary))).replace("SECRET", json.dumps(str(secret))).replace("PORT", str(api.port))
        files = {"Checks.csproj": '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework><UseAppHost>false</UseAppHost></PropertyGroup></Project>', "Program.cs": code,
                 "NuGet.Config": '<configuration><packageSources><clear /></packageSources></configuration>'}
        args = ["run", "--project", "Checks.csproj", "--disable-build-servers"]
    repo = make_repo(tmp_path / "repo", files)
    host = subprocess.run([executable, *args], cwd=repo, capture_output=True, text=True, timeout=300)
    assert host.returncode == 0, host.stdout + host.stderr
    assert "SENTINEL_SDK write=false read=false dial=false" in host.stdout, host.stdout + host.stderr
    canary.unlink()
    # Exclude host build products: the confined tool must compile from source.
    subprocess.run(["git", "clean", "-fdx"], cwd=repo, check=True, capture_output=True)
    change = _create_change(api, repo)
    actor = _actor(api, change, ["change.legacy_verify"])
    response = _verify(api, change, actor, tool, args)
    assert response.status_code == 200, response.text
    result = response.json()["verification"]
    assert result["status"] == "PASSED", result["stdout"] + result["stderr"]
    assert "SENTINEL_SDK write=true read=true dial=true" in result["stdout"], result["stdout"] + result["stderr"]
    assert result["boundary"] == "APPCONTAINER"
    assert [run["boundary"] for run in _check_runs(api, change)] == ["APPCONTAINER"]
    assert not canary.exists()
    assert not (repo / "target").exists() and not (repo / "bin").exists()



def test_hidden_runner_uses_real_authenticated_verification_contract(live_api: LiveApi, tmp_path):
    from types import SimpleNamespace
    from backend.app.evals.hidden import VerificationHiddenTestRunner
    api = live_api
    repo = make_repo(tmp_path / "repo", {"mathops.py": "def add(a,b): return a+b\n"})
    change = _create_change(api, repo)
    actor = _actor(api, change, ["change.legacy_verify"])
    tree = tmp_path / "hidden-run"
    (tree / "hidden_tests").mkdir(parents=True)
    (tree / "hidden_tests/check.py").write_text("from mathops import add; assert add(2,3)==5; print('hidden-ok')")
    class Client:
        def get_change(self, identifier):
            return self._request("GET", f"/api/v1/changes/{identifier}")
        def _request(self, method, path, json_body=None):
            response = api.call(method, path.removeprefix("/api/v1"), **({"json": json_body} if json_body is not None else {}))
            assert response.status_code == 200, response.text
            return response.json()
    task = SimpleNamespace(hidden_test_command=("python", "-c", "exec(open('hidden_tests/check.py').read())"), timeout_seconds=60)
    result = VerificationHiddenTestRunner(Client()).run(task, tree, outcome=SimpleNamespace(change_id=change, actor_id=actor))
    assert result.passed
    assert result.boundary == "APPCONTAINER"
    assert "hidden-ok" in result.output_tail
