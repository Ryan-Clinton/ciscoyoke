"""The published package says the same thing everywhere it says it."""

from __future__ import annotations

import tomllib
from pathlib import Path

from ciscoyoke import __version__

ROOT = Path(__file__).parent.parent
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_the_runtime_version_matches_the_package_version() -> None:
    """`ciscoyoke --version`, a failure record's host facts and PyPI must agree,
    and the release workflow refuses a tag that disagrees with pyproject."""
    assert __version__ == PROJECT["version"]


def test_the_changelog_has_an_entry_for_this_version() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{PROJECT['version']}]" in changelog


def test_the_classifier_matches_the_stated_status() -> None:
    assert "Development Status :: 3 - Alpha" in PROJECT["classifiers"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "alpha" in readme.lower()
