"""Map Node's V8 coverage (``NODE_V8_COVERAGE``) onto repository line numbers.

This is the measurement layer for JavaScript diff coverage: it reads the
per-script function/block ranges V8 writes, resolves each offset to the count
of its innermost covering range, and reports per file which lines are
executable and which ran. It is deliberately conservative, because an
over-reported executed line would let a diff claim pass:

* a line is executable when it holds code (not only whitespace or a comment);
* a line counts as executed only when every code character on it ran, so a
  line with a skipped branch (``if (a) { b() }`` with ``a`` false) is NOT
  executed;
* offsets are UTF-16 code units, as V8 reports them;
* only ``file:`` scripts strictly inside the repository root are mapped, never
  ``node_modules`` or anything outside it.

The output uses the coverage.py JSON shape (``executed_lines`` /
``missing_lines``), which ``diff_coverage.evaluate_report`` already consumes.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

MAX_SOURCE_BYTES = 4 * 1024 * 1024
_JS_SUFFIXES = (".js", ".mjs", ".cjs")


def _utf16_index(source: str) -> list[int]:
    """For each UTF-16 code unit offset, the index of the Python character it belongs to."""
    index: list[int] = []
    for position, char in enumerate(source):
        index.extend([position] * (2 if ord(char) > 0xFFFF else 1))
    return index


def _code_mask(source: str) -> list[bool]:
    """True for characters that are code: not whitespace and not inside a comment.

    Strings, template literals and regex literals are not parsed; a ``//`` or
    ``/*`` inside one can hide code from the mask, which only makes a line
    less likely to count as executable (never as executed when it did not run).
    """
    mask = [False] * len(source)
    i, n = 0, len(source)
    while i < n:
        if source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end == -1 else end
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        mask[i] = not source[i].isspace()
        i += 1
    return mask


def line_coverage(source: str, functions: Iterable[Mapping[str, Any]]) -> tuple[set[int], set[int]]:
    """(executable, executed) 1-based line numbers of ``source`` under V8 ``functions``."""
    units = _utf16_index(source)
    counts: list[int | None] = [None] * len(source)
    span: list[int] = [len(units) + 1] * len(source)  # size of the range that set each count
    for function in functions:
        for item in function.get("ranges") or ():
            try:
                start, end, count = int(item["startOffset"]), int(item["endOffset"]), int(item["count"])
            except (KeyError, TypeError, ValueError):
                continue
            start, end = max(start, 0), min(end, len(units))
            if start >= end:
                continue
            size = end - start
            first, last = units[start], units[end - 1]
            for position in range(first, last + 1):
                if size <= span[position]:  # the innermost range wins
                    span[position], counts[position] = size, count
    mask = _code_mask(source)
    executable: set[int] = set()
    unexecuted: set[int] = set()
    line = 1
    for position, char in enumerate(source):
        if mask[position]:
            executable.add(line)
            if not counts[position]:  # None (not covered by any range) or 0
                unexecuted.add(line)
        if char == "\n":
            line += 1
    return executable, executable - unexecuted


def _repository_path(url: str, root: Path) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    raw = unquote(parsed.path)
    if os.name == "nt" and len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    try:
        relative = Path(os.path.normcase(os.path.abspath(raw))).relative_to(
            Path(os.path.normcase(os.path.abspath(root))))
    except ValueError:
        return None
    parts = relative.parts
    if not parts or ".." in parts or "node_modules" in (part.lower() for part in parts):
        return None
    if not relative.name.lower().endswith(_JS_SUFFIXES):
        return None
    return relative.as_posix()


def _read_source(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
            return None
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError):
        return None


def map_reports(reports: Iterable[Mapping[str, Any]], root: Path, *,
                read: Callable[[Path], str | None] = _read_source) -> dict[str, dict[str, list[int]]]:
    """Merge V8 coverage reports into a coverage.py-shaped ``files`` mapping for ``root``.

    A line is executed when any report executed it; scripts that cannot be read
    or mapped are skipped (their lines stay unmeasured, never executed).
    """
    merged: dict[str, tuple[set[int], set[int]]] = {}
    for report in reports:
        for script in report.get("result") or ():
            if not isinstance(script, Mapping) or not isinstance(script.get("url"), str):
                continue
            relative = _repository_path(script["url"], root)
            if relative is None:
                continue
            source = read(root / relative)
            if source is None:
                continue
            executable, executed = line_coverage(source, script.get("functions") or ())
            known_exec, known_ran = merged.get(relative, (set(), set()))
            merged[relative] = (known_exec | executable, known_ran | executed)
    return {path: {"executed_lines": sorted(ran),
                   "missing_lines": sorted(executable - ran)}
            for path, (executable, ran) in sorted(merged.items())}


def load_coverage_directory(directory: Path, *, max_files: int = 256,
                            max_bytes: int = 64 * 1024 * 1024) -> list[dict[str, Any]]:
    """The ``coverage-*.json`` reports Node wrote into ``directory``, bounded."""
    reports: list[dict[str, Any]] = []
    total = 0
    for path in sorted(directory.glob("coverage-*.json"))[:max_files]:
        if path.is_symlink() or not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        if total > max_bytes:
            raise ValueError("V8 coverage output exceeds the size bound")
        try:
            data = json.loads(path.read_bytes())
        except (ValueError, UnicodeError):
            continue
        if isinstance(data, dict):
            reports.append(data)
    return reports
