"""Linux confined check boxes on a real kernel, through the real HTTP API.

The Linux twin of ``acceptance/test_confined_checks.py``: a live uvicorn server
runs ``create_app`` with the Linux check-box layer (bubblewrap + seccomp + a
run cgroup per check); every check is requested over HTTP. Each escape
attempt has a positive control first: the same agent-authored ``conftest.py``
run by pytest on the host, at user authority, in a copy of the repository
outside Sentinel, must succeed. Only then does the confined denial mean
anything.

In a mount namespace a path the box was not given does not exist. A write to
such a path either fails or lands on the sandbox's private tmpfs, never on the
host, so writes are judged by the host: no canary may land. Reads and listings
of the store, the repository and the user's home must not reveal anything.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from backend.tests.acceptance.test_confined_checks import (
    LiveApi,
    _actor,
    _create_change,
    _free_port,
    _RequestLog,
    _StandInListener,
    _verify,
)
from backend.tests.execution.linux.conftest import (
    PARENT,
    requires_controllers,
    requires_linux_sandbox,
)
from backend.tests.support_kb import git, make_repo, write

pytestmark = [requires_linux_sandbox, pytest.mark.real_check_boxes]

PROBE_MARKER = "SENTINEL_PROBE "
INTERNET = ("1.1.1.1", 443)
PYTEST_ARGS = ["-s", "-q", "-p", "no:cacheprovider", "tests"]
PY_FILES = {
    ".gitignore": ".env\n__pycache__/\n.pytest_cache/\n",
    "calc.py": "def add(left, right):\n    return left + right\n",
    "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
}

HOSTILE_CONFTEST = r'''# Agent-authored conftest.py: pytest imports it before collecting any test.
import json
import os
import socket

CONFIG = json.loads(r"""__CONFIG__""")
RESULTS = {"cwd": os.getcwd(), "tree_has_ignored_env": os.path.exists(".env")}


def _attempt(name, action):
    try:
        value = action()
        RESULTS[name] = "allowed" if value is None else value
    except Exception as exc:  # the box's denial is the expected outcome
        RESULTS[name] = f"denied:{type(exc).__name__}:{getattr(exc, 'errno', '') or ''}"


def _write(path):
    def action():
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("escaped from a check")
    return action


def _read(path):
    def action():
        with open(path, "rb") as handle:
            handle.read(64)
    return action


def _list(path):
    return lambda: "listed:" + ",".join(sorted(os.listdir(path)))


def _connect(port):
    def action():
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
    return action


def _internet():
    with socket.create_connection((CONFIG["internet_host"], CONFIG["internet_port"]), timeout=8):
        pass


for label, target in CONFIG.get("writes", {}).items():
    _attempt("write_" + label, _write(target))
for label, target in CONFIG.get("reads", {}).items():
    _attempt("read_" + label, _read(target))
for label, target in CONFIG.get("lists", {}).items():
    _attempt("list_" + label, _list(target))
for label, port in CONFIG.get("ports", {}).items():
    _attempt("connect_" + label, _connect(port))
if CONFIG.get("internet_host"):
    _attempt("internet", _internet)

print("SENTINEL_PROBE " + json.dumps(RESULTS, sort_keys=True), flush=True)
'''


# ------------------------------------------------------------------ live API


@pytest.fixture
def live_api(tmp_path, monkeypatch, hierarchy_factory):
    import httpx
    import uvicorn

    import backend.app.main as main
    from backend.app.core.config import Settings
    from backend.app.core.evidence_store import prepare_store_directory
    from backend.app.credentials.memory_store import InMemoryCredentialStore
    from backend.app.execution.linux_check_box import (
        linux_box_platform,
        linux_resolve_check_runtime,
    )

    store = tmp_path / "Sentinel"
    prepare_store_directory(store)
    (store / "trusted_keys.json").write_text('{"schema_version": 1, "keys": []}\n',
                                             encoding="utf-8")
    # The production Linux layer; only the cgroup parent is this test's subtree.
    monkeypatch.setattr(main, "_check_box_layer", lambda directory: {
        "platform": linux_box_platform(directory, cgroups=hierarchy_factory),
        "resolver": partial(linux_resolve_check_runtime, protected=(directory,)),
    })
    database = store / "sentinel.sqlite3"
    app = main.create_app(settings=Settings(database_path=database),
                          credential_store=InMemoryCredentialStore())
    log = _RequestLog(app)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(log, host="127.0.0.1", port=port, lifespan="on",
                                          log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "live API did not start"
        time.sleep(0.05)
    listener = _StandInListener()
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", timeout=600,
                          headers={"Authorization": f"Bearer {app.state.api_token}"})
    api = LiveApi(base=f"http://127.0.0.1:{port}", port=port, store=store, database=database,
                  log=log, listener=listener, app=app, client=client)
    try:
        yield api
    finally:
        client.close()
        server.should_exit = True
        thread.join(30)
        listener.close()
        report = app.state.check_boxes.sweep(live_run_ids=())
        assert not report.failed, report


# ------------------------------------------------------------------ helpers


def _probe_line(stdout: str) -> dict:
    lines = [line for line in stdout.splitlines() if line.startswith(PROBE_MARKER)]
    assert len(lines) == 1, stdout
    return json.loads(lines[0][len(PROBE_MARKER):])


def _conftest(config: dict) -> str:
    return HOSTILE_CONFTEST.replace("__CONFIG__", json.dumps(config))


def _host_pytest(repo: Path, destination: Path) -> dict:
    copy = shutil.copytree(repo, destination, ignore=shutil.ignore_patterns(".git"))
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PYTEST_", "COV_"))}
    completed = subprocess.run([sys.executable, "-m", "pytest", *PYTEST_ARGS], cwd=copy,
                               capture_output=True, text=True, timeout=300, env=env)
    return _probe_line(completed.stdout)


def _journal(api: LiveApi, change_id: str, event_type: str) -> list[dict]:
    from backend.app.core.database import Database

    with Database(api.database).connection() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM journal_events WHERE change_id = ? AND event_type = ? "
            "ORDER BY seq", (change_id, event_type)).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def _check_runs(api: LiveApi, change_id: str) -> list[dict]:
    response = api.call("GET", f"/changes/{change_id}/checks")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _verified_check(api: LiveApi, repo: Path, executable: str = "pytest",
                    args: list[str] | None = None) -> tuple[str, dict]:
    change_id = _create_change(api, repo)
    actor = _actor(api, change_id, ["change.legacy_verify"])
    response = _verify(api, change_id, actor, executable, PYTEST_ARGS if args is None else args)
    assert response.status_code == 200, response.text
    return change_id, response.json()["verification"]


def _assert_linux_confined(api: LiveApi, change_id: str, verification: dict) -> None:
    assert verification["boundary"] == "LINUX_SANDBOX", verification
    run_id = verification["check_run_id"]
    runs = {item["id"]: item for item in _check_runs(api, change_id)}
    run = runs[run_id]
    assert run["boundary"] == "LINUX_SANDBOX" and run["state"] == "CLEANED"
    assert run["token"] is None and run["network"] is False
    facts = run["linux_sandbox"]
    assert facts["verified"] is True and facts["network_isolated"] is True
    assert facts["no_new_privs"] is True and facts["seccomp_mode"] == "2"
    assert facts["seccomp_filters_added"] >= 1
    assert {"user", "mnt", "pid", "net"} <= set(facts["separate_namespaces"])
    events = [event for event in _journal(api, change_id, "check.confined_run")
              if event["check_run_id"] == run_id]
    assert events and all(event["boundary"] == "LINUX_SANDBOX"
                          and event["package_sid"].startswith("linux-sandbox:")
                          and event["linux_sandbox"]["verified"] is True
                          and event["capabilities"] == [] for event in events)
    assert _journal(api, change_id, "check.unconfined_run") == []


def _no_boxes_left(api: LiveApi) -> None:
    packages = api.store / "linux-checks" / "Packages"
    assert not packages.exists() or list(packages.iterdir()) == []
    if PARENT:
        assert not [entry.name for entry in Path(PARENT).iterdir()
                    if entry.name.startswith("sentinel-run-")]


def _passport(api: LiveApi, change_id: str):
    from backend.app.core.database import Database
    from backend.app.passport.v2 import PassportV2Issuer

    return PassportV2Issuer(Database(api.database)).snapshot(UUID(change_id))


# ------------------------------------------------------------------ escapes


def test_python_conftest_escapes_are_denied_through_the_api(
        live_api: LiveApi, tmp_path: Path) -> None:
    api = live_api
    repo = make_repo(tmp_path / "repo", PY_FILES)
    write(repo, ".env", "SECRET=canary-in-the-user-repository\n")  # git-ignored
    home = tmp_path / "fake-home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("canary-private-key\n")
    canary = f"escaped-{uuid4().hex}.txt"
    canaries = [repo / canary, api.store / canary, home / canary]
    config = {
        "writes": {"repo": str(canaries[0]), "store": str(canaries[1]), "home": str(canaries[2])},
        "reads": {"repo_env": str(repo / ".env"), "api_token": str(api.store / "api_token"),
                  "database": str(api.database), "ssh_key": str(home / ".ssh" / "id_ed25519"),
                  "trust_registry": str(api.store / "trusted_keys.json")},
        "lists": {"repo": str(repo), "store": str(api.store), "home": str(home)},
        "ports": {"api": api.port, "standin": api.listener.port},
    }
    write(repo, "conftest.py", _conftest(config))  # untracked: part of the check tree

    # Positive control: at user authority, outside Sentinel, every attempt works.
    control = _host_pytest(repo, tmp_path / "control-copy")
    for key, value in control.items():
        if key.startswith(("write_", "read_", "connect_")):
            assert value == "allowed", control
        if key.startswith("list_"):
            assert value.startswith("listed:"), control
    assert "api_token" in control["list_store"] and "id_ed25519" not in control["list_home"]
    assert all(path.exists() for path in canaries)
    assert len(api.listener.accepted) >= 1
    for path in canaries:
        path.unlink()
    accepted_before = len(api.listener.accepted)

    change_id, verification = _verified_check(api, repo)
    assert verification["status"] == "PASSED", verification["stdout"] + verification["stderr"]
    probe = _probe_line(verification["stdout"])
    print("confined probe:", json.dumps(probe, sort_keys=True))

    assert Path(probe["cwd"]).name == "tree"
    assert probe["tree_has_ignored_env"] is False
    # A write either fails or lands on the sandbox's own tmpfs: never on the host.
    assert not any(path.exists() for path in canaries), probe
    for name in config["reads"]:
        assert probe["read_" + name].startswith("denied:"), probe
    for name in config["lists"]:
        listing = probe["list_" + name]
        assert listing.startswith("denied:") or not any(
            secret in listing for secret in ("api_token", "sentinel.sqlite3", ".env", ".ssh",
                                             "calc.py")), probe
    for name in config["ports"]:
        assert probe["connect_" + name].startswith("denied:"), probe
    assert len(api.listener.accepted) == accepted_before
    assert "canary" not in verification["stdout"]
    _assert_linux_confined(api, change_id, verification)

    claims = _passport(api, change_id)
    assert claims.confined_checks == "PASS", claims.limitations
    assert [(str(item.check_run_id), item.boundary) for item in claims.check_runs] == [
        (verification["check_run_id"], "LINUX_SANDBOX")]
    _no_boxes_left(api)


def test_an_agent_gitignore_edit_does_not_put_dotenv_in_the_box(
        live_api: LiveApi, tmp_path: Path) -> None:
    """CR-01: the agent's change un-ignores ``.env``; the box still never receives it."""

    repo = make_repo(tmp_path / "repo", PY_FILES)
    write(repo, ".env", "SECRET=canary-in-the-user-repository\n")
    write(repo, ".gitignore", "__pycache__/\n.pytest_cache/\n!.env\n")
    write(repo, "conftest.py", _conftest({}))
    assert ".env" in git(repo, "ls-files", "--others", "--exclude-standard").split()
    assert _host_pytest(repo, tmp_path / "control-copy")["tree_has_ignored_env"] is True

    change_id, verification = _verified_check(live_api, repo)
    assert verification["status"] == "PASSED", verification["stdout"] + verification["stderr"]
    assert _probe_line(verification["stdout"])["tree_has_ignored_env"] is False
    _assert_linux_confined(live_api, change_id, verification)


