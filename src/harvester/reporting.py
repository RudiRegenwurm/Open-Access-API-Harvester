# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""Run reporting (MASTER_SPEC section 31).

Every run ends with a machine-readable summary written to
``<reports_dir>/<run_id>.json`` and a human-readable rendering for the console.
Counters are maintained under a lock because acquisition runs on a thread pool.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .util import to_pretty_json


@dataclass(slots=True)
class RunStats:
    """The mandatory run counters."""

    run_id: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    status: str = "RUNNING"

    records_discovered: int = 0
    records_normalized: int = 0
    duplicates: int = 0
    queued: int = 0
    attempted: int = 0
    downloaded: int = 0
    validated: int = 0
    completed: int = 0
    already_complete: int = 0
    skipped: int = 0
    failed_retryable: int = 0
    failed_permanent: int = 0
    #: Documents that acquired validated XML full text but no PDF. They are counted
    #: among the failures — a PDF is required for COMPLETED (MASTER_SPEC section 16) —
    #: and this is what explains a report with downloads but no completions.
    partial_fulltext: int = 0
    pdf_count: int = 0
    xml_count: int = 0
    bytes_downloaded: int = 0
    retry_count: int = 0
    discovery_pages: int = 0
    reconciled: int = 0

    #: How this run's query was constructed (Assisted Search V1 sections 31-33).
    #: Records the search mode and, for Assisted Search, the research question, the
    #: generated query and the effective query actually executed. Never a credential.
    search_provenance: dict[str, Any] | None = None

    suspend_reason: str | None = None
    suspend_details: dict[str, Any] | None = None
    #: Total structured failures recorded for the run (``failures`` is a bounded sample).
    failure_count: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StatsCollector:
    """Thread-safe counter bag."""

    def __init__(self, run_id: str, started_at: str) -> None:
        self._lock = threading.Lock()
        self.stats = RunStats(run_id=run_id, started_at=started_at)

    def increment(self, field_name: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self.stats, field_name, getattr(self.stats, field_name) + amount)

    def set(self, field_name: str, value: Any) -> None:
        with self._lock:
            setattr(self.stats, field_name, value)

    def add_note(self, note: str) -> None:
        with self._lock:
            if note not in self.stats.notes:
                self.stats.notes.append(note)

    def snapshot(self) -> RunStats:
        with self._lock:
            return RunStats(**asdict(self.stats))


def write_report(reports_dir: Path, stats: RunStats) -> Path:
    """Persist the machine-readable run summary (AC-016)."""
    directory = Path(reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stats.run_id}.json"
    path.write_text(to_pretty_json(stats.to_dict()), encoding="utf-8")
    return path


_SUMMARY_FIELDS = (
    ("records_discovered", "discovered"),
    ("records_normalized", "normalized"),
    ("duplicates", "duplicates"),
    ("queued", "queued"),
    ("attempted", "attempted"),
    ("downloaded", "downloaded"),
    ("validated", "validated"),
    ("completed", "completed"),
    ("already_complete", "already complete"),
    ("skipped", "skipped"),
    ("failed_retryable", "failed (retryable)"),
    ("failed_permanent", "failed (permanent)"),
    ("partial_fulltext", "partial full text (XML, no PDF)"),
    ("pdf_count", "PDF artifacts"),
    ("xml_count", "XML artifacts"),
    ("bytes_downloaded", "bytes downloaded"),
    ("retry_count", "retries"),
    ("reconciled", "reconciled"),
)


def render_summary(stats: RunStats) -> str:
    """Human-readable run summary."""
    lines = [
        f"run {stats.run_id}",
        f"  status            : {stats.status}",
        f"  started           : {stats.started_at}",
        f"  finished          : {stats.finished_at}",
    ]
    if stats.duration_seconds is not None:
        lines.append(f"  duration          : {stats.duration_seconds:.1f}s")
    provenance = stats.search_provenance or {}
    if provenance:
        lines.append(f"  search mode       : {provenance.get('search_mode', 'conventional')}")
        for key, label in (
            ("research_question", "research question"),
            ("generated_query", "generated query"),
            ("effective_search_query", "effective query"),
        ):
            value = provenance.get(key)
            if value:
                lines.append(f"  {label.ljust(17)}: {value}")
        advisor = provenance.get("query_advisor") or {}
        if advisor:
            lines.append(
                f"  advisor           : {advisor.get('provider')} / {advisor.get('model')} "
                f"(prompt {advisor.get('prompt_version')})"
            )
    width = max(len(label) for _, label in _SUMMARY_FIELDS)
    for field_name, label in _SUMMARY_FIELDS:
        lines.append(f"  {label.ljust(width)}: {getattr(stats, field_name)}")
    if stats.suspend_reason:
        lines.append(f"  suspended because : {stats.suspend_reason}")
        if stats.suspend_details:
            for key in sorted(stats.suspend_details):
                lines.append(f"      {key}: {stats.suspend_details[key]}")
    if stats.failures:
        lines.append(f"  failure records   : {stats.failure_count or len(stats.failures)}")
        for failure in stats.failures[:10]:
            lines.append(
                f"      [{failure.get('category')}] {failure.get('document_id') or '-'}: "
                f"{failure.get('message')}"
            )
        remaining = (stats.failure_count or len(stats.failures)) - 10
        if remaining > 0:
            lines.append(f"      ... and {remaining} more")
    for note in stats.notes:
        lines.append(f"  note              : {note}")
    return "\n".join(lines)
