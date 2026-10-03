"""V8 coverage maps onto repository lines conservatively, verified against real Node runs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.app.assurance.v8_coverage import line_coverage, load_coverage_directory, map_reports


def _fn(*ranges: tuple[int, int, int]) -> dict:
    return {"functionName": "f", "isBlockCoverage": True,
            "ranges": [{"startOffset": s, "endOffset": e, "count": c} for s, e, c in ranges]}


def test_innermost_range_decides_and_a_skipped_block_is_not_executed() -> None:
    source = "function f(a) {\n  if (a) {\n    g();\n  }\n}\nf(false);\n"
    block_start = source.index("{\n    g")
    block_end = source.index("}\n}") + 1
    executable, executed = line_coverage(source, [
        _fn((0, len(source), 1)),                      # the whole script ran
        _fn((block_start, block_end, 0)),              # the if-block never ran
    ])
    assert executable == {1, 2, 3, 4, 5, 6}
    assert 3 not in executed and 4 not in executed     # inside the skipped block
    assert 2 not in executed                           # "if (a) {": its "{" did not run
    assert {1, 5, 6} <= executed


def test_comment_and_blank_lines_are_not_executable() -> None:
    source = "// header\n\n/* block\n   comment */\nconst x = 1; // trailing\n"
    executable, executed = line_coverage(source, [_fn((0, len(source), 1))])
    assert executable == {5} and executed == {5}


def test_uncovered_code_is_never_executed() -> None:
    source = "a();\nb();\n"
    executable, executed = line_coverage(source, [_fn((0, 4, 1))])  # only "a();" covered
    assert executable == {1, 2} and executed == {1}
    assert line_coverage(source, []) == ({1, 2}, set())


def test_offsets_are_utf16_code_units() -> None:
    source = 'const s = "\U0001F600";\nrun();\n'   # one astral char = two UTF-16 units
    utf16_len = len(source.encode("utf-16-le")) // 2
    second = source.index("run")
    second_utf16 = len(source[:second].encode("utf-16-le")) // 2
    executable, executed = line_coverage(source, [_fn((0, utf16_len, 1)), _fn((second_utf16, utf16_len, 0))])
    assert executed == {1} and executable == {1, 2}


@pytest.mark.parametrize("ranges", [
    [{"startOffset": "x", "endOffset": 4, "count": 1}], [{"endOffset": 4, "count": 1}],
    [{"startOffset": 9, "endOffset": 2, "count": 1}], [{"startOffset": -5, "endOffset": 999, "count": None}],
])
def test_malformed_ranges_never_count_as_executed(ranges) -> None:
    executable, executed = line_coverage("a();\n", [{"ranges": ranges}])
    assert executable == {1} and executed == set()


def test_only_repository_javascript_is_mapped(tmp_path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "node_modules" / "dep").mkdir(parents=True)
    for rel in ("src/app.js", "node_modules/dep/index.js", "src/data.json"):
        (root / rel).write_text("a();\n", encoding="utf-8")
    outside = tmp_path / "outside.js"
    outside.write_text("a();\n", encoding="utf-8")
    script = lambda path: {"url": Path(path).resolve().as_uri(), "functions": [_fn((0, 5, 1))]}
    report = {"result": [script(root / "src/app.js"), script(root / "node_modules/dep/index.js"),
                         script(root / "src/data.json"), script(outside), {"url": "node:fs", "functions": []},
                         {"url": "evalmachine.<anonymous>", "functions": []}, "junk"]}
    assert map_reports([report], root) == {"src/app.js": {"executed_lines": [1], "missing_lines": []}}


# ------------------------------------------------------------------------- real Node

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node is required")

APP = """\
'use strict';
function used(x) {
  if (x > 10) {
    return 'big';
  }
  return 'small';
}

function unused() {
  return 'never';
}

module.exports = { used, unused };
"""

TEST = """\
const { used } = require('./app.js');
used(1);
"""


def _run_node(root: Path, entry: str) -> Path:
    coverage = root / ".v8"
    coverage.mkdir()
    env = {**os.environ, "NODE_V8_COVERAGE": str(coverage)}
    completed = subprocess.run([NODE, entry], cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    return coverage


@needs_node
def test_real_node_commonjs_run_maps_to_the_lines_that_ran(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.js").write_text(APP, encoding="utf-8")
    (root / "run.js").write_text(TEST, encoding="utf-8")
    files = map_reports(load_coverage_directory(_run_node(root, "run.js")), root)
    app = files["app.js"]
    executed, missing = set(app["executed_lines"]), set(app["missing_lines"])
    assert {2, 6} <= executed                       # used() ran and returned 'small'
    assert {4, 10} <= missing                       # the big branch and unused() never ran
    assert 3 in missing                             # the if-line's block did not fully run
    assert not executed & missing
    assert set(files["run.js"]["executed_lines"]) == {1, 2}


@needs_node
def test_real_node_esm_run_maps_too(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "lib.mjs").write_text("export function hit() { return 1; }\nexport function miss() {\n  return 2;\n}\n",
                                  encoding="utf-8")
    (root / "main.mjs").write_text("import { hit } from './lib.mjs';\nhit();\n", encoding="utf-8")
    files = map_reports(load_coverage_directory(_run_node(root, "main.mjs")), root)
    assert 1 in files["lib.mjs"]["executed_lines"]
    assert 3 in files["lib.mjs"]["missing_lines"]


@needs_node
def test_reports_from_several_processes_merge(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.js").write_text(APP, encoding="utf-8")
    (root / "small.js").write_text("require('./app.js').used(1);\n", encoding="utf-8")
    (root / "big.js").write_text("require('./app.js').used(50);\n", encoding="utf-8")
    coverage = root / ".v8"
    coverage.mkdir()
    for entry in ("small.js", "big.js"):
        subprocess.run([NODE, entry], cwd=root, env={**os.environ, "NODE_V8_COVERAGE": str(coverage)},
                       check=True, capture_output=True, timeout=60)
    app = map_reports(load_coverage_directory(coverage), root)["app.js"]
    assert {4, 6} <= set(app["executed_lines"])     # each branch ran in one of the processes
    assert 10 in app["missing_lines"]


def test_coverage_directory_is_bounded(tmp_path) -> None:
    (tmp_path / "coverage-1.json").write_text(json.dumps({"result": []}), encoding="utf-8")
    (tmp_path / "coverage-2.json").write_text("not json", encoding="utf-8")
    assert load_coverage_directory(tmp_path) == [{"result": []}]
    with pytest.raises(ValueError):
        load_coverage_directory(tmp_path, max_bytes=5)
