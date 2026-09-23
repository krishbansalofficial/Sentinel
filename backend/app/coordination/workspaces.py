"""Managed Git worktrees for coordination attempts and integration candidates.

Plan section 9. Every attempt edits in its own worktree on its own branch,
pinned to an explicit committed base SHA, beneath a Sentinel-owned root. The
user's checkout, current branch, and uncommitted files are never read into or
written by these operations: worktrees are created from a commit, not from
the user's working directory.

All Git calls go through `GitRepositoryInspector._capture_git`, the existing
hardened runner (absolute git outside the repository, minimal environment,
content-filter overrides, bounded output and timeout). Repository hooks are
neutralized with `core.hooksPath` pointing at an empty managed directory, and
commits use `--no-verify`.

Limits (plan section 2): a worktree separates normal edits but shares Git
metadata with the source repository and is not a filesystem or network
sandbox. Declared write paths are validated after the run, not enforced
during it.
"""

from __future__ import annotations

import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

from backend.app.assurance.deviations import matches_any
from backend.app.git.adapter import METADATA_LIMIT, GitRepositoryInspector
from backend.app.git.errors import GitCommandError
from backend.app.core.errors import AppError


GIT_SHA = re.compile(r"[0-9a-f]{40}")
SENTINEL_NAME = "Sentinel Coordinator"
SENTINEL_EMAIL = "sentinel@localhost"
# Never captured into an attempt result even when an agent creates them. This
# is a conservative denylist for obvious credential material, not a secret
# scanner; anything excluded is reported so it stays visible.
SENSITIVE_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "id_rsa*", "id_ed25519*",
    ".git-credentials", ".npmrc", ".pypirc", "*.sqlite3-journal",
]
CONFLICT_MARKER = re.compile(rb"^(<{7} |>{7} |={7}$)", re.MULTILINE)
MAX_MARKER_SCAN_BYTES = 2 * 1_048_576
# Upper bound on files in one captured result. A larger change set is refused
# (index restored, worktree untouched) rather than committed or truncated.
MAX_CAPTURE_FILES = 2000
RESULT_REF_PREFIX = "refs/heads/sentinel/results/"


def workspace_error(code: str, message: str, **details: object) -> AppError:
    return AppError(code, message, status_code=409, details=dict(details))


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """`identity` is the canonical common Git directory, shared by every
    worktree of one repository, so resources keyed by it cannot be aliased by
    opening the same repository through a different worktree or path spelling."""

    identity: str
    root: str


@dataclass(frozen=True, slots=True)
class CapturedFile:
    path: str
    status: str
    old_path: str | None = None
    binary: bool = False


@dataclass(frozen=True, slots=True)
class CaptureResult:
    base_sha: str
    result_sha: str
    no_change: bool
    files: list[CapturedFile] = field(default_factory=list)
    scope_violations: list[str] = field(default_factory=list)
    excluded_sensitive: list[str] = field(default_factory=list)
    conflict_markers: list[str] = field(default_factory=list)
    sensitive_committed: list[str] = field(default_factory=list)
    excluded_embedded: list[str] = field(default_factory=list)

    def to_manifest(self) -> dict[str, object]:
        """JSON-safe manifest stored with the workspace record."""

        return {
            "base_sha": self.base_sha,
            "result_sha": self.result_sha,
            "no_change": self.no_change,
            "files": [
                {"path": f.path, "status": f.status, "old_path": f.old_path, "binary": f.binary}
                for f in self.files
            ],
            "scope_violations": list(self.scope_violations),
            "excluded_sensitive": list(self.excluded_sensitive),
            "conflict_markers": list(self.conflict_markers),
            "sensitive_committed": list(self.sensitive_committed),
            "excluded_embedded": list(self.excluded_embedded),
        }


