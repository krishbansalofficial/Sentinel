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


# -- capture correctness ------------------------------------------------------


def _commit_in(path: Path, message: str) -> str:
    git(path, "add", "-A")
    git(path, "commit", "-q", "--no-verify", "-m", message)
    return git(path, "rev-parse", "HEAD")


def test_agent_committed_work_is_a_result_not_no_change(
    repo: Path, manager: WorkspaceManager
) -> None:
    """Regression: an agent that commits its own edits leaves nothing staged;
    that must still produce a result, not an empty `no_change` capture."""

    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "self-commit", base, "sentinel/attempt/self-commit")
    (path / "app.py").write_text("def value():\n    return 42\n", encoding="utf-8")
    agent_head = _commit_in(path, "agent commit")

    result = manager.capture(path, base_sha=base, write_paths=[], message="capture")

    assert result.no_change is False
    assert result.result_sha == agent_head  # nothing new to commit on top
    assert [(f.path, f.status) for f in result.files] == [("app.py", "modified")]


def test_agent_commits_plus_uncommitted_edits_form_one_result(
    repo: Path, manager: WorkspaceManager
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "mixed", base, "sentinel/attempt/mixed")
    (path / "committed.txt").write_text("c\n", encoding="utf-8")
    agent_head = _commit_in(path, "agent part")
    (path / "uncommitted.txt").write_text("u\n", encoding="utf-8")

    result = manager.capture(path, base_sha=base, write_paths=[], message="capture")

    assert git(repo, "rev-parse", f"{result.result_sha}~1") == agent_head
    assert sorted(f.path for f in result.files) == ["committed.txt", "uncommitted.txt"]


def test_recapture_after_commit_returns_the_same_result(
    repo: Path, manager: WorkspaceManager
) -> None:
    """A capture interrupted after its commit (or simply replayed) must not
    mint a second commit or report the committed work as no change."""

    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "replay", base, "sentinel/attempt/replay")
    (path / "app.py").write_text("changed\n", encoding="utf-8")
    first = manager.capture(path, base_sha=base, write_paths=[], message="one")
    second = manager.capture(path, base_sha=base, write_paths=[], message="two")

    assert second.result_sha == first.result_sha
    assert second.no_change is False
    assert [f.path for f in second.files] == ["app.py"]
    assert git(repo, "rev-list", "--count", f"{base}..{first.result_sha}") == "1"


def test_deleted_and_unicode_paths_are_captured(repo: Path, manager: WorkspaceManager) -> None:
    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "unicode", base, "sentinel/attempt/unicode")
    (path / "old_name.txt").unlink()
    nested = path / "dïr with space"
    nested.mkdir()
    (nested / "fïle ñ.txt").write_text("hola\n", encoding="utf-8")

    result = manager.capture(path, base_sha=base, write_paths=[], message="unicode")

    by_path = {f.path: f.status for f in result.files}
    assert by_path == {"old_name.txt": "deleted", "dïr with space/fïle ñ.txt": "added"}


