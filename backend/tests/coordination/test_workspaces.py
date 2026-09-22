"""Phase 2: managed worktrees against real temporary Git repositories."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.coordination.workspaces import WorkspaceManager
from backend.app.core.errors import AppError


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "user repo with spaces"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "core.autocrlf", "false")
    (root / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "old_name.txt").write_text("rename me\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture
def manager(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(tmp_path / "managed root" / "coord-workspaces")


def test_two_attempts_edit_same_file_without_touching_each_other_or_user(
    repo: Path, manager: WorkspaceManager
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    # The user has uncommitted work on their own branch; it must survive.
    (repo / "app.py").write_text("USER DIRTY EDIT\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("user untracked\n", encoding="utf-8")

    one = manager.create(str(repo), "attempt-one", base, "sentinel/attempt/one")
    two = manager.create(str(repo), "attempt-two", base, "sentinel/attempt/two")
    assert (one / "app.py").read_text(encoding="utf-8") == "def value():\n    return 1\n"

    (one / "app.py").write_text("def value():\n    return 'one'\n", encoding="utf-8")
    (two / "app.py").write_text("def value():\n    return 'two'\n", encoding="utf-8")
    assert "'one'" in (one / "app.py").read_text(encoding="utf-8")
    assert "'two'" in (two / "app.py").read_text(encoding="utf-8")

    first = manager.capture(one, base_sha=base, write_paths=[], message="attempt one")
    second = manager.capture(two, base_sha=base, write_paths=[], message="attempt two")
    assert first.result_sha != second.result_sha
    assert [f.path for f in first.files] == ["app.py"]

    # User's branch, HEAD, dirty file and untracked file are all unchanged.
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "rev-parse", "HEAD") == base
    assert (repo / "app.py").read_text(encoding="utf-8") == "USER DIRTY EDIT\n"
    assert (repo / "scratch.txt").exists()
    # Each result is an immutable commit reachable from its own branch.
    assert git(repo, "rev-parse", "sentinel/attempt/one") == first.result_sha
    assert git(repo, "rev-parse", "sentinel/attempt/two") == second.result_sha


def test_capture_reports_rename_binary_untracked_scope_and_sensitive(
    repo: Path, manager: WorkspaceManager
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "attempt-capture", base, "sentinel/attempt/capture")
    (path / "old_name.txt").rename(path / "new_name.txt")
    (path / "image.bin").write_bytes(bytes(range(256)) * 4)
    (path / "docs").mkdir()
    (path / "docs" / "notes.md").write_text("new untracked doc\n", encoding="utf-8")
    (path / ".env").write_text("API_KEY=secret\n", encoding="utf-8")

    result = manager.capture(path, base_sha=base, write_paths=["src/**", "*.txt", "*.bin"],
                             message="capture")

    by_path = {f.path: f for f in result.files}
    assert by_path["new_name.txt"].status == "renamed"
    assert by_path["new_name.txt"].old_path == "old_name.txt"
    assert by_path["image.bin"].binary is True
    assert by_path["docs/notes.md"].status == "added"
    assert result.scope_violations == ["docs/notes.md"]
    assert result.excluded_sensitive == [".env"]
    assert ".env" not in by_path
    committed = git(repo, "show", "--name-only", "--format=", result.result_sha).splitlines()
    assert ".env" not in committed


def test_sensitive_tracked_file_is_restored_not_deleted(
    repo: Path, manager: WorkspaceManager
) -> None:
    (repo / "service.key").write_text("original\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tracked key")
    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "attempt-key", base, "sentinel/attempt/key")
    (path / "service.key").write_text("agent overwrote\n", encoding="utf-8")
    (path / "app.py").write_text("changed\n", encoding="utf-8")

    result = manager.capture(path, base_sha=base, write_paths=[], message="c")

    assert result.excluded_sensitive == ["service.key"]
    assert git(repo, "show", f"{result.result_sha}:service.key") == "original"


def test_no_change_result_is_explicit(repo: Path, manager: WorkspaceManager) -> None:
    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "attempt-empty", base, "sentinel/attempt/empty")
    result = manager.capture(path, base_sha=base, write_paths=[], message="nothing")
    assert result.no_change is True
    assert result.result_sha == base


def test_conflict_markers_are_reported(repo: Path, manager: WorkspaceManager) -> None:
    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "attempt-markers", base, "sentinel/attempt/markers")
    (path / "app.py").write_text("<<<<<<< ours\na\n=======\nb\n>>>>>>> theirs\n",
                                 encoding="utf-8")
    result = manager.capture(path, base_sha=base, write_paths=[], message="markers")
    assert result.conflict_markers == ["app.py"]


def test_merge_conflict_detected_and_both_sides_preserved(
    repo: Path, manager: WorkspaceManager
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    one = manager.create(str(repo), "a1", base, "sentinel/attempt/a1")
    two = manager.create(str(repo), "a2", base, "sentinel/attempt/a2")
    (one / "app.py").write_text("def value():\n    return 'one'\n", encoding="utf-8")
    (two / "app.py").write_text("def value():\n    return 'two'\n", encoding="utf-8")
    r1 = manager.capture(one, base_sha=base, write_paths=[], message="one")
    r2 = manager.capture(two, base_sha=base, write_paths=[], message="two")

    candidate = manager.create(str(repo), "integration-1", r1.result_sha, None)
    conflicts = manager.merge_into(candidate, r2.result_sha, "merge two")
    assert conflicts == ["app.py"]
    manager.abort_merge(candidate)
    assert manager.head(candidate) == r1.result_sha
    # Both source results remain intact commits.
    assert "'one'" in git(repo, "show", f"{r1.result_sha}:app.py")
    assert "'two'" in git(repo, "show", f"{r2.result_sha}:app.py")


def test_clean_merge_of_different_files(repo: Path, manager: WorkspaceManager) -> None:
    base = git(repo, "rev-parse", "HEAD")
    one = manager.create(str(repo), "c1", base, "sentinel/attempt/c1")
    two = manager.create(str(repo), "c2", base, "sentinel/attempt/c2")
    (one / "a.txt").write_text("a\n", encoding="utf-8")
    (two / "b.txt").write_text("b\n", encoding="utf-8")
    r1 = manager.capture(one, base_sha=base, write_paths=[], message="a")
    r2 = manager.capture(two, base_sha=base, write_paths=[], message="b")
    candidate = manager.create(str(repo), "integration-2", r1.result_sha, None)
    assert manager.merge_into(candidate, r2.result_sha, "merge") == []
    assert (candidate / "a.txt").exists() and (candidate / "b.txt").exists()
    assert manager.tracked_modifications(candidate) == []


def test_managed_ref_compare_and_swap(repo: Path, manager: WorkspaceManager) -> None:
    base = git(repo, "rev-parse", "HEAD")
    ref = "refs/heads/sentinel/integration/change-1"
    assert manager.ensure_ref(str(repo), ref, base) == base
    ws = manager.create(str(repo), "cas", base, "sentinel/attempt/cas")
    (ws / "x.txt").write_text("x\n", encoding="utf-8")
    new = manager.capture(ws, base_sha=base, write_paths=[], message="x").result_sha

    assert manager.compare_and_swap_ref(str(repo), ref, new, expected_old=new) is False
    assert manager.read_ref(str(repo), ref) == base
    assert manager.compare_and_swap_ref(str(repo), ref, new, expected_old=base) is True
    assert manager.read_ref(str(repo), ref) == new
    with pytest.raises(AppError) as error:
        manager.compare_and_swap_ref(str(repo), "refs/heads/main", new, base)
    assert error.value.code == "WORKSPACE_REF_NOT_MANAGED"


def test_repository_identity_is_shared_across_worktrees(
    repo: Path, manager: WorkspaceManager
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    ws = manager.create(str(repo), "ident", base, "sentinel/attempt/ident")
    assert manager.repository_identity(str(repo)).identity == \
        manager.repository_identity(str(ws)).identity


def test_cleanup_refuses_live_foreign_and_escaping_paths(
    repo: Path, manager: WorkspaceManager, tmp_path: Path
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    ws = manager.create(str(repo), "cleanup", base, "sentinel/attempt/cleanup")

    with pytest.raises(AppError) as live:
        manager.remove(str(repo), ws, attempt_live=True)
    assert live.value.code == "WORKSPACE_ACTIVE"

    with pytest.raises(AppError) as foreign:
        manager.remove(str(repo), repo, attempt_live=False)
    assert foreign.value.code == "WORKSPACE_NOT_MANAGED"
    assert (repo / "app.py").exists()

    with pytest.raises(AppError) as dotdot:
        manager.remove(str(repo), manager.managed_root / ".." / "..", attempt_live=False)
    assert dotdot.value.code == "WORKSPACE_NOT_MANAGED"

    manager.remove(str(repo), ws, attempt_live=False)
    assert not ws.exists()
    assert (repo / "app.py").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions are Windows-only")
def test_cleanup_refuses_junction_escaping_managed_root(
    repo: Path, manager: WorkspaceManager, tmp_path: Path
) -> None:
    import _winapi

    outside = tmp_path / "outside target"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep\n", encoding="utf-8")
    junction = manager.managed_root / "looks-managed"
    _winapi.CreateJunction(str(outside), str(junction))
    try:
        with pytest.raises(AppError) as error:
            manager.remove(str(repo), junction, attempt_live=False)
        assert error.value.code == "WORKSPACE_NOT_MANAGED"
        assert (outside / "precious.txt").exists()
        # A case-aliased spelling of a real managed workspace is still recognized.
        base = git(repo, "rev-parse", "HEAD")
        ws = manager.create(str(repo), "case-alias", base, "sentinel/attempt/case-alias")
        assert manager.contains(Path(str(ws).upper())) is True
    finally:
        os.rmdir(junction)
