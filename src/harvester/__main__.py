"""Entry point used by standalone desktop packages."""

from __future__ import annotations

from .desktop import main


if __name__ == "__main__":
    raise SystemExit(main())
