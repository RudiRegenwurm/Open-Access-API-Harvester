"""The package version is written down twice. These tests keep the copies honest.

``pyproject.toml`` feeds the distribution metadata; ``harvester.__version__`` feeds
``harvester --version`` and the ``harvester_version`` recorded in every sidecar's
provenance. Nothing derives one from the other, so a release that bumps only one of
them ships an artifact that misreports itself — in the file it hands downstream, no
less. That is the drift these tests exist to catch.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
from pathlib import Path

import pytest

import harvester

DISTRIBUTION = "oa-harvester"
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

#: A release version, so a placeholder or a stray edit is rejected rather than shipped.
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def declared_project_version() -> str:
    """The ``version`` of the ``[project]`` table, read without a TOML dependency.

    ``tomllib`` is stdlib only from 3.11 and this project supports 3.10, so the one
    line that matters is read directly. The search is scoped to ``[project]`` and must
    match exactly once, which is what stops it from picking up some other table's key.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    section = re.split(r"^\[", text, flags=re.MULTILINE)
    project = [part for part in section if part.startswith("project]")]
    assert len(project) == 1, "pyproject.toml must contain exactly one [project] table"
    matches = re.findall(r'^version\s*=\s*"([^"]+)"', project[0], flags=re.MULTILINE)
    assert len(matches) == 1, f"expected one [project] version, found {matches}"
    return matches[0]


def test_the_two_declared_versions_agree():
    """The guard: bumping one file and forgetting the other must fail here."""
    assert PYPROJECT.is_file(), f"{PYPROJECT} is missing"
    assert harvester.__version__ == declared_project_version()


def test_the_version_is_a_release_number():
    assert VERSION_PATTERN.match(harvester.__version__), harvester.__version__


def test_the_installed_distribution_reports_the_same_version():
    """Coherence of the built artifact, not of the working tree.

    An editable install records its version when it is installed, so after a bump its
    metadata legitimately lags until the next ``pip install -e .``. Comparing it then
    would fail for a reason that is not a defect, so that case is skipped explicitly
    rather than quietly weakened. Against a wheel — a clean-install smoke test, or CI —
    there is nothing to excuse and the comparison is exact.
    """
    try:
        distribution = importlib.metadata.distribution(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - not installed
        pytest.skip(f"{DISTRIBUTION} is not installed in this environment")

    direct_url = distribution.read_text("direct_url.json")
    if direct_url and json.loads(direct_url).get("dir_info", {}).get("editable"):
        pytest.skip(
            "editable install: its recorded metadata is a snapshot from install time "
            "and lags a version bump until the project is reinstalled"
        )
    assert distribution.version == harvester.__version__


def test_the_console_script_entry_point_is_declared():
    """The one thing that makes ``harvester`` a command rather than a module."""
    text = PYPROJECT.read_text(encoding="utf-8")
    assert 'harvester = "harvester.cli:main"' in text
    from harvester.cli import main  # the target must actually exist

    assert callable(main)


def test_the_web_ui_assets_are_declared_as_package_data():
    """The Control Center ships as package data; nothing else puts these files in a wheel.

    They are read from the installed package at runtime (``webui.server.STATIC_ROOT``),
    so an installation without them serves "UI assets are missing" instead of a UI.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    assert '"harvester.webui" = ["static/*"]' in text

    from harvester.webui.server import STATIC_ROOT

    present = {path.name for path in STATIC_ROOT.iterdir() if path.is_file()}
    assert {"app.js", "index.html", "styles.css"} <= present
    # The declared glob has no ``**``, so a nested asset would be silently left out of
    # the wheel. Adding one must fail here rather than at a customer's first page load.
    assert not [path for path in STATIC_ROOT.iterdir() if path.is_dir()], (
        "a subdirectory under static/ is not covered by the package-data glob"
    )
