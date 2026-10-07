"""Release surface: the license, the package metadata and the PyPI release workflow agree."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_the_project_is_apache_2_licensed_in_every_place_it_says_so() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License\n                           Version 2.0, January 2004" in license_text
    assert "Copyright 2026 Krish Bansal" in (ROOT / "NOTICE").read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE", "NOTICE"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Apache License 2.0" in readme and "pending" not in readme.split("##")[1]


def test_the_distribution_name_is_the_one_the_runtime_reports() -> None:
    from backend.app.policy.version import DISTRIBUTION

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == DISTRIBUTION == "sentinel-runtime"
    excluded = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert excluded["tool"]["setuptools"]["packages"]["find"]["exclude"] == ["backend.tests*"]


def test_release_workflow_publishes_with_trusted_publishing_only_from_tags() -> None:
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    publish = text[text.index("  publish:"):]
    assert "tags: ['v*']" in text
    assert "if: startsWith(github.ref, 'refs/tags/v')" in publish
    assert "id-token: write" in publish and "pypa/gh-action-pypi-publish" in publish
    assert "password" not in text and "secrets." not in text  # OIDC, never a stored token
    assert "twine check --strict" in text and 'test "v$version" = "$GITHUB_REF_NAME"' in text
