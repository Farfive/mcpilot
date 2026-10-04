"""TECH-17: one version across the Python package, the TypeScript package and the changelog."""

import json
import re
import tomllib
from pathlib import Path

import mcpilot

ROOT = Path(__file__).resolve().parent.parent


def test_versions_are_consistent_and_documented():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert mcpilot.__version__ == version
    assert json.loads((ROOT / "ts" / "package.json").read_text())["version"] == version
    assert f'VERSION = "{version}"' in (ROOT / "ts" / "src" / "runtime.ts").read_text()
    assert re.search(rf"^## \[{re.escape(version)}\]", (ROOT / "CHANGELOG.md").read_text(), re.M)


def test_ci_runs_both_suites_and_builds_artifacts():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for step in ("python -m pytest", "ruff check", "npm test", "scripts/build_artifacts.sh", '"3.11"', '"3.13"'):
        assert step in workflow