def test_sensitive_file_committed_by_agent_is_reported(
    repo: Path, manager: WorkspaceManager
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "leak", base, "sentinel/attempt/leak")
    (path / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
    _commit_in(path, "agent committed a secret")

    result = manager.capture(path, base_sha=base, write_paths=[], message="capture")

    assert result.sensitive_committed == [".env"]
    assert result.excluded_sensitive == []  # it never reached the working-tree filter


def test_base_divergence_is_refused_without_committing(
    repo: Path, manager: WorkspaceManager
) -> None:
    (repo / "second.txt").write_text("2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "second")
    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "diverge", base, "sentinel/attempt/diverge")
    git(path, "reset", "-q", "--hard", "HEAD~1")  # agent rewound past its base
    (path / "app.py").write_text("edit\n", encoding="utf-8")
    head_before = git(path, "rev-parse", "HEAD")

    with pytest.raises(AppError) as error:
        manager.capture(path, base_sha=base, write_paths=[], message="capture")

    assert error.value.code == "WORKSPACE_BASE_DIVERGED"
    assert error.value.details["base_sha"] == base
    assert git(path, "rev-parse", "HEAD") == head_before
    assert (path / "app.py").read_text(encoding="utf-8") == "edit\n"


def test_oversized_result_is_refused_and_index_restored(
    repo: Path, manager: WorkspaceManager
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "big", base, "sentinel/attempt/big")
    for index in range(5):
        (path / f"gen_{index}.txt").write_text(f"{index}\n", encoding="utf-8")

    with pytest.raises(AppError) as error:
        manager.capture(path, base_sha=base, write_paths=[], message="big", max_files=3)

    assert error.value.code == "WORKSPACE_RESULT_TOO_LARGE"
    assert error.value.details == {"file_count": 5, "limit": 3}
    assert git(path, "rev-parse", "HEAD") == base
    assert git(path, "diff", "--cached", "--name-only") == ""
    assert all((path / f"gen_{i}.txt").exists() for i in range(5))
    # The same work is accepted once the bound allows it.
    result = manager.capture(path, base_sha=base, write_paths=[], message="big", max_files=5)
    assert len(result.files) == 5


def test_oversized_bound_counts_agent_commits(repo: Path, manager: WorkspaceManager) -> None:
    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "big-commit", base, "sentinel/attempt/big-commit")
    for index in range(4):
        (path / f"c_{index}.txt").write_text("x\n", encoding="utf-8")
    _commit_in(path, "agent bulk commit")

    with pytest.raises(AppError) as error:
        manager.capture(path, base_sha=base, write_paths=[], message="c", max_files=3)
    assert error.value.details["file_count"] == 4


def test_broken_worktree_link_never_commits_into_an_enclosing_repo(
    repo: Path, tmp_path: Path
) -> None:
    """If an agent deletes its worktree's `.git` file, Git would discover the
    nearest enclosing repository. Capture must refuse instead of committing
    the agent's files there."""

    outer = tmp_path / "outer repo"
    outer.mkdir()
    git(outer, "init", "-q", "-b", "main")
    (outer / "readme.txt").write_text("outer\n", encoding="utf-8")
    git(outer, "add", "-A")
    git(outer, "commit", "-q", "-m", "outer base")
    outer_head = git(outer, "rev-parse", "HEAD")
    manager = WorkspaceManager(outer / "managed")
    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "broken", base, "sentinel/attempt/broken")
    (path / ".git").unlink()
    (path / "payload.txt").write_text("agent output\n", encoding="utf-8")

    with pytest.raises(AppError) as error:
        manager.capture(path, base_sha=base, write_paths=[], message="capture")
    assert error.value.code == "WORKSPACE_CORRUPT"
    with pytest.raises(AppError) as merge_error:
        manager.merge_into(path, base, "merge")
    assert merge_error.value.code == "WORKSPACE_CORRUPT"
    assert git(outer, "rev-parse", "HEAD") == outer_head
    assert git(outer, "diff", "--cached", "--name-only") == ""


# -- Git configuration an attacker or user repo could carry ----------------------


def test_repository_hooks_never_run(repo: Path, manager: WorkspaceManager, tmp_path: Path) -> None:
    marker = tmp_path / "hook ran"
    script = f'#!/bin/sh\necho ran > "{marker.as_posix()}"\nexit 1\n'
    hooks = repo / ".git" / "hooks"
    for name in ("pre-commit", "commit-msg", "post-commit", "post-checkout"):
        (hooks / name).write_text(script, encoding="utf-8", newline="\n")
        os.chmod(hooks / name, 0o755)
    # A repository-configured hooks directory must be overridden as well.
    custom = repo / "custom-hooks"
    custom.mkdir()
    for name in ("pre-commit", "post-checkout"):
        (custom / name).write_text(script, encoding="utf-8", newline="\n")
        os.chmod(custom / name, 0o755)
    git(repo, "config", "core.hooksPath", str(custom))
    base = git(repo, "rev-parse", "HEAD")

    path = manager.create(str(repo), "hooks", base, "sentinel/attempt/hooks")
    (path / "app.py").write_text("hooked\n", encoding="utf-8")
    result = manager.capture(path, base_sha=base, write_paths=[], message="capture")

    assert result.no_change is False
    assert not marker.exists()


