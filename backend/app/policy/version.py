"""Product version read from installed package metadata, the release source of truth."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib

SOURCE_VERSION_FALLBACK = "0+source"
# The distribution name on PyPI; on-disk names (the store, the database) keep change_assurance.
DISTRIBUTION = "sentinel-runtime"


def product_version() -> str:
    try:
        return version(DISTRIBUTION)
    except PackageNotFoundError:
        source = Path(__file__).resolve().parents[3] / "pyproject.toml"
        try:
            return str(tomllib.loads(source.read_text(encoding="utf-8"))["project"]["version"])
        except (OSError, ValueError, KeyError, TypeError):
            return SOURCE_VERSION_FALLBACK
