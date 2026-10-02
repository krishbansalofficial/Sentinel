"""A Change Contract kept in the repository, read only from the Change's baseline commit.

The working tree belongs to the agent once it runs, so ``.sentinel/contract.toml``
is never read from disk: it is read from the baseline commit Sentinel captured
before any agent work, as a regular committed blob (no symlink, no gitlink),
size-bounded, strict UTF-8 TOML, validated as a ``ChangeContract``. An agent's
edit to the file can therefore only take effect after a new baseline is captured.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from backend.app.contracts.models import ChangeContract
from backend.app.core.errors import AppError
from backend.app.git.safe_exec import run_git

CONTRACT_PATH = ".sentinel/contract.toml"
MAX_CONTRACT_BYTES = 65_536
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REGULAR_MODES = frozenset({"100644", "100755"})


@dataclass(frozen=True, slots=True)
class RepositoryContract:
    contract: ChangeContract
    commit: str
    path: str
    blob_sha256: str


def _unavailable(message: str) -> AppError:
    return AppError("REPOSITORY_CONTRACT_UNAVAILABLE", message, status_code=409)


def _invalid(message: str, details: dict[str, object] | None = None) -> AppError:
    return AppError("REPOSITORY_CONTRACT_INVALID", message, status_code=422,
                    details=details or {})


def _git(repository: Path, args: list[str], *, limit: int = 65_536):
    try:
        result = run_git(repository, args, limit=limit)
    except AppError as exc:
        raise _unavailable(f"Git refused to read the baseline commit ({exc.code}).") from exc
    if result.timed_out or result.incomplete:
        raise _unavailable("Git did not finish reading the baseline commit.")
    return result


def read_baseline_contract(repository: str | Path, baseline_sha: str) -> RepositoryContract:
    """The contract committed at ``baseline_sha``; raises a stable ``AppError`` otherwise."""

    if not _SHA.fullmatch(baseline_sha or ""):
        raise _unavailable("The baseline checkpoint records no commit.")
    root = Path(repository)
    resolved = _git(root, ["rev-parse", "--verify", "-q", f"{baseline_sha}^{{commit}}"])
    commit = resolved.stdout.decode("ascii", errors="replace").strip()
    if resolved.returncode != 0 or commit != baseline_sha:
        raise _unavailable("The baseline commit is not in the repository.")
    listed = _git(root, ["ls-tree", "-z", "--full-tree", commit, "--", CONTRACT_PATH])
    if listed.returncode != 0 or listed.truncated:
        raise _unavailable("Git could not list the baseline commit.")
    entries = [item for item in listed.stdout.split(b"\0") if item]
    if not entries:
        raise AppError("REPOSITORY_CONTRACT_MISSING",
                       f"The baseline commit has no {CONTRACT_PATH}.", status_code=404)
    meta, _, name = entries[0].partition(b"\t")
    fields = meta.decode("ascii", errors="replace").split(" ")
    if (len(entries) != 1 or name.decode("utf-8", errors="replace") != CONTRACT_PATH
            or len(fields) != 3 or fields[1] != "blob" or fields[0] not in _REGULAR_MODES):
        raise _invalid(f"{CONTRACT_PATH} must be a regular committed file.")
    blob = _git(root, ["cat-file", "blob", fields[2]], limit=MAX_CONTRACT_BYTES)
    if blob.returncode != 0:
        raise _unavailable(f"Git could not read {CONTRACT_PATH} at the baseline commit.")
    if blob.truncated or len(blob.stdout) > MAX_CONTRACT_BYTES:
        raise _invalid(f"{CONTRACT_PATH} is larger than {MAX_CONTRACT_BYTES} bytes.")
    raw = blob.stdout
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid(f"{CONTRACT_PATH} is not UTF-8.") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _invalid(f"{CONTRACT_PATH} is not valid TOML: {exc}") from exc
    try:
        contract = ChangeContract.model_validate(data)
    except ValidationError as exc:
        problems = [f"{'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
                    for error in exc.errors()[:16]]
        raise _invalid(f"{CONTRACT_PATH} is not a valid Change Contract.",
                       {"problems": problems}) from exc
    return RepositoryContract(contract=contract, commit=commit, path=CONTRACT_PATH,
                              blob_sha256=hashlib.sha256(raw).hexdigest())