def test_configured_clean_filter_command_is_not_executed(
    repo: Path, manager: WorkspaceManager, tmp_path: Path
) -> None:
    """The hardened Git runner refuses to inspect filtered files at all, so a
    repository-configured clean filter can never run during capture. The
    cost is that such repositories are unsupported (recorded limitation)."""

    marker = tmp_path / "filter ran"
    (repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "attributes")
    command = "sh -c \"echo x > '" + marker.as_posix() + "'; cat\""
    git(repo, "config", "filter.evil.clean", command)
    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "filter", base, "sentinel/attempt/filter")
    (path / "notes.txt").write_text("content\n", encoding="utf-8")

    with pytest.raises(AppError) as error:
        manager.capture(path, base_sha=base, write_paths=[], message="capture")

    assert error.value.code == "GIT_COMMAND_FAILED"
    assert not marker.exists()
    assert manager.head(path) == base  # nothing was committed


# -- creation guards ----------------------------------------------------------------


def test_create_refuses_bad_ids_bases_branches_and_existing_paths(
    repo: Path, manager: WorkspaceManager
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    cases = [
        ("../escape", base, None, "WORKSPACE_ID_INVALID"),
        ("ok-1", "HEAD", None, "WORKSPACE_BASE_UNRESOLVED"),
        ("ok-2", base, "main", "WORKSPACE_REF_NOT_MANAGED"),
        ("ok-3", base, "sentinel/../x", "WORKSPACE_REF_NOT_MANAGED"),
    ]
    for workspace_id, base_sha, branch, code in cases:
        with pytest.raises(AppError) as error:
            manager.create(str(repo), workspace_id, base_sha, branch)
        assert error.value.code == code, workspace_id
    (manager.managed_root / "taken").mkdir()
    with pytest.raises(AppError) as exists:
        manager.create(str(repo), "taken", base, None)
    assert exists.value.code == "WORKSPACE_EXISTS"
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "branch", "--list", "sentinel/*") == ""


def test_resolve_commit_rejects_unknown_revisions_and_non_commits(
    repo: Path, manager: WorkspaceManager
) -> None:
    root = str(repo)
    assert manager.resolve_commit(root, "main") == git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    for revision in ("does-not-exist", tree, "--output=x"):
        with pytest.raises(AppError) as error:
            manager.resolve_commit(root, revision)
        assert error.value.code == "WORKSPACE_BASE_UNRESOLVED", revision


def test_base_is_pinned_even_when_the_user_branch_moves(
    repo: Path, manager: WorkspaceManager
) -> None:
    base = manager.resolve_commit(str(repo), "main")
    path = manager.create(str(repo), "pinned", base, "sentinel/attempt/pinned")
    (repo / "app.py").write_text("user moved on\n", encoding="utf-8")
    git(repo, "commit", "-qam", "user commit")

    assert manager.head(path) == base
    assert (path / "app.py").read_text(encoding="utf-8") == "def value():\n    return 1\n"


# -- results and refs -----------------------------------------------------------------


def test_pinned_result_survives_branch_deletion_worktree_removal_and_gc(
    repo: Path, manager: WorkspaceManager
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "keep", base, "sentinel/attempt/keep")
    (path / "app.py").write_text("keep me\n", encoding="utf-8")
    result = manager.capture(path, base_sha=base, write_paths=[], message="keep")
    ref = manager.pin_result(str(repo), "keep", result.result_sha)

    assert ref == "refs/heads/sentinel/results/keep"
    assert manager.pin_result(str(repo), "keep", result.result_sha) == ref  # same sha is fine
    with pytest.raises(AppError) as conflict:
        manager.pin_result(str(repo), "keep", base)
    assert conflict.value.code == "WORKSPACE_RESULT_REF_CONFLICT"

    manager.remove(str(repo), path, attempt_live=False)
    git(repo, "branch", "-D", "sentinel/attempt/keep")
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "-q", "--prune=now")
    assert git(repo, "show", f"{result.result_sha}:app.py") == "keep me"
    assert manager.read_ref(str(repo), ref) == result.result_sha


