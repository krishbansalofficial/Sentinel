"""A diff touching a Change Contract forbidden path never lands.

Real Windows AppContainer and WorkspaceManager; the agent is node under the
``fake_node_launcher`` profile. The refusal holds at preview (no approval token)
and again at apply, for patterns the contract gained after an approved preview.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from backend.app.contracts.models import AgentRunStatus
from backend.app.execution.process_supervisor import IS_WINDOWS
from backend.app.workspace.manager import FORBIDDEN_PATH_LIMITATION, WorkspaceManager
from backend.app.workspace.models import FORBIDDEN_FLAG, ApplyRefusal
from backend.tests.support_kb import git
from backend.tests.workspace.conftest import FakeNodeLauncher, repo_fingerprint

pytestmark = pytest.mark.skipif(not IS_WINDOWS, reason="AppContainers are Windows-only")

EDIT_AND_RENAME = "\n".join([
    "const fs = require('fs');",
    "fs.appendFileSync('calc.py', " + json.dumps("\ndef sub(a, b):\n    return a - b\n") + ");",
    "fs.renameSync('rename_me.txt', 'renamed.txt');",
    "process.stdout.write('done');",
])


def _run(fake: FakeNodeLauncher, change_id, repo: Path) -> None:
    run = fake.launch(change_id, repo, EDIT_AND_RENAME)
    assert run.status is AgentRunStatus.PASSED, (run.stderr, run.limitations)


def _flags(preview, path: str) -> tuple[str, ...]:
    return next(flags for _status, name, _old, _new, flags in preview.changed_paths
                if name == path)


def test_preview_refuses_a_forbidden_path_with_no_token(
    workspace_manager: WorkspaceManager, user_repo: Path,
    fake_node_launcher: FakeNodeLauncher,
) -> None:
    change_id = uuid4()
    before = repo_fingerprint(user_repo)
    _run(fake_node_launcher, change_id, user_repo)

    preview = workspace_manager.preview(change_id, ["calc.py"])

    assert preview.refusal_reason == ApplyRefusal.FORBIDDEN_PATH_IN_DIFF.value
    assert preview.approval_token is None and preview.fast_forward_possible is False
    assert FORBIDDEN_FLAG in _flags(preview, "calc.py")
    assert FORBIDDEN_FLAG not in _flags(preview, "renamed.txt")
    assert FORBIDDEN_PATH_LIMITATION in preview.limitations
    assert repo_fingerprint(user_repo) == before


def test_renaming_away_from_a_forbidden_path_is_refused(
    workspace_manager: WorkspaceManager, user_repo: Path,
    fake_node_launcher: FakeNodeLauncher,
) -> None:
    change_id = uuid4()
    _run(fake_node_launcher, change_id, user_repo)
    preview = workspace_manager.preview(change_id, ["rename_me.txt"])
    assert preview.refusal_reason == ApplyRefusal.FORBIDDEN_PATH_IN_DIFF.value
    assert preview.approval_token is None


def test_apply_refuses_patterns_added_after_an_approved_preview(
    workspace_manager: WorkspaceManager, user_repo: Path,
    fake_node_launcher: FakeNodeLauncher,
) -> None:
    change_id = uuid4()
    before = repo_fingerprint(user_repo)
    head = git(user_repo, "rev-parse", "HEAD").strip()
    _run(fake_node_launcher, change_id, user_repo)
    preview = workspace_manager.preview(change_id, ["docs/**"])
    assert preview.refusal_reason is None and preview.approval_token

    record = workspace_manager.apply(change_id, preview.approval_token, ["calc.py"])

    assert record.refusal_reason == ApplyRefusal.FORBIDDEN_PATH_IN_DIFF.value
    assert record.applied_sha is None and record.approval_digest is None
    assert git(user_repo, "rev-parse", "HEAD").strip() == head
    assert repo_fingerprint(user_repo) == before
