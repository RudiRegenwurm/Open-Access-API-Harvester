"""Desktop launcher support for standalone Notanda packages.

The wheel/CLI deliberately keeps its historical relative defaults. A desktop installer
cannot: launchers may start with an arbitrary working directory and installed program
directories may be read-only. This module creates explicit per-user defaults once and
then lets the normal configuration loader and Settings UI take over.
"""

from __future__ import annotations

import os
from pathlib import Path

from .webui import serve as serve_ui
from .webui.settings_store import read_file, write_file

APP_DIR_NAME = "Notanda"


def desktop_data_root(home: Path | None = None) -> Path:
    """Return the visible, user-owned root used by the desktop application."""
    return (Path.home() if home is None else Path(home)) / APP_DIR_NAME


def ensure_desktop_config(data_root: Path) -> Path:
    """Create stable writable defaults without overwriting user choices."""
    root = Path(data_root)
    config_path = root / "config" / "harvester.json"
    defaults = {
        "storage_root": str(root / "corpus"),
        "state_db": str(root / "state" / "harvester.sqlite3"),
        "reports_dir": str(root / "reports"),
    }

    data = read_file(config_path)
    changed = False
    for key, value in defaults.items():
        if key not in data:
            data[key] = value
            changed = True

    for directory in (
        config_path.parent,
        root / "corpus",
        (root / "state"),
        (root / "reports"),
    ):
        directory.mkdir(parents=True, exist_ok=True)

    if changed or not config_path.exists():
        write_file(config_path, data)

    # Best effort on POSIX. Windows inherits the user's profile ACL; chmod semantics
    # there do not provide an equivalent privacy guarantee and are intentionally not
    # presented as one.
    if os.name == "posix":
        try:
            os.chmod(config_path.parent, 0o700)
            os.chmod(config_path, 0o600)
        except OSError:
            pass

    return config_path


def main() -> int:
    """Start the browser-based control center as a desktop application."""
    config_path = ensure_desktop_config(desktop_data_root())
    return serve_ui(
        config_path=config_path,
        host="127.0.0.1",
        port=8765,
        open_browser=True,
    )


__all__ = ["APP_DIR_NAME", "desktop_data_root", "ensure_desktop_config", "main"]