def test_registered_worktrees_tracks_create_and_remove(
    repo: Path, manager: WorkspaceManager
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "listed", base, None)
    canonical = WorkspaceManager._canonical(path)
    assert canonical in manager.registered_worktrees(str(repo))
    manager.remove(str(repo), path, attempt_live=False)
    assert canonical not in manager.registered_worktrees(str(repo))


def test_ensure_ref_does_not_overwrite_an_existing_ref(
    repo: Path, manager: WorkspaceManager
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    ws = manager.create(str(repo), "ens", base, "sentinel/attempt/ens")
    (ws / "n.txt").write_text("n\n", encoding="utf-8")
    newer = manager.capture(ws, base_sha=base, write_paths=[], message="n").result_sha
    ref = "refs/heads/sentinel/integration/ensure"
    assert manager.ensure_ref(str(repo), ref, base) == base
    assert manager.ensure_ref(str(repo), ref, newer) == base
    assert manager.read_ref(str(repo), ref) == base
    assert manager.read_ref(str(repo), "refs/heads/sentinel/none") is None


def test_resolution_attempt_completes_a_conflicted_merge(
    repo: Path, manager: WorkspaceManager
) -> None:
    """Phase 5 resolution tasks start from target + conflicting merge; the
    captured result must be a real two-parent merge with markers removed."""

    base = git(repo, "rev-parse", "HEAD")
    one = manager.create(str(repo), "r1", base, "sentinel/attempt/r1")
    two = manager.create(str(repo), "r2", base, "sentinel/attempt/r2")
    (one / "app.py").write_text("def value():\n    return 'one'\n", encoding="utf-8")
    (two / "app.py").write_text("def value():\n    return 'two'\n", encoding="utf-8")
    r1 = manager.capture(one, base_sha=base, write_paths=[], message="one").result_sha
    r2 = manager.capture(two, base_sha=base, write_paths=[], message="two").result_sha

    resolver = manager.create(str(repo), "resolve", r1, "sentinel/attempt/resolve")
    assert manager.merge_into(resolver, r2, "merge r2") == ["app.py"]
    unresolved = manager.capture(resolver, base_sha=r1, write_paths=[], message="unresolved")
    assert unresolved.conflict_markers == ["app.py"]

    # A second resolver actually fixes the markers before capture.
    fixer = manager.create(str(repo), "resolve-2", r1, "sentinel/attempt/resolve-2")
    manager.merge_into(fixer, r2, "merge r2")
    (fixer / "app.py").write_text("def value():\n    return 'both'\n", encoding="utf-8")
    fixed = manager.capture(fixer, base_sha=r1, write_paths=[], message="resolved")
    assert fixed.conflict_markers == []
    parents = git(repo, "rev-list", "--parents", "-n", "1", fixed.result_sha).split()
    assert parents[1:] == [r1, r2]


# -- concurrency and model-based checks ----------------------------------------------


def test_parallel_captures_in_separate_workspaces_are_isolated(
    repo: Path, manager: WorkspaceManager
) -> None:
    import threading

    base = git(repo, "rev-parse", "HEAD")
    count = 4
    paths = [manager.create(str(repo), f"par-{i}", base, f"sentinel/attempt/par-{i}")
             for i in range(count)]
    for index, path in enumerate(paths):
        (path / "app.py").write_text(f"value = {index}\n", encoding="utf-8")
        (path / f"own_{index}.txt").write_text("mine\n", encoding="utf-8")
    barrier = threading.Barrier(count)
    results: dict[int, object] = {}
    errors: list[BaseException] = []

    def work(index: int) -> None:
        try:
            barrier.wait(timeout=30)
            results[index] = manager.capture(paths[index], base_sha=base, write_paths=[],
                                             message=f"par {index}")
        except BaseException as exc:  # surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert errors == []
    assert len({results[i].result_sha for i in range(count)}) == count
    for index in range(count):
        sha = results[index].result_sha
        assert git(repo, "show", f"{sha}:app.py") == f"value = {index}"
        assert sorted(f.path for f in results[index].files) == ["app.py", f"own_{index}.txt"]


def test_randomized_edits_capture_exactly_what_each_workspace_changed(
    repo: Path, manager: WorkspaceManager
) -> None:
    """Fixed-seed model check: for several workspaces sharing one base, the
    captured file set equals the model's expected set, each result's content
    matches what that workspace wrote, and the user checkout never changes."""

    import random

    rng = random.Random(20260922)
    base = git(repo, "rev-parse", "HEAD")
    (repo / "user_dirty.txt").write_text("dirty\n", encoding="utf-8")
    tracked = ["app.py", "old_name.txt"]
    for round_index in range(6):
        path = manager.create(str(repo), f"rand-{round_index}", base,
                              f"sentinel/attempt/rand-{round_index}")
        expected: dict[str, str | None] = {}
        for _ in range(rng.randint(1, 6)):
            action = rng.choice(["add", "modify", "delete"])
            if action == "add":
                name = f"gen/{rng.randint(0, 9)}.txt"
                content = f"r{round_index}-{rng.random()}\n"
                (path / name).parent.mkdir(exist_ok=True)
                (path / name).write_text(content, encoding="utf-8")
                expected[name] = content
            else:
                name = rng.choice(tracked)
                if not (path / name).exists():
                    continue
                if action == "modify":
                    content = f"m{round_index}-{rng.random()}\n"
                    (path / name).write_text(content, encoding="utf-8")
                    expected[name] = content
                else:
                    (path / name).unlink()
                    expected[name] = None
        result = manager.capture(path, base_sha=base, write_paths=[], message="rand")
        assert {f.path for f in result.files} == set(expected), round_index
        for name, content in expected.items():
            if content is None:
                assert next(f for f in result.files if f.path == name).status == "deleted"
            else:
                assert git(repo, "show", f"{result.result_sha}:{name}") == content.rstrip("\n")
    assert git(repo, "rev-parse", "HEAD") == base
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "status", "--porcelain") == "?? user_dirty.txt"


