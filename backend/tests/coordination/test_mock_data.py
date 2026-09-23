"""Mock-data scenarios for coordination, driven by fixed seeds.

- A generated, realistic repository (packages, tests, docs, binary assets,
  CRLF scripts, unicode names, ignore rules) edited by several mock agents
  that run as real subprocesses in parallel, each in its own registry
  workspace, with an independent model of what every agent changed.
- A few hundred seeded workspace rows across many Changes in every retention
  state, reconciled in one pass.
- Bulk task creation with execution fields and dependency chains over HTTP.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from backend.app.contracts.models import WorkspacePurpose, WorkspaceState
from backend.app.coordination.workspace_registry import WorkspaceRegistry
from backend.app.coordination.workspaces import WorkspaceManager
from backend.app.core.database import Database
from backend.app.core.journal import JournalWriter
from backend.app.core.replay_service import ReplayService
from backend.tests.acceptance.test_coordination_routes import _create_change, build_client


SEED = 20260922
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, encoding="utf-8", env=GIT_ENV).stdout.strip()


def git_bytes(cwd: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          env=GIT_ENV).stdout


def _seed_changes(database: Database, change_ids: list[UUID]) -> None:
    now = datetime.now(UTC).isoformat()
    with database.connection(immediate=True) as connection:
        for change_id in change_ids:
            connection.execute(
                "INSERT INTO changes (id, title, intent, repository_path, created_at, "
                "updated_at) VALUES (?, 'Mock', 'Mock data', 'C:\\mock', ?, ?)",
                (str(change_id), now, now),
            )


# -- mock repository -----------------------------------------------------------------


def build_mock_repository(root: Path, rng: random.Random) -> list[str]:
    """Create and commit a small but realistic project; return tracked paths."""

    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "core.autocrlf", "false")
    files: dict[str, bytes] = {
        ".gitignore": b"__pycache__/\n*.log\nbuild/\n",
        "README.md": b"# Mock project\n\nGenerated for coordination tests.\n",
        "pyproject.toml": b"[project]\nname = \"mock\"\nversion = \"0.1.0\"\n",
        "scripts/run.bat": b"@echo off\r\npython -m mock\r\n",
        "docs/gu\u00eda de uso.md": "Gu\u00eda\n".encode(),
    }
    for index in range(40):
        files[f"src/mock/module_{index:02d}.py"] = (
            f"\"\"\"Module {index}.\"\"\"\n\n\ndef compute_{index}(x):\n"
            f"    return x * {rng.randint(2, 99)}\n").encode()
        files[f"tests/test_module_{index:02d}.py"] = (
            f"from mock.module_{index:02d} import compute_{index}\n\n\n"
            f"def test_{index}():\n    assert compute_{index}(0) == 0\n").encode()
    for index in range(10):
        files[f"docs/page_{index}.md"] = f"# Page {index}\n\n{rng.random()}\n".encode()
        files[f"assets/blob_{index}.bin"] = rng.randbytes(2048)
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "mock project")
    return sorted(item for item in git(root, "ls-files", "-z").split("\0") if item)


MOCK_AGENT = r'''
"""Mock agent: applies a JSON edit plan inside its working directory."""
import json, os, subprocess, sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
env = {**os.environ, "GIT_AUTHOR_NAME": "agent", "GIT_AUTHOR_EMAIL": "a@a",
       "GIT_COMMITTER_NAME": "agent", "GIT_COMMITTER_EMAIL": "a@a"}
for op in plan:
    kind, path = op["op"], Path(op.get("path", "."))
    if kind in ("write", "junk", "secret"):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = bytes.fromhex(op["hex"]) if "hex" in op else op["text"].encode("utf-8")
        path.write_bytes(data)
    elif kind == "delete":
        path.unlink()
    elif kind == "rename":
        target = Path(op["to"])
        target.parent.mkdir(parents=True, exist_ok=True)
        path.rename(target)
    elif kind == "commit":
        subprocess.run(["git", "add", "-A"], check=True, env=env)
        subprocess.run(["git", "commit", "-q", "--no-verify", "-m", "agent checkpoint"],
                       check=True, env=env)
'''


def make_plan(rng: random.Random, agent: int, tracked: list[str]) -> tuple[list[dict], dict]:
    """Return (plan, model). The model maps each path the result must touch to
    its expected final bytes (None for deleted), plus the ignored/secret
    files the result must not contain."""

    live = {path for path in tracked if path != ".gitignore"}
    expected: dict[str, bytes | None] = {}
    plan: list[dict] = []
    # Every agent edits the same shared file, differently.
    shared = "src/mock/module_00.py"
    text = f"def compute_0(x):\n    return x + {agent}  # agent {agent}\n"
    plan.append({"op": "write", "path": shared, "text": text})
    expected[shared] = text.encode()
    committed_once = False
    for _ in range(rng.randint(4, 10)):
        kind = rng.choice(["modify", "add", "delete", "rename", "binary", "commit"])
        candidates = sorted(live - set(expected) - {shared})
        if kind == "modify" and candidates:
            path = rng.choice(candidates)
            text = f"# rewritten by agent {agent}\nVALUE = {rng.random()}\n"
            plan.append({"op": "write", "path": path, "text": text})
            expected[path] = text.encode()
        elif kind == "add":
            path = f"src/mock/agent_{agent}/feature_{rng.randint(0, 999):03d}.py"
            if path in expected:
                continue
            text = f"FEATURE = {agent}\n"
            plan.append({"op": "write", "path": path, "text": text})
            expected[path] = text.encode()
            live.add(path)
        elif kind == "delete" and candidates:
            path = rng.choice(candidates)
            plan.append({"op": "delete", "path": path})
            expected[path] = None
            live.discard(path)
        elif kind == "rename" and candidates:
            path = rng.choice([c for c in candidates if c.endswith(".md")] or candidates)
            target = f"archive/agent_{agent}/{Path(path).name}"
            if target in expected or target in live:
                continue
            plan.append({"op": "rename", "path": path, "to": target})
            original = (TRACKED_CONTENT[path])
            expected[path] = None
            expected[target] = original
            live.discard(path)
            live.add(target)
        elif kind == "binary":
            path = f"assets/agent_{agent}_{rng.randint(0, 99)}.bin"
            if path in expected:
                continue
            data = rng.randbytes(rng.randint(1, 4096))
            plan.append({"op": "write", "path": path, "hex": data.hex()})
            expected[path] = data
            live.add(path)
        elif kind == "commit" and not committed_once:
            plan.append({"op": "commit"})
            committed_once = True
    # Noise every agent leaves behind that must never reach a result.
    plan.append({"op": "junk", "path": f"build/out_{agent}.log", "text": "log\n"})
    plan.append({"op": "junk", "path": "src/mock/__pycache__/m.cpython.pyc", "text": "pyc"})
    plan.append({"op": "secret", "path": "config/.env", "text": f"TOKEN={agent}\n"})
    return plan, expected


TRACKED_CONTENT: dict[str, bytes] = {}


def test_parallel_mock_agents_on_a_generated_repository(tmp_path: Path) -> None:
    rng = random.Random(SEED)
    repo = tmp_path / "mock project"
    tracked = build_mock_repository(repo, rng)
    TRACKED_CONTENT.clear()
    TRACKED_CONTENT.update({path: (repo / path).read_bytes() for path in tracked})
    base = git(repo, "rev-parse", "HEAD")
    # The user keeps working in their own checkout meanwhile.
    (repo / "README.md").write_bytes(b"user is editing this\n")
    (repo / "notes-user.txt").write_text("private\n", encoding="utf-8")

    database = Database(tmp_path / "state" / "coord.sqlite3")
    database.initialize()
    change_id = uuid4()
    _seed_changes(database, [change_id])
    registry = WorkspaceRegistry(database, WorkspaceManager(tmp_path / "state" / "ws"),
                                 journal=JournalWriter(database))
    agent_script = tmp_path / "mock_agent.py"
    agent_script.write_text(MOCK_AGENT, encoding="utf-8")

    agents = 5
    workspaces, models = [], []
    for agent in range(agents):
        plan, model = make_plan(rng, agent, tracked)
        plan_file = tmp_path / f"plan_{agent}.json"
        plan_file.write_text(json.dumps(plan), encoding="utf-8")
        record = registry.provision(change_id, str(repo), workspace_id=uuid4(),
                                    purpose=WorkspacePurpose.ATTEMPT, base_revision="main")
        workspaces.append((record, plan_file))
        models.append(model)

    # Run every mock agent at once as a real process in its own worktree.
    processes = [
        subprocess.Popen([sys.executable, str(agent_script), str(plan_file)],
                         cwd=record.path, env=GIT_ENV,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for record, plan_file in workspaces
    ]
    for process in processes:
        _out, err = process.communicate(timeout=120)
        assert process.returncode == 0, err.decode(errors="replace")

    # Finalize all of them concurrently, as a dispatcher would.
    captured: list[object] = [None] * agents
    errors: list[BaseException] = []
    barrier = threading.Barrier(agents)

    def finalize(index: int) -> None:
        try:
            barrier.wait(timeout=30)
            captured[index] = registry.capture(workspaces[index][0].id, write_paths=[],
                                               message=f"agent {index}")
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=finalize, args=(i,)) for i in range(agents)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)
    assert errors == []

    for index, record in enumerate(captured):
        model = models[index]
        manifest = record.capture
        touched = {item["path"] for item in manifest["files"]} | {
            item["old_path"] for item in manifest["files"] if item["old_path"]}
        assert touched == set(model), f"agent {index}"
        for path, content in model.items():
            if content is not None:
                assert git_bytes(repo, "cat-file", "blob", f"{record.result_sha}:{path}") \
                    == content, (index, path)
        tree = set(git(repo, "ls-tree", "-r", "-z", "--name-only", record.result_sha).split("\0"))
        assert not any(p.startswith("build/") or "__pycache__" in p for p in tree)
        assert "config/.env" not in tree
        assert manifest["excluded_sensitive"] == ["config/.env"]
        # Each result carries only its own agent's version of the shared file.
        shared = git(repo, "show", f"{record.result_sha}:src/mock/module_00.py")
        assert f"# agent {index}" in shared

    for record in captured:
        registry.remove(record.id, attempt_live=False)
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "-q", "--prune=now")
    for record in captured:
        assert git(repo, "rev-parse", record.result_ref) == record.result_sha
    assert git(repo, "rev-parse", "HEAD") == base
    assert git(repo, "branch", "--show-current") == "main"
    assert (repo / "README.md").read_bytes() == b"user is editing this\n"
    assert git_bytes(repo, "status", "--porcelain").decode().splitlines() == [
        " M README.md", "?? notes-user.txt"]
    assert registry.reconcile().orphan_paths == []
    assert ReplayService(database).verify_chain(change_id).verified is True


# -- mock database rows -------------------------------------------------------------


def test_reconcile_over_hundreds_of_seeded_rows(tmp_path: Path) -> None:
    rng = random.Random(SEED + 1)
    database = Database(tmp_path / "coord.sqlite3")
    database.initialize()
    changes = [uuid4() for _ in range(10)]
    _seed_changes(database, changes)
    manager = WorkspaceManager(tmp_path / "managed")
    registry = WorkspaceRegistry(database, manager, journal=JournalWriter(database))
    expected: dict[UUID, WorkspaceState] = {}
    transitions = {
        WorkspaceState.CREATING: WorkspaceState.FAILED,
        WorkspaceState.READY: WorkspaceState.FAILED,
        WorkspaceState.CAPTURED: WorkspaceState.REMOVED,
        WorkspaceState.REMOVING: WorkspaceState.REMOVED,
        WorkspaceState.REMOVED: WorkspaceState.REMOVED,
        WorkspaceState.FAILED: WorkspaceState.FAILED,
    }
    per_change_transitions = {change: 0 for change in changes}
    now = datetime.now(UTC).isoformat()
    with database.connection(immediate=True) as connection:
        for change in changes:
            for _ in range(30):
                workspace_id = uuid4()
                state = rng.choice(list(WorkspaceState))
                expected[workspace_id] = transitions[state]
                if transitions[state] is not state:
                    per_change_transitions[change] += 1
                connection.execute(
                    "INSERT INTO coord_workspaces (id, change_id, purpose, repository_identity, "
                    "repository_root, path, base_sha, state, created_at, updated_at) "
                    "VALUES (?, ?, 'ATTEMPT', 'mock', ?, ?, ?, ?, ?, ?)",
                    (str(workspace_id), str(change), str(tmp_path / "no such repo"),
                     str(manager.managed_root / str(workspace_id)), "0" * 40, state.value,
                     now, now),
                )
    orphans = []
    for index in range(3):
        orphan = manager.managed_root / f"stray-{index}"
        orphan.mkdir()
        orphans.append(str(orphan))

    started = time.monotonic()
    report = registry.reconcile()
    elapsed = time.monotonic() - started

    assert len(expected) == 300
    assert elapsed < 60, elapsed
    for workspace_id, state in expected.items():
        assert registry.get(workspace_id).state is state
    assert len(report.failed) + len(report.removed) == sum(per_change_transitions.values())
    assert report.orphan_paths == sorted(orphans)
    assert all(Path(orphan).is_dir() for orphan in orphans)
    for change in changes:
        with database.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM journal_events WHERE change_id = ?",
                (str(change),)).fetchone()["n"]
        assert count == per_change_transitions[change]
        assert ReplayService(database).verify_chain(change).verified is True
    second = registry.reconcile()
    assert (second.failed, second.removed, second.readied) == ([], [], [])


# -- mock tasks over HTTP -------------------------------------------------------------


def test_bulk_mock_tasks_with_execution_fields_and_dependencies(tmp_path: Path) -> None:
    rng = random.Random(SEED + 2)
    with build_client(tmp_path) as client:
        change_id = _create_change(client)
        base_url = f"/api/v1/changes/{change_id}/tasks"
        sent: dict[str, dict] = {}
        ids: list[str] = []
        for index in range(40):
            body = {
                "title": f"Mock task {index}",
                "instructions": f"Do step {index} carefully",
                "adapter": rng.choice(["claude", "codex", "local"]),
                "priority": rng.randint(0, 1000),
                "max_attempts": rng.randint(1, 10),
                "executable": rng.choice(["python", "node", "agent.exe"]),
                "args": [f"--step={index}", "{instructions}"][: rng.randint(0, 2)],
                "write_paths": rng.sample(["src/**", "tests/**", "docs/*.md", "*.toml"],
                                          rng.randint(0, 3)),
                "verification": [{"executable": "pytest", "args": ["-q", f"-k={index}"],
                                  "timeout_seconds": rng.randint(1, 300)}]
                                if rng.random() < 0.5 else [],
                "resources": [{"key": f"port:{8000 + index}", "units": rng.randint(1, 4)}]
                             if rng.random() < 0.3 else [],
            }
            response = client.post(base_url, json=body,
                                   headers={"Idempotency-Key": f"mock-task-{index:04d}"})
            assert response.status_code == 201, response.text
            task_id = response.json()["id"]
            ids.append(task_id)
            sent[task_id] = body
            # A replayed create returns the same task, not a second one.
            replay = client.post(base_url, json=body,
                                 headers={"Idempotency-Key": f"mock-task-{index:04d}"})
            assert replay.json()["id"] == task_id

        # Dependency chains: each task may depend on up to two earlier ones.
        for index in range(1, 40):
            predecessors = rng.sample(ids[:index], min(index, rng.randint(0, 2)))
            if not predecessors:
                continue
            response = client.put(f"{base_url}/{ids[index]}/dependencies", json={
                "expected_revision": 1, "depends_on_task_ids": predecessors})
            assert response.status_code == 200, response.text
        # Closing any chain into a cycle is refused.
        last = client.get(f"{base_url}/{ids[-1]}").json()
        if last["depends_on_task_ids"]:
            first_dep = last["depends_on_task_ids"][0]
            dep_view = client.get(f"{base_url}/{first_dep}").json()
            cycle = client.put(f"{base_url}/{first_dep}/dependencies", json={
                "expected_revision": dep_view["revision"],
                "depends_on_task_ids": [*dep_view["depends_on_task_ids"], ids[-1]]})
            assert cycle.status_code == 409
            assert cycle.json()["error"]["code"] == "TASK_DEPENDENCY_CYCLE"

        listed: dict[str, dict] = {}
        offset = 0
        while True:
            page = client.get(base_url, params={"limit": 15, "offset": offset}).json()
            for item in page["items"]:
                listed[item["id"]] = item
            offset += page["count"]
            if page["count"] == 0 or offset >= page["total"]:
                break
        assert page["total"] == 40
        assert set(listed) == set(ids)
        for task_id, body in sent.items():
            item = listed[task_id]
            for key in ("executable", "args", "write_paths", "verification", "resources",
                        "priority", "max_attempts", "adapter"):
                assert item[key] == body[key], (task_id, key)

        # Submitting every task: roots become READY, dependents WAITING.
        for task_id in ids:
            view = client.get(f"{base_url}/{task_id}").json()
            submitted = client.post(f"{base_url}/{task_id}/submit",
                                    json={"expected_revision": view["revision"]})
            assert submitted.status_code == 200, submitted.text
            state = submitted.json()["state"]
            assert state == ("WAITING" if view["depends_on_task_ids"] else "READY")
