"""Generate the seed eval suite: 30 small Python bug-fix tasks with hidden tests.

    python evals/generate_seed_suite.py          # rewrites evals/tasks/

Six families of real, small bugs, five variants each. Every task has a buggy
``repo/``, ``hidden_tests/check.py`` (plain asserts, stdlib only, so the hidden
command needs nothing installed) and a reference ``solution/`` that the mock
agent uses and the suite test checks: the fixture must fail its hidden tests and
fixture + solution must pass.
"""

from __future__ import annotations

import operator
import shutil
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "tasks"
HIDDEN_COMMAND = '["python", "hidden_tests/check.py"]'


def _family_operator(i: int):
    name, op, wrong, a, b = [("add", "+", "-", 7, 5), ("multiply", "*", "+", 6, 4),
                             ("subtract", "-", "+", 9, 3), ("power", "**", "*", 3, 4),
                             ("floor_div", "//", "/", 17, 5)][i]
    compute = {"+": operator.add, "*": operator.mul, "-": operator.sub, "**": operator.pow,
               "//": operator.floordiv}[op]
    expected = compute(a, b)
    return (f"op-{name}", f"Fix {name}()", f"{name}(a, b) in mathops.py must return a {op} b.",
            {"mathops.py": f"def {name}(a, b):\n    return a {wrong} b\n"},
            {"mathops.py": f"def {name}(a, b):\n    return a {op} b\n"},
            f"from mathops import {name}\nassert {name}({a}, {b}) == {expected!r}\n"
            f"assert {name}(2, 1) == {compute(2, 1)!r}\n")


def _family_off_by_one(i: int):
    n = [5, 10, 3, 8, 12][i]
    return (f"range-sum-{n}", "Fix the off-by-one in sum_to()",
            "sum_to(n) in series.py must return 1 + 2 + ... + n (inclusive of n).",
            {"series.py": "def sum_to(n):\n    return sum(range(1, n))\n"},
            {"series.py": "def sum_to(n):\n    return sum(range(1, n + 1))\n"},
            f"from series import sum_to\nassert sum_to({n}) == {n * (n + 1) // 2}\n"
            "assert sum_to(1) == 1 and sum_to(0) == 0\n")


def _family_strings(i: int):
    name, wrong, right, sample, expected = [
        ("shout", "text.lower()", "text.upper() + '!'", "hi", "HI!"),
        ("reverse", "text", "text[::-1]", "abc", "cba"),
        ("initials", "text[:1]", "''.join(w[0].upper() for w in text.split())", "ada lovelace", "AL"),
        ("strip_spaces", "text.strip()", "''.join(text.split())", " a b  c ", "abc"),
        ("title_words", "text.capitalize()", "text.title()", "hello big world", "Hello Big World"),
    ][i]
    return (f"str-{name.replace('_', '-')}", f"Fix {name}()",
            f"{name}(text) in words.py is wrong; it must turn {sample!r} into {expected!r}.",
            {"words.py": f"def {name}(text):\n    return {wrong}\n"},
            {"words.py": f"def {name}(text):\n    return {right}\n"},
            f"from words import {name}\nassert {name}({sample!r}) == {expected!r}\n")


def _family_empty(i: int):
    name, body_wrong, body_right, default = [
        ("largest", "max(values)", "max(values) if values else None", None),
        ("smallest", "min(values)", "min(values) if values else None", None),
        ("average", "sum(values) / len(values)", "sum(values) / len(values) if values else 0.0", 0.0),
        ("first", "values[0]", "values[0] if values else None", None),
        ("total", "sum(values[1:])", "sum(values)", 0),
    ][i]
    sample = [3, 9, 4]
    expected = {"largest": 9, "smallest": 3, "average": 16 / 3, "first": 3, "total": 16}[name]
    return (f"empty-{name}", f"Make {name}() handle every list",
            f"{name}(values) in stats_utils.py must work for every list, returning {default!r} for "
            "an empty one.",
            {"stats_utils.py": f"def {name}(values):\n    return {body_wrong}\n"},
            {"stats_utils.py": f"def {name}(values):\n    return {body_right}\n"},
            f"from stats_utils import {name}\nassert {name}({sample}) == {expected!r}\n"
            f"assert {name}([]) == {default!r}\n")


def _family_clamp(i: int):
    lo, hi = [(0, 10), (-5, 5), (1, 3), (10, 20), (-100, 0)][i]
    return (f"clamp-{i}", "Fix clamp()",
            f"clamp(x, lo, hi) in bounds.py must keep x within [lo, hi]; it returns the wrong bound.",
            {"bounds.py": "def clamp(x, lo, hi):\n    return max(hi, min(lo, x))\n"},
            {"bounds.py": "def clamp(x, lo, hi):\n    return max(lo, min(hi, x))\n"},
            f"from bounds import clamp\nassert clamp({lo - 7}, {lo}, {hi}) == {lo}\n"
            f"assert clamp({hi + 7}, {lo}, {hi}) == {hi}\nassert clamp({(lo + hi) // 2}, {lo}, {hi}) == "
            f"{(lo + hi) // 2}\n")


def _family_labels(i: int):
    a, b = [(3, 5), (2, 7), (4, 6), (3, 4), (5, 9)][i]
    wrong = textwrap.dedent(f"""\
        def label(n):
            if n % {a} == 0:
                return "Fizz"
            if n % {b} == 0:
                return "Buzz"
            if n % {a} == 0 and n % {b} == 0:
                return "FizzBuzz"
            return str(n)
        """)
    right = textwrap.dedent(f"""\
        def label(n):
            if n % {a} == 0 and n % {b} == 0:
                return "FizzBuzz"
            if n % {a} == 0:
                return "Fizz"
            if n % {b} == 0:
                return "Buzz"
            return str(n)
        """)
    both = a * b
    return (f"fizzbuzz-{a}-{b}", "Fix label() for shared multiples",
            f"label(n) in fizz.py must return FizzBuzz for multiples of both {a} and {b}.",
            {"fizz.py": wrong}, {"fizz.py": right},
            f"from fizz import label\nassert label({both}) == 'FizzBuzz'\nassert label({a}) == 'Fizz'\n"
            f"assert label({b}) == 'Buzz'\nassert label(1) == '1'\n")


FAMILIES = (_family_operator, _family_off_by_one, _family_strings, _family_empty,
            _family_clamp, _family_labels)


def generate(root: Path = ROOT) -> list[str]:
    if root.exists():
        shutil.rmtree(root)
    ids = []
    for family in FAMILIES:
        tags = family.__name__.removeprefix("_family_").replace("_", "-")
        for variant in range(5):
            task_id, title, prompt, buggy, fixed, check = family(variant)
            directory = root / task_id
            for folder, files in (("repo", buggy), ("solution", fixed)):
                for name, text in files.items():
                    path = directory / folder / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
            (directory / "hidden_tests").mkdir(parents=True)
            (directory / "hidden_tests" / "check.py").write_text(
                "import sys\nsys.path.insert(0, '.')\n" + check + "print('ok')\n", encoding="utf-8")
            (directory / "task.toml").write_text(
                f'id = "{task_id}"\ntitle = "{title}"\nprompt = """{prompt}"""\n'
                f"timeout_seconds = 300\nbudget_usd = 0.25\nhidden_test_command = {HIDDEN_COMMAND}\n"
                f'tags = ["python", "bugfix", "{tags}"]\n', encoding="utf-8")
            ids.append(task_id)
    return ids


if __name__ == "__main__":
    print(f"generated {len(generate())} tasks in {ROOT}")