# -- regressions found by the second review pass --------------------------------------


def _with_tracked_key(repo: Path) -> str:
    (repo / "service.key").write_text("original\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tracked key")
    return git(repo, "rev-parse", "HEAD")


def test_staged_rename_of_tracked_secret_never_deletes_it(
    repo: Path, manager: WorkspaceManager
) -> None:
    """`git mv service.key notes.txt` is a staged rename; the working-tree
    status only showed the new name, so the secret used to be committed as
    deleted. The old path must be restored and reported."""

    base = _with_tracked_key(repo)
    path = manager.create(str(repo), "mv-key", base, "sentinel/attempt/mv-key")
    git(path, "mv", "service.key", "notes.txt")

    result = manager.capture(path, base_sha=base, write_paths=[], message="mv")

    assert result.excluded_sensitive == ["service.key"]
    assert git(repo, "show", f"{result.result_sha}:service.key") == "original"
    assert [(f.path, f.status) for f in result.files] == [("notes.txt", "added")]


def test_agent_rm_cached_does_not_break_capture(repo: Path, manager: WorkspaceManager) -> None:
    """`git rm --cached` of a file still on disk left a staged deletion plus
    an untracked entry for one path; the strict status parser rejected that
    as duplicate paths and capture failed outright."""

    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "rm-cached", base, "sentinel/attempt/rm-cached")
    git(path, "rm", "-q", "--cached", "app.py")
    (path / "extra.txt").write_text("e\n", encoding="utf-8")

    result = manager.capture(path, base_sha=base, write_paths=[], message="rm")

    # The file is still on disk, so `add -A` keeps it tracked and unchanged.
    assert [(f.path, f.status) for f in result.files] == [("extra.txt", "added")]
    assert git(repo, "show", f"{result.result_sha}:app.py").startswith("def value")


