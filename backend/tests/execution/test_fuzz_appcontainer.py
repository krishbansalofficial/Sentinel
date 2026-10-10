"""Seeded hostile Windows behaviors under the real AppContainer + Job Object boundary."""
from __future__ import annotations

import base64
import json
import os
import random
import secrets
import socket
import threading
import time

import pytest

from backend.app.execution.process_supervisor import IS_WINDOWS, is_process_running
from backend.tests.execution.test_appcontainer import node_exe, profile, _run

pytestmark = pytest.mark.skipif(not IS_WINDOWS, reason="AppContainers are Windows-only")
SEED = 20261004
SCENARIOS = int(os.environ.get("SENTINEL_FUZZ_SCENARIOS", "12"))


def test_hostile_scenario_sync_spawn_denial(profile, node_exe, tmp_path, monkeypatch):
    """Exercise CI's synchronous Node denial while retaining every escape assertion."""
    real_run = _run

    def deny_spawn(profile, node_exe, script, **kwargs):
        assert "detached:true" in script
        prefix = (
            "require('child_process').spawn=()=>{"
            "throw Object.assign(new Error('spawn EPERM'),{code:'EPERM'});};"
        )
        return real_run(profile, node_exe, prefix + script, **kwargs)

    monkeypatch.setattr(__name__ + "._run", deny_spawn)
    test_hostile_scenario(0, profile, node_exe, tmp_path)


@pytest.mark.parametrize("scenario", range(SCENARIOS))
def test_hostile_scenario(scenario, profile, node_exe, tmp_path):
    secret = secrets.token_hex(24)
    secret_file = tmp_path / "credential"
    secret_file.write_text(secret)
    contract = tmp_path / "contract.toml"
    contract.write_text("baseline")
    outside = tmp_path / "escape"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.1)
    accepted = []
    done = threading.Event()

    def listen():
        while not done.is_set():
            try:
                connection, _ = listener.accept()
                accepted.append(True)
                connection.close()
            except socket.timeout:
                pass

    thread = threading.Thread(target=listen)
    thread.start()
    behaviors = {
        "write_outside": f"try {{fs.writeFileSync({json.dumps(str(outside))},'escaped'); console.log('ESCAPED');}} catch(e) {{console.log('write_outside',e.code);}}",
        "contract": f"try {{fs.writeFileSync({json.dumps(str(contract))},'escaped'); console.log('ESCAPED');}} catch(e) {{console.log('contract',e.code);}}",
        "read_secret": f"try {{const s=fs.readFileSync({json.dumps(str(secret_file))}); console.log(s.toString(),s.toString('base64'));}} catch(e) {{console.log('read_secret',e.code);}}",
        "junction": f"try {{fs.symlinkSync({json.dumps(str(tmp_path))},'link','junction'); fs.writeFileSync('link/escape','escaped'); console.log('ESCAPED');}} catch(e) {{console.log('junction',e.code);}}",
        "local_api": f"const s=require('net').connect({listener.getsockname()[1]},'127.0.0.1'); s.on('connect',()=>{{console.log('ESCAPED');s.destroy();}}); s.on('error',()=>console.log('local_api denied')); s.setTimeout(1000,()=>s.destroy());",
        "detached": "try {const c=cp.spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{detached:true,stdio:'ignore',windowsHide:true}); c.on('error',e=>console.log('detached denied',e.code)); console.log('PID',c.pid); c.unref();} catch(e) {console.log('detached denied',e.code);}",
        "spawn_burst": "for(let i=0;i<12;i++){const c=cp.spawnSync(process.execPath,['-e','process.exit(0)'],{timeout:2000,stdio:'ignore',windowsHide:true}); console.log('spawn_burst',c.status===0?'exited':('denied '+(c.error?.code||c.status)));} console.log('spawn_burst done');",
    }
    names = random.Random(SEED + scenario).sample(sorted(behaviors), 4)
    script = "const fs=require('fs'),cp=require('child_process');" + "\n".join(behaviors[name] for name in names)
    # A positive control proves the program executed inside its writable container.
    script += "\nfs.writeFileSync('ran','yes');console.log('DONE');"
    try:
        result, process = _run(profile, node_exe, script, timeout=30)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        assert result.returncode == 0, output
        assert "DONE" in output
        assert (profile.container_path / "ran").read_text() == "yes"
        assert "ESCAPED" not in output, (scenario, names, output)
        assert secret not in output and base64.b64encode(secret.encode()).decode() not in output
        assert not outside.exists()
        assert contract.read_text() == "baseline"
        assert not accepted
        pids = [int(line.split()[1]) for line in output.splitlines() if line.startswith("PID ") and line.split()[1].isdigit()]
        pids.append(process.pid)
        deadline = time.monotonic() + 5
        while any(is_process_running(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not any(is_process_running(pid) for pid in pids), (scenario, names, pids)
    finally:
        done.set()
        thread.join(timeout=2)
        listener.close()
