from __future__ import annotations

import json
from pathlib import Path

from harvester.desktop import desktop_data_root, ensure_desktop_config


def test_desktop_data_root_is_visible_under_home(tmp_path: Path):
    assert desktop_data_root(tmp_path) == tmp_path / "Notanda"


def test_desktop_config_creates_absolute_user_owned_defaults(tmp_path: Path):
    root = tmp_path / "Notanda"
    config_path = ensure_desktop_config(root)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["storage_root"] == str(root / "corpus")
    assert data["state_db"] == str(root / "state" / "harvester.sqlite3")
    assert data["reports_dir"] == str(root / "reports")
    assert (root / "corpus").is_dir()
    assert (root / "state").is_dir()
    assert (root / "reports").is_dir()


def test_desktop_config_preserves_user_selected_locations(tmp_path: Path):
    root = tmp_path / "Notanda"
    config_path = root / "config" / "harvester.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({"storage_root": str(tmp_path / "my-corpus"), "openalex": {"api_key": "secret"}}),
        encoding="utf-8",
    )

    ensure_desktop_config(root)
    data = json.loads(config_path.read_text(encoding="utf-8"))

    assert data["storage_root"] == str(tmp_path / "my-corpus")
    assert data["openalex"]["api_key"] == "secret"
    assert data["state_db"] == str(root / "state" / "harvester.sqlite3")
    assert data["reports_dir"] == str(root / "reports")