def test_embedded_repository_is_excluded_and_reported(
    repo: Path, manager: WorkspaceManager
) -> None:
    """An agent that clones or inits a repository inside its workspace used
    to crash capture (the hardened runner refuses gitlinks)."""

    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "nested", base, "sentinel/attempt/nested")
    vendor = path / "vendor" / "lib"
    vendor.mkdir(parents=True)
    git(vendor, "init", "-q", "-b", "main")
    (vendor / "code.py").write_text("x = 1\n", encoding="utf-8")
    git(vendor, "add", "-A")
    git(vendor, "commit", "-q", "-m", "nested")
    (path / "real.txt").write_text("real\n", encoding="utf-8")

    result = manager.capture(path, base_sha=base, write_paths=[], message="nested")

    assert result.excluded_embedded == ["vendor/lib"]
    assert [f.path for f in result.files] == ["real.txt"]
    assert "vendor" not in git(repo, "ls-tree", "-r", "--name-only", result.result_sha)
    assert result.to_manifest()["excluded_embedded"] == ["vendor/lib"]


def test_embedded_repository_alone_is_an_explicit_no_change(
    repo: Path, manager: WorkspaceManager
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "nested-only", base, "sentinel/attempt/nested-only")
    (path / "sub").mkdir()
    git(path / "sub", "init", "-q")
    (path / "sub" / "f.txt").write_text("f\n", encoding="utf-8")

    result = manager.capture(path, base_sha=base, write_paths=[], message="n")

    assert result.no_change is True
    assert result.excluded_embedded == ["sub"]


def test_unstaging_uses_literal_pathspecs(repo: Path, manager: WorkspaceManager) -> None:
    """An excluded name is agent-chosen. As a glob, a directory called `[ab]`
    would also match (and silently unstage) the ordinary file `a`."""

    base = git(repo, "rev-parse", "HEAD")
    path = manager.create(str(repo), "glob", base, "sentinel/attempt/glob")
    (path / "[ab]").mkdir()
    git(path / "[ab]", "init", "-q")
    (path / "[ab]" / "f.txt").write_text("f\n", encoding="utf-8")
    (path / "a").write_text("ordinary file\n", encoding="utf-8")

    result = manager.capture(path, base_sha=base, write_paths=[], message="glob")

    assert result.excluded_embedded == ["[ab]"]
    assert [f.path for f in result.files] == ["a"]


def test_agent_commit_deleting_or_renaming_a_secret_is_reported(
    repo: Path, manager: WorkspaceManager
) -> None:
    base = _with_tracked_key(repo)
    path = manager.create(str(repo), "del-key", base, "sentinel/attempt/del-key")
    git(path, "mv", "service.key", "renamed.txt")
    git(path, "commit", "-q", "--no-verify", "-m", "agent renamed the key away")

    result = manager.capture(path, base_sha=base, write_paths=[], message="c")

    assert result.sensitive_committed == ["service.key"]


def test_parallel_captures_of_identical_content_do_not_race_on_objects(
    repo: Path, manager: WorkspaceManager
) -> None:
    """Regression: worktrees share one object database. Parallel captures
    writing identical blobs used to fail intermittently on Windows with
    "unable to write file .git/objects/...: Permission denied"."""

    import threading

    base = git(repo, "rev-parse", "HEAD")
    for round_index in range(4):
        paths = [manager.create(str(repo), f"same-{round_index}-{i}", base,
                                f"sentinel/attempt/same-{round_index}-{i}") for i in range(4)]
        for path in paths:
            for name in range(25):
                (path / f"gen_{name}.txt").write_text(f"shared {round_index} {name}\n",
                                                      encoding="utf-8")
        barrier = threading.Barrier(len(paths))
        errors: list[BaseException] = []
        results: list[object] = []

        def work(path: Path) -> None:
            try:
                barrier.wait(timeout=30)
                results.append(manager.capture(path, base_sha=base, write_paths=[],
                                               message="same"))
            except BaseException as exc:  # surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(path,)) for path in paths]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        assert errors == [], round_index
        # Identical trees from identical parents at distinct commits.
        trees = {git(repo, "rev-parse", f"{r.result_sha}^{{tree}}") for r in results}
        assert len(trees) == 1 and len(results) == 4