def test_internet_is_denied_without_a_declared_network(live_api: LiveApi, tmp_path: Path) -> None:
    try:
        with socket.create_connection(INTERNET, timeout=8):
            pass
    except OSError as exc:
        pytest.skip(f"host cannot reach {INTERNET[0]}:{INTERNET[1]} ({exc!r}); "
                    "the positive control cannot pass offline")
    repo = make_repo(tmp_path / "repo", PY_FILES)
    write(repo, "conftest.py", _conftest({"internet_host": INTERNET[0],
                                          "internet_port": INTERNET[1]}))
    assert _host_pytest(repo, tmp_path / "control-copy")["internet"] == "allowed"
    change_id, verification = _verified_check(live_api, repo)
    assert _probe_line(verification["stdout"])["internet"].startswith("denied:")
    _assert_linux_confined(live_api, change_id, verification)


# ------------------------------------------------------------------ lifetime and limits


def test_a_daemon_left_by_a_check_is_killed_with_its_box(live_api: LiveApi, tmp_path: Path) -> None:
    token = f"sentinel-daemon-{uuid4().hex}"
    repo = make_repo(tmp_path / "repo", {
        **PY_FILES,
        "conftest.py": (
            "import os, subprocess, sys\n"
            "if os.fork() == 0:\n"
            "    os.setsid()\n"
            "    null = os.open(os.devnull, os.O_RDWR)\n"
            "    for fd in (0, 1, 2):\n"
            "        os.dup2(null, fd)\n"
            f"    os.execv(sys.executable, [sys.executable, '-c', 'import time; time.sleep(600)', "
            f"'{token}'])\n"),
    })
    change_id, verification = _verified_check(live_api, repo)
    assert verification["status"] == "PASSED", verification["stdout"] + verification["stderr"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        alive = [entry.name for entry in Path("/proc").iterdir() if entry.name.isdigit()
                 and _cmdline_has(entry, token)]
        if not alive:
            break
        time.sleep(0.1)
    assert not alive, "a daemon outlived its check box"
    _assert_linux_confined(live_api, change_id, verification)
    _no_boxes_left(live_api)


def _cmdline_has(entry: Path, token: str) -> bool:
    try:
        return token.encode() in (entry / "cmdline").read_bytes()
    except OSError:
        return False


@requires_controllers
def test_a_fork_bomb_in_a_check_is_stopped_by_pids_max(live_api: LiveApi, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo", {
        **PY_FILES,
        "conftest.py": (
            "import os, time\n"
            "children = 0\n"
            "try:\n"
            "    while children < 100000:\n"
            "        if os.fork() == 0:\n"
            "            time.sleep(5)\n"
            "            os._exit(0)\n"
            "        children += 1\n"
            "except OSError as exc:\n"
            "    print('SENTINEL_PROBE {\"fork_stopped\": %d}' % children, flush=True)\n"),
    })
    started = time.monotonic()
    change_id, verification = _verified_check(live_api, repo)
    probe = _probe_line(verification["stdout"])
    assert 0 < probe["fork_stopped"] < 1000, probe
    assert time.monotonic() - started < 120
    _assert_linux_confined(live_api, change_id, verification)
    _no_boxes_left(live_api)