class WorkspaceManager:
    # Shared by every manager in the process: the object database belongs to
    # the repository, not to one manager instance.
    _locks_guard = threading.Lock()
    _object_locks: dict[str, threading.Lock] = {}

    def __init__(self, managed_root: str | Path) -> None:
        root = Path(managed_root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        self.managed_root = root.resolve(strict=True)
        self._hooks_dir = self.managed_root / ".empty-hooks"
        self._hooks_dir.mkdir(exist_ok=True)

    # -- repository facts -------------------------------------------------

    def repository_identity(self, repository_path: str) -> RepositoryIdentity:
        inspector = GitRepositoryInspector()
        root = inspector._canonical_root(repository_path)
        common = self._git(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
        identity = self._canonical(Path(common.strip()))
        return RepositoryIdentity(identity=identity, root=root)

    def resolve_commit(self, root: str, revision: str) -> str:
        try:
            sha = self._git(root, ["rev-parse", "--verify", "--end-of-options",
                                   f"{revision}^{{commit}}"]).strip()
        except GitCommandError as exc:
            raise workspace_error("WORKSPACE_BASE_UNRESOLVED",
                                  "The base revision is not a commit in this repository.",
                                  revision=revision) from exc
        if not GIT_SHA.fullmatch(sha):
            raise GitCommandError("Git returned an invalid commit id.")
        return sha

    def read_ref(self, root: str, ref: str) -> str | None:
        try:
            sha = self._git(root, ["rev-parse", "--verify", "--quiet", "--end-of-options",
                                   f"{ref}^{{commit}}"]).strip()
        except GitCommandError as exc:
            if exc.details.get("exit_code") == 1:
                return None
            raise
        return sha or None

    def ensure_ref(self, root: str, ref: str, sha: str) -> str:
        """Create `ref` at `sha` only if it does not exist; return its value."""

        self._require_managed_ref(ref)
        existing = self.read_ref(root, ref)
        if existing is not None:
            return existing
        # An all-zero old value makes creation itself a compare-and-swap.
        self._git(root, ["update-ref", ref, sha, "0" * 40])
        return sha

    def pin_result(self, root: str, workspace_id: str, sha: str) -> str:
        """Keep `sha` reachable under a managed result ref, independent of the
        attempt branch (which an agent can move) and of the worktree (which
        cleanup removes). Pinning is create-once: an existing ref must
        already name the same commit."""

        ref = RESULT_REF_PREFIX + workspace_id
        existing = self.ensure_ref(root, ref, sha)
        if existing != sha:
            raise workspace_error("WORKSPACE_RESULT_REF_CONFLICT",
                                  "The workspace result is already pinned to a different commit.",
                                  ref=ref, pinned_sha=existing, result_sha=sha)
        return ref

    def compare_and_swap_ref(self, root: str, ref: str, new_sha: str, expected_old: str) -> bool:
        """Advance `ref` only if it still points at `expected_old`."""

        self._require_managed_ref(ref)
        try:
            self._git(root, ["update-ref", ref, new_sha, expected_old])
        except GitCommandError as exc:
            if "exit_code" in exc.details:
                return False
            raise
        return True

    # -- lifecycle ----------------------------------------------------------

    def create(
        self, repository_root: str, workspace_id: str, base_sha: str, branch: str | None,
    ) -> Path:
        if not GIT_SHA.fullmatch(base_sha):
            raise workspace_error("WORKSPACE_BASE_UNRESOLVED", "The base must be a full commit id.")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", workspace_id):
            raise workspace_error("WORKSPACE_ID_INVALID", "The workspace id is invalid.")
        path = self.managed_root / workspace_id
        if path.exists():
            raise workspace_error("WORKSPACE_EXISTS", "The workspace path already exists.",
                                  workspace_id=workspace_id)
        args = ["worktree", "add", "--quiet"]
        if branch is not None:
            self._require_managed_ref(f"refs/heads/{branch}")
            args += ["-b", branch]
        else:
            args += ["--detach"]
        args += ["--end-of-options", str(path), base_sha]
        self._git(repository_root, args)
        return path

    def merge_into(self, path: Path, source_sha: str, message: str) -> list[str]:
        """Merge `source_sha` into the worktree HEAD. Returns conflicted paths
        (empty on a clean merge). A conflicted merge is left in progress so a
        resolution attempt can edit the markers; callers that only want to
        detect conflicts call `abort_merge` afterwards."""

        self._require_worktree(path)
        with self._object_lock(path):
            return self._merge_locked(path, source_sha, message)

    def _merge_locked(self, path: Path, source_sha: str, message: str) -> list[str]:
        try:
            self._git(str(path), [*self._identity_args(), "merge", "--no-ff", "--no-edit",
                                  "-m", message, "--end-of-options", source_sha])
            return []
        except GitCommandError as exc:
            if "exit_code" not in exc.details:
                raise
        conflicted = self._git(str(path), ["diff", "--name-only", "-z", "--diff-filter=U"])
        paths = [item for item in conflicted.split("\0") if item]
        if not paths:
            raise GitCommandError("The merge failed without reporting conflicts.")
        return paths

    def abort_merge(self, path: Path) -> None:
        self._git(str(path), ["merge", "--abort"])

    def head(self, path: Path) -> str:
        return self._git(str(path), ["rev-parse", "--verify", "HEAD"]).strip()

    def tracked_modifications(self, path: Path) -> list[str]:
        """Tracked files changed relative to HEAD (untracked build output is
        not counted: the candidate commit is what gets advanced)."""

        output = self._git(str(path), ["diff", "--name-only", "-z", "HEAD", "--"])
        return [item for item in output.split("\0") if item]

    def is_ancestor(self, root: str, ancestor: str, descendant: str) -> bool:
        try:
            self._git(root, ["merge-base", "--is-ancestor", ancestor, descendant])
        except GitCommandError as exc:
            if exc.details.get("exit_code") == 1:
                return False
            raise
        return True

    def registered_worktrees(self, repository_root: str) -> set[str]:
        """Canonical paths of every worktree Git currently knows about."""

        output = self._git(repository_root, ["worktree", "list", "--porcelain", "-z"])
        return {
            self._canonical(Path(item[len("worktree "):]))
            for item in output.split("\0") if item.startswith("worktree ")
        }

    def capture(self, path: Path, *, base_sha: str, write_paths: list[str],
                message: str, max_files: int = MAX_CAPTURE_FILES) -> CaptureResult:
        """Commit the attempt's edits as an immutable result.

        Call only after the attempt's process exit is confirmed. Sensitive
        files are left out of the commit and reported. Declared write-path
        violations and leftover conflict markers are reported, not silently
        accepted; the caller decides whether the result is acceptable.

        Refused outright, with nothing committed: a worktree whose Git link was
        broken or redirected, a HEAD that no longer descends from the pinned
        base (the agent reset or switched branches), and a change set larger
        than `max_files`.
        """

        self._require_worktree(path)
        with self._object_lock(path):
            return self._capture_locked(path, base_sha=base_sha, write_paths=write_paths,
                                        message=message, max_files=max_files)

    def _capture_locked(self, path: Path, *, base_sha: str, write_paths: list[str],
                        message: str, max_files: int) -> CaptureResult:
        head = self.head(path)
        if not self.is_ancestor(str(path), base_sha, head):
            raise workspace_error("WORKSPACE_BASE_DIVERGED",
                                  "The workspace HEAD no longer descends from its pinned base.",
                                  base_sha=base_sha, head_sha=head)
        worktree = str(path)
        # Repositories an agent created or cloned inside its workspace would
        # be staged as gitlinks, which the hardened runner refuses to inspect.
        # They are left out of the result and reported.
        # They are excluded from `add` itself: one without a commit makes
        # `git add` fail outright, so unstaging afterwards is too late.
        embedded = self._embedded_repositories(worktree)
        self._git(worktree, ["add", "-A", "--", ".",
                             *(f":(exclude,literal){name}" for name in embedded)])
        # Read what is staged (with rename detection) rather than the working
        # tree status: this sees both sides of an agent's `git mv`, and it
        # tolerates index states such as `git rm --cached` of a file that is
        # still on disk.
        excluded = sorted({
            name
            for change in self._name_status(worktree, ["--cached", "HEAD"])
            for name in (change.path, change.old_path)
            if name and matches_any(name, SENSITIVE_PATTERNS)
        })
        if excluded:
            # Restore the index entry to HEAD: an untracked secret leaves the
            # index, a tracked one keeps its committed content (never a
            # deletion, even when the agent renamed it away).
            self._unstage(worktree, excluded)
        merging = self._merge_in_progress(path)
        staged = {item for item in self._git(
            worktree, ["diff", "--cached", "--name-only", "-z", "HEAD", "--"]).split("\0") if item}
        # Commits the agent (or an interrupted earlier capture) already made
        # count toward the bound too: the result is everything since base.
        committed = {item for item in self._git(
            worktree, ["diff", "--name-only", "-z", base_sha, head, "--"]).split("\0") if item}
        file_count = len(staged | committed)
        if file_count > max_files:
            self._git(worktree, ["reset", "-q", "HEAD", "--", "."])
            raise workspace_error("WORKSPACE_RESULT_TOO_LARGE",
                                  "The attempt changed more files than one result may hold.",
                                  file_count=file_count, limit=max_files)
        if staged or merging:
            self._git(worktree, [*self._identity_args(), "commit", "--quiet", "--no-verify",
                                 "--allow-empty", "-m", message])
        result_sha = self.head(path)
        # "No change" means the result is the base itself, not "nothing was
        # staged": an agent that committed its own edits still has a result.
        if result_sha == base_sha:
            return CaptureResult(base_sha=base_sha, result_sha=result_sha, no_change=True,
                                 excluded_sensitive=excluded, excluded_embedded=embedded)
        files = self._diff_files(worktree, base_sha, result_sha)
        violations = (
            [f.path for f in files if not matches_any(f.path, write_paths)]
            if write_paths else []
        )
        markers = [
            f.path for f in files
            if f.status != "deleted" and not f.binary and self._has_markers(path / f.path)
        ]
        # Exclusion only covers uncommitted work. A sensitive file that the
        # agent's own commits added, changed, deleted, or renamed away cannot
        # be undone without rewriting its history, so it is reported instead.
        sensitive_committed = sorted({
            name
            for f in files
            for name in (f.path, f.old_path)
            if name and matches_any(name, SENSITIVE_PATTERNS)
        })
        return CaptureResult(
            base_sha=base_sha, result_sha=result_sha, no_change=False, files=files,
            scope_violations=violations, excluded_sensitive=excluded, conflict_markers=markers,
            sensitive_committed=sensitive_committed, excluded_embedded=embedded,
        )

    def remove(self, repository_root: str, path: str | Path, *, attempt_live: bool) -> None:
        """Remove a managed worktree. Refuses anything outside the managed
        root (after resolving junctions/aliases) and any live attempt's
        workspace. Never trusts an agent-supplied path."""

        if attempt_live:
            raise workspace_error("WORKSPACE_ACTIVE",
                                  "An active attempt's workspace cannot be removed.")
        target = self._contained(path)
        try:
            self._git(repository_root, ["worktree", "remove", "--force", "--end-of-options",
                                        str(target)])
        except GitCommandError:
            # Git no longer knows the worktree (for example it was pruned);
            # remove the directory only because containment was proven above.
            if target.exists():
                shutil.rmtree(target)
            self._git(repository_root, ["worktree", "prune"])

    def prune(self, repository_root: str) -> None:
        """Drop Git's records of worktrees whose directories no longer exist."""

        self._git(repository_root, ["worktree", "prune"])

    def contains(self, path: str | Path) -> bool:
        try:
            self._contained(path)
        except AppError:
            return False
        return True

    # -- internals ----------------------------------------------------------

    def _object_lock(self, path: Path) -> threading.Lock:
        """One lock per shared object database (canonical common Git dir).

        Worktrees share `.git/objects`. On Windows, two Git processes writing
        the same loose object at once (two agents producing a file with
        identical content) can fail with "Permission denied"; commits and
        merges into one repository are therefore serialized here."""

        common = self._git(str(path), ["rev-parse", "--path-format=absolute",
                                       "--git-common-dir"]).strip()
        key = self._canonical(Path(common))
        with WorkspaceManager._locks_guard:
            return WorkspaceManager._object_locks.setdefault(key, threading.Lock())

    def _require_worktree(self, path: Path) -> None:
        """Refuse to run Git in a workspace whose `.git` link is missing or
        points elsewhere: Git would otherwise discover an enclosing
        repository and commit into it."""

        try:
            top = self._git(str(path), ["rev-parse", "--show-toplevel"]).strip()
        except GitCommandError as exc:
            raise workspace_error("WORKSPACE_CORRUPT",
                                  "The workspace is no longer a Git worktree.") from exc
        if self._canonical(Path(top)) != self._canonical(Path(path)):
            raise workspace_error("WORKSPACE_CORRUPT",
                                  "The workspace resolves to a different Git worktree.",
                                  toplevel=top)

    def _contained(self, path: str | Path) -> Path:
        try:
            resolved = Path(path).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise workspace_error("WORKSPACE_NOT_FOUND", "The workspace does not exist.") from exc
        canonical_root = self._canonical(self.managed_root)
        canonical = self._canonical(resolved)
        if canonical == canonical_root or not canonical.startswith(canonical_root + "/"):
            raise workspace_error("WORKSPACE_NOT_MANAGED",
                                  "The path is not inside Sentinel's managed workspace root.")
        return resolved

    @staticmethod
    def _canonical(path: Path) -> str:
        # resolve() follows junctions and symlinks; casefold matches Windows'
        # case-insensitive filesystem so two spellings cannot alias.
        return str(path.resolve()).replace("\\", "/").rstrip("/").casefold()

    @staticmethod
    def _require_managed_ref(ref: str) -> None:
        if not re.fullmatch(r"refs/heads/sentinel/[A-Za-z0-9/_-]{1,200}", ref):
            raise workspace_error("WORKSPACE_REF_NOT_MANAGED",
                                  "Coordination may only write Sentinel-managed refs.", ref=ref)

    def _merge_in_progress(self, path: Path) -> bool:
        git_path = self._git(str(path), ["rev-parse", "--git-path", "MERGE_HEAD"]).strip()
        candidate = Path(git_path)
        if not candidate.is_absolute():
            candidate = path / candidate
        return candidate.exists()

    def _name_status(self, worktree: str, revisions: list[str]) -> list[CapturedFile]:
        """Parse `git diff --name-status -z -M` for `revisions` (binary flags
        are not filled in here)."""

        raw = self._git(worktree, ["diff", "--no-ext-diff", "--no-textconv", "--name-status",
                                   "-z", "-M", *revisions, "--"])
        fields = raw.split("\0")[:-1] if raw else []
        files: list[CapturedFile] = []
        index = 0
        names = {"A": "added", "M": "modified", "D": "deleted", "T": "type_changed"}
        while index < len(fields):
            code = fields[index]
            index += 1
            if code[:1] in {"R", "C"}:
                old, new = fields[index], fields[index + 1]
                index += 2
                files.append(CapturedFile(new, "renamed" if code[0] == "R" else "copied",
                                          old_path=old))
                continue
            files.append(CapturedFile(fields[index], names.get(code[:1], "modified")))
            index += 1
        return files

    def _diff_files(self, worktree: str, base_sha: str, result_sha: str) -> list[CapturedFile]:
        numstat = GitRepositoryInspector._parse_numstat(self._git(worktree, [
            "diff", "--no-ext-diff", "--no-textconv", "--numstat", "-z", "-M",
            base_sha, result_sha, "--",
        ]))
        files = []
        for item in self._name_status(worktree, [base_sha, result_sha]):
            stat = numstat.get(item.path)
            files.append(CapturedFile(item.path, item.status, old_path=item.old_path,
                                      binary=bool(stat and stat.binary)))
        return files

    def _unstage(self, worktree: str, paths: list[str]) -> None:
        # Literal pathspecs: an agent-chosen file name such as `*.key` must
        # name only itself, never act as a glob over other files.
        self._git(worktree, ["--literal-pathspecs", "reset", "-q", "HEAD", "--", *paths])

    def _embedded_repositories(self, worktree: str) -> list[str]:
        """Untracked directories that are Git repositories of their own; Git
        lists each as a single `dir/` entry instead of its files."""

        output = self._git(worktree, ["ls-files", "-z", "--others", "--exclude-standard"])
        return sorted(item.rstrip("/") for item in output.split("\0") if item.endswith("/"))

    @staticmethod
    def _has_markers(file_path: Path) -> bool:
        try:
            with file_path.open("rb") as handle:
                data = handle.read(MAX_MARKER_SCAN_BYTES)
        except OSError:
            return False
        return b"\0" not in data and CONFLICT_MARKER.search(data) is not None

    @staticmethod
    def _identity_args() -> list[str]:
        return ["-c", f"user.name={SENTINEL_NAME}", "-c", f"user.email={SENTINEL_EMAIL}",
                "-c", "commit.gpgsign=false"]

    def _git(self, root: str, args: list[str]) -> str:
        full = ["-c", f"core.hooksPath={self._hooks_dir}", *args]
        # `_capture_git` runs its filter/attribute preflight only when args[0]
        # is status/diff; keep that preflight for those commands.
        if args[0] in {"status", "diff"}:
            full = args
        result = GitRepositoryInspector._capture_git(root, full, METADATA_LIMIT)
        if result.truncated:
            raise GitCommandError("Git output exceeded the coordination limit.")
        return result.stdout.decode("utf-8", errors="replace")


__all__ = [
    "CaptureResult", "CapturedFile", "MAX_CAPTURE_FILES", "RESULT_REF_PREFIX",
    "RepositoryIdentity", "SENSITIVE_PATTERNS", "WorkspaceManager",
]
