# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""Logging for unattended operation (MASTER_SPEC section 30).

Human-readable output by default; structured JSON lines with ``--log-format json``.
A redaction filter is installed on every handler as a defence in depth: even if a
call site forgets :func:`harvester.http.redact_url`, an API key or contact address
cannot reach the log (AC-017).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

_SECRET_PATTERNS = [
    re.compile(r"((?:api_key|apikey|access_token|token|key)=)([^&\s\"']+)", re.IGNORECASE),
    re.compile(r"(email=)([^&\s\"']+)", re.IGNORECASE),
    re.compile(r"(mailto:)([^\s\"'<>)]+)", re.IGNORECASE),
    # Bare address anywhere in a message.
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
]


def scrub(text: str) -> str:
    """Mask secrets in an arbitrary log string."""
    result = text
    for pattern in _SECRET_PATTERNS[:-1]:
        result = pattern.sub(lambda m: f"{m.group(1)}REDACTED", result)
    return _SECRET_PATTERNS[-1].sub("REDACTED", result)


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            return True
        scrubbed = scrub(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }
        for key in ("run_id", "document_id", "source", "operation"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(
    level: str = "INFO",
    *,
    log_format: str = "text",
    log_file: Path | None = None,
    stream: Any = None,
) -> None:
    """Install the harvester's logging configuration (idempotent)."""
    root = logging.getLogger("harvester")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.propagate = False

    formatter: logging.Formatter
    if log_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"
        )

    console = logging.StreamHandler(stream if stream is not None else sys.stderr)
    console.setFormatter(formatter)
    console.addFilter(RedactionFilter())
    root.addHandler(console)

    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(RedactionFilter())
        root.addHandler(file_handler)