# ------------------------------------------------------------------ refusals and evidence


def test_an_unconfined_toolchain_is_refused_without_the_opt_in(
        live_api: LiveApi, tmp_path: Path, monkeypatch) -> None:
    toolchain = tmp_path / "stand-in-toolchain"
    toolchain.mkdir()
    uv = toolchain / "uv"
    uv.write_text("#!/bin/sh\necho ran > uv-ran.txt\n")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{toolchain}:{os.environ['PATH']}")
    repo = make_repo(tmp_path / "repo", PY_FILES)
    change_id = _create_change(live_api, repo)
    actor = _actor(live_api, change_id, ["change.legacy_verify"])
    response = _verify(live_api, change_id, actor, "uv", ["run"])
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CHECK_TOOLCHAIN_UNCONFINED"
    assert not (repo / "uv-ran.txt").exists()
    assert _check_runs(live_api, change_id) == []


def test_tampered_check_facts_drop_the_passport_claim(live_api: LiveApi, tmp_path: Path) -> None:
    from backend.app.core.database import Database

    repo = make_repo(tmp_path / "repo", PY_FILES)
    change_id, verification = _verified_check(live_api, repo)
    assert _passport(live_api, change_id).confined_checks == "PASS"
    with Database(live_api.database).connection() as connection:
        row = connection.execute("SELECT facts_json FROM check_runs WHERE id = ?",
                                 (verification["check_run_id"],)).fetchone()
        facts = json.loads(row["facts_json"])
        facts["seccomp_filters"] = facts["supervisor_seccomp_filters"]  # "no filter added"
        connection.execute("UPDATE check_runs SET facts_json = ? WHERE id = ?",
                           (json.dumps(facts), verification["check_run_id"]))
    claims = _passport(live_api, change_id)
    assert claims.confined_checks == "FAIL"
    assert [item.boundary for item in claims.check_runs] == [None]
    [run] = _check_runs(live_api, change_id)
    assert run["boundary"] is None


def test_npm_test_runs_confined_with_the_host_node(live_api: LiveApi, tmp_path: Path) -> None:
    if shutil.which("node", path="/usr/local/bin:/usr/bin:/bin") is None:
        pytest.skip("Node.js is not installed in a trusted directory")
    repo = make_repo(tmp_path / "repo", {
        "package.json": json.dumps({"name": "confined", "version": "1.0.0", "private": True,
                                    "scripts": {"test": "node check.js"}}),
        "check.js": ("console.log('SENTINEL_PROBE ' + JSON.stringify({cwd: process.cwd(),"
                     " node: process.version}));\n"),
    })
    change_id, verification = _verified_check(live_api, repo, "npm", ["test"])
    assert verification["status"] == "PASSED", verification["stdout"] + verification["stderr"]
    probe = _probe_line(verification["stdout"])
    assert Path(probe["cwd"]).name == "tree"
    _assert_linux_confined(live_api, change_id, verification)
