# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""Background execution of harvest, resume and verify operations.

Each operation is executed by the existing core service on a worker thread. The
manager owns no harvesting logic: it starts the operation, remembers what is running
so the UI can say so, and lets the state store remain the source of truth for
everything else.

One operation runs at a time. The state store's transactional claiming already makes
concurrent runs safe, but a single active operation keeps the activity panel
unambiguous and matches how one operator actually works.
"""

from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config
from ..errors import HarvesterError
from ..http import ClientPool
from ..orchestrator import Harvester
from ..providers.openalex import OpenAlexQuery
from ..state import StateStore
from ..util import utc_now_iso
from ..verify import verify_corpus

LOGGER = logging.getLogger("harvester.webui.runmanager")


class BusyError(RuntimeError):
    """Raised when an operation is requested while another one is running."""


@dataclass
class ActiveOperation:
    """What the UI needs in order to describe the operation in flight."""

    kind: str                      # "harvest" | "dry_run" | "resume" | "verify"
    label: str
    started_at: str
    run_id: str | None = None
    phase: str = "starting"
    finished: bool = False
    ok: bool | None = None
    message: str | None = None
    result: dict[str, Any] | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "started_at": self.started_at,
            "run_id": self.run_id,
            "phase": self.phase,
            "finished": self.finished,
            "ok": self.ok,
            "message": self.message,
            "result": self.result,
            "detail": dict(self.detail),
        }


class RunManager:
    """Runs one core operation at a time on a worker thread."""

    def __init__(self, config_provider: Callable[[], Config]) -> None:
        self._config_provider = config_provider
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._active: ActiveOperation | None = None
        self._last: ActiveOperation | None = None

    # ------------------------------------------------------------------ state

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._active is not None and not self._active.finished

    def snapshot(self) -> dict[str, Any] | None:
        """The operation in flight, or the most recent one once it has finished."""
        with self._lock:
            current = self._active or self._last
            return current.to_dict() if current else None

    def _begin(self, operation: ActiveOperation) -> None:
        with self._lock:
            if self._active is not None and not self._active.finished:
                raise BusyError(
                    f"{self._active.label} is still running. Wait for it to finish, "
                    "then try again."
                )
            self._active = operation

    def _finish(
        self,
        operation: ActiveOperation,
        *,
        ok: bool,
        message: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            operation.finished = True
            operation.ok = ok
            operation.message = message
            operation.result = result
            operation.phase = "finished"
            self._last = operation
            self._active = None

    def _spawn(self, operation: ActiveOperation, target: Callable[[], None]) -> None:
        def runner() -> None:
            try:
                target()
            except BaseException as exc:  # never let a worker die silently
                LOGGER.exception("background operation failed: %s", operation.label)
                self._finish(
                    operation,
                    ok=False,
                    message=str(exc) or exc.__class__.__name__,
                    result={"traceback": traceback.format_exc(limit=6)},
                )

        thread = threading.Thread(target=runner, name=f"webui-{operation.kind}", daemon=True)
        self._thread = thread
        thread.start()

    def wait(self, timeout: float = 120.0) -> None:
        """Block until the current operation finishes. Used by the tests."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # ------------------------------------------------------------- operations

    def start_harvest(
        self,
        query: OpenAlexQuery,
        *,
        limit: int | None,
        dry_run: bool,
        label: str,
        search_provenance: dict[str, Any] | None = None,
    ) -> ActiveOperation:
        """Run a harvest (or a dry-run preview) through the existing orchestrator."""
        operation = ActiveOperation(
            kind="dry_run" if dry_run else "harvest",
            label=label,
            started_at=utc_now_iso(),
            detail={
                "limit": limit,
                "query": query.to_dict(),
                "search_mode": (search_provenance or {}).get("search_mode", "conventional"),
            },
        )
        self._begin(operation)
        config = self._config_provider()

        def work() -> None:
            operation.phase = "discovering"
            with StateStore(config.state_db) as store, ClientPool(config) as clients:
                harvester = Harvester(config, store, clients)
                operation.phase = "running"
                result = harvester.harvest(
                    query,
                    limit=limit,
                    dry_run=dry_run,
                    search_provenance=search_provenance,
                )
                operation.run_id = result.run_id
                self._finish(
                    operation,
                    ok=result.status.value in ("COMPLETED", "SUSPENDED"),
                    message=_harvest_message(result),
                    result={
                        "run_id": result.run_id,
                        "status": result.status.value,
                        "stats": result.stats.to_dict(),
                        "report_path": str(result.report_path) if result.report_path else None,
                    },
                )

        self._spawn(operation, work)
        return operation

    def resume_run(self, run_id: str) -> ActiveOperation:
        operation = ActiveOperation(
            kind="resume",
            label=f"Resuming run {run_id}",
            started_at=utc_now_iso(),
            run_id=run_id,
        )
        self._begin(operation)
        config = self._config_provider()

        def work() -> None:
            operation.phase = "running"
            with StateStore(config.state_db) as store, ClientPool(config) as clients:
                result = Harvester(config, store, clients).resume(run_id)
                self._finish(
                    operation,
                    ok=result.status.value in ("COMPLETED", "SUSPENDED"),
                    message=_harvest_message(result),
                    result={
                        "run_id": result.run_id,
                        "status": result.status.value,
                        "stats": result.stats.to_dict(),
                    },
                )

        self._spawn(operation, work)
        return operation

    def start_verify(self, *, deep: bool) -> ActiveOperation:
        operation = ActiveOperation(
            kind="verify",
            label="Deep verification" if deep else "Verification",
            started_at=utc_now_iso(),
            detail={"deep": deep},
        )
        self._begin(operation)
        config = self._config_provider()

        def work() -> None:
            operation.phase = "checking"
            with StateStore(config.state_db) as store:
                report = verify_corpus(config, store, deep=deep)
                payload = report.to_dict()
                store.record_verification(payload, deep=deep)
            self._finish(
                operation,
                ok=report.ok,
                message=(
                    "Corpus verified: no problems found."
                    if report.ok
                    else f"{len(report.problems)} problem(s) and "
                    f"{len(report.orphans)} orphan file(s) found."
                ),
                result=payload,
            )

        self._spawn(operation, work)
        return operation


def _harvest_message(result: Any) -> str:
    """A plain-language outcome for the operator, not an exit code."""
    stats = result.stats
    status = result.status.value
    if status == "SUSPENDED":
        return (
            f"Paused: {stats.suspend_reason or 'the provider daily budget is used up'}. "
            f"{stats.completed} document(s) completed. Resume when the budget resets."
        )
    if status == "INTERRUPTED":
        return f"Interrupted. {stats.completed} document(s) completed. The run can be resumed."
    if status == "FAILED":
        return "The run failed before finishing. See the failures below."
    parts = [f"{stats.completed} document(s) completed"]
    if stats.already_complete:
        parts.append(f"{stats.already_complete} already had artifacts")
    if stats.failed_permanent or stats.failed_retryable:
        parts.append(f"{stats.failed_permanent + stats.failed_retryable} could not be acquired")
    return ", ".join(parts) + "."


__all__ = ["ActiveOperation", "BusyError", "RunManager"]
