"""Harvest orchestration: discovery, deduplication, acquisition, ingestion, reporting.

Implements the pipeline of MASTER_SPEC section 0 with the resumability, idempotency,
reconciliation and budget-suspension guarantees of sections 24, 25, 47, 48 and
SPEC_PATCH section 2.

Concurrency model: discovery is sequential (the cursor is inherently serial); acquisition
runs on a bounded thread pool where every document is claimed transactionally before any
work starts, so two workers can never acquire the same logical document.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .acquisition import AcquisitionOutcome, Acquirer, sweep_stale_parts
from .config import Config
from .errors import (
    BudgetExhaustedError,
    ConfigurationError,
    ErrorCategory,
    Failure,
    HarvesterError,
    NotFoundError,
    StorageError,
)
from .http import ClientPool, redact_url
from .identity import artifact_path, normalize_doi, pmcid_from_urls, sha256_file
from .models import (
    ArtifactKind,
    ArtifactRecord,
    CrossCheck,
    DocumentStatus,
    FulltextCandidate,
    LogicalDocument,
    RunStatus,
    Source,
    SourceRecord,
)
from .providers import EuropePmcAdapter, OpenAlexAdapter, OpenAlexQuery, UnpaywallAdapter
from .reporting import RunStats, StatsCollector, write_report
from .state import StateStore
from .storage import SidecarInputs, build_sidecar, write_sidecar
from .util import to_json, utc_now_iso
from .validation import validate_pdf, validate_xml

LOGGER = logging.getLogger("harvester.orchestrator")

#: Upper bound on failures embedded in a run report. All failures remain in state.
MAX_REPORTED_FAILURES = 200

#: Acquisition order by discovery source (MASTER_SPEC section 13).
_SOURCE_PRIORITY = {
    Source.OPENALEX.value: 0,
    Source.EUROPE_PMC.value: 1,
    Source.UNPAYWALL.value: 2,
}

#: Locations known to answer automated clients with an access challenge rather than
#: the file. They are sorted last, never dropped: the guard is a live observation
#: (NCBI PMC began serving a proof-of-work page for ``/articles/PMC…/pdf/…`` and may
#: stop), so the location stays in the list and is still tried when nothing else
#: works. Ordering is the whole remedy — a legitimate mirror simply goes first.
_CHALLENGED_HOSTS = frozenset({"pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"})


def _is_challenged_location(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (parsed.hostname or "").lower() in _CHALLENGED_HOSTS and "/pdf" in (
        parsed.path or ""
    )


class SuspendedRun(Exception):
    """Raised internally to unwind cleanly when a provider budget is exhausted."""

    def __init__(self, error: BudgetExhaustedError) -> None:
        super().__init__(str(error))
        self.error = error


class _ProviderOperation:
    """One logical provider operation in the Evidence Ledger.

    A logical operation is one thing asked of one provider — a cross-check lookup, a
    DOI resolution, the retrieval of one candidate location — not one HTTP message.
    The transport may retry inside it; that is recorded in ``attempts`` and summarised
    in the outcome as ``http_attempts``, never as a second request (MASTER_SPEC section
    26). Exactly one terminal outcome is written and the first one written wins, so no
    path can report the same operation twice.
    """

    __slots__ = ("_store", "request_id", "_notes", "_finished")

    def __init__(self, store: StateStore, request_id: str) -> None:
        self._store = store
        self.request_id = request_id
        self._notes: dict[str, Any] = {}
        self._finished = False

    @property
    def finished(self) -> bool:
        return self._finished

    def note(self, **fields: Any) -> None:
        """Attach details that must survive whichever terminal outcome is recorded."""
        self._notes.update(fields)

    def hit(
        self,
        *,
        http_status: int | None = None,
        result_count: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._finish(
            "HIT", http_status=http_status, result_count=result_count, details=details
        )

    def no_hit(
        self,
        *,
        http_status: int | None = None,
        result_count: int | None = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        """The provider answered, and the answer was "nothing here"."""
        self._finish(
            "NO_HIT", http_status=http_status, result_count=result_count, details=details
        )

    def failed(self, error: BaseException) -> None:
        """Terminate on an error, preserving status, category and retryability."""
        if isinstance(error, BudgetExhaustedError):
            self._finish(
                "ERROR",
                http_status=error.http_status,
                error_category=error.category.value,
                details=error.suspension_details(),
            )
            return
        if isinstance(error, HarvesterError):
            self._finish(
                "TIMEOUT" if error.category is ErrorCategory.TIMEOUT else "ERROR",
                http_status=error.http_status,
                error_category=error.category.value,
                details={"message": error.message, "retryable": error.retryable},
            )
            return
        # An unexpected defect is still terminal evidence. Only the exception type is
        # recorded: a third-party exception message is not a vetted secret-safe string,
        # and the full traceback is already in the log.
        self._finish(
            "ERROR",
            error_category=ErrorCategory.UNKNOWN_ERROR.value,
            details={
                "message": f"unexpected {type(error).__name__}",
                "retryable": False,
            },
        )

    def _finish(
        self,
        status: str,
        *,
        http_status: int | None = None,
        result_count: int | None = None,
        error_category: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        merged = dict(self._notes)
        merged.update(details or {})
        self._store.finish_provider_request(
            self.request_id,
            status=status,
            http_status=http_status,
            result_count=result_count,
            error_category=error_category,
            details=merged,
        )


@dataclass(slots=True)
class HarvestResult:
    run_id: str
    status: RunStatus
    stats: RunStats
    report_path: Path | None


class Harvester:
    """Owns one harvest execution (a fresh run or the continuation of an existing one)."""

    def __init__(
        self,
        config: Config,
        store: StateStore,
        clients: ClientPool,
        *,
        worker_prefix: str | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.clients = clients
        self.openalex = OpenAlexAdapter(clients.openalex, config.openalex)
        self.europe_pmc = EuropePmcAdapter(clients.europe_pmc, config.europe_pmc)
        self.unpaywall = UnpaywallAdapter(
            clients.unpaywall, config.unpaywall, contact_email=config.contact_email
        )
        self.acquirer = Acquirer(config, clients.downloads)
        self._worker_prefix = worker_prefix or f"worker-{uuid.uuid4().hex[:8]}"
        self._suspension: BudgetExhaustedError | None = None
        self._suspension_lock = threading.Lock()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ entry points

    def harvest(
        self,
        query: OpenAlexQuery,
        *,
        limit: int | None = None,
        dry_run: bool = False,
        run_id: str | None = None,
        search_provenance: dict[str, Any] | None = None,
    ) -> HarvestResult:
        """Start a new harvest run.

        *search_provenance* records how the query was constructed (Assisted Search V1
        section 31). It is descriptive metadata only: the pipeline below consumes the
        query, never the provenance, so a run started without it behaves identically.
        """
        run_id = run_id or _new_run_id()
        self.store.create_run(
            run_id,
            query=query.to_dict(),
            config=self.config.redacted_dict(),
            dry_run=dry_run,
            record_limit=limit,
            search_provenance=search_provenance,
        )
        LOGGER.info("run %s started (dry_run=%s, limit=%s)", run_id, dry_run, limit)
        return self._execute(run_id, query, limit=limit, dry_run=dry_run, resuming=False)

    def resume(self, run_id: str) -> HarvestResult:
        """Continue an interrupted or suspended run (MASTER_SPEC section 24)."""
        run = self.store.get_run(run_id)
        if run is None:
            raise ConfigurationError(f"unknown run: {run_id}")
        if run.status is RunStatus.COMPLETED:
            LOGGER.info("run %s is already COMPLETED; verifying instead of re-running", run_id)
        query = OpenAlexQuery.from_dict(run.query)
        LOGGER.info(
            "resuming run %s (cursor=%s, discovery_complete=%s)",
            run_id,
            "<checkpoint>" if run.discovery_cursor else "*",
            run.discovery_complete,
        )
        return self._execute(
            run_id, query, limit=run.record_limit, dry_run=run.dry_run, resuming=True
        )

    # --------------------------------------------------------------------- pipeline

    def _execute(
        self,
        run_id: str,
        query: OpenAlexQuery,
        *,
        limit: int | None,
        dry_run: bool,
        resuming: bool,
    ) -> HarvestResult:
        started = time.monotonic()
        run = self.store.get_run(run_id)
        assert run is not None
        collector = StatsCollector(run_id, run.started_at)
        collector.set("records_discovered", run.discovery_seen)
        collector.set("discovery_pages", run.discovery_pages)
        # Read back from state rather than from the caller, so a resumed run reports
        # the same provenance as the run that created it.
        collector.set("search_provenance", run.search_provenance)
        if dry_run:
            collector.add_note("dry run: discovery and normalization only, no artifacts written")

        status = RunStatus.RUNNING
        try:
            # Sweeping abandoned temporary files touches no state and is safe on every
            # run. Re-queueing documents another process may still hold is not, so it
            # happens only on an explicit resume.
            if not dry_run:
                self._sweep_parts(run_id, collector)
            if resuming:
                self._reconcile(run_id, collector)
            self._discover(run_id, query, collector, limit=limit)
            if not dry_run:
                self._acquire_all(run_id, collector)
            status = RunStatus.COMPLETED
        except SuspendedRun as suspended:
            status = RunStatus.SUSPENDED
            self._record_suspension(run_id, suspended.error, collector)
        except KeyboardInterrupt:
            status = RunStatus.INTERRUPTED
            collector.add_note("interrupted by operator; state checkpointed and resumable")
            LOGGER.warning("run %s interrupted; state is checkpointed and resumable", run_id)
        except HarvesterError as exc:
            status = RunStatus.FAILED
            failure = exc.as_failure("harvest", source=None)
            self.store.record_failure(run_id, failure)
            collector.stats.failures.append(failure.to_dict())
            LOGGER.error("run %s failed: %s", run_id, exc)

        return self._finalise(run_id, status, collector, started)

    # -------------------------------------------------------------------- discovery

    def _discover(
        self, run_id: str, query: OpenAlexQuery, collector: StatsCollector, *, limit: int | None
    ) -> None:
        run = self.store.get_run(run_id)
        assert run is not None
        if run.discovery_complete:
            LOGGER.info("run %s: discovery already complete", run_id)
            return
        if not self.config.openalex.enabled:
            raise ConfigurationError("OpenAlex is the primary discovery source and is disabled")

        cursor = run.discovery_cursor or "*"
        pages = run.discovery_pages
        seen = run.discovery_seen
        # A provider that cycles its cursor would otherwise loop forever. Cursors are
        # short strings, so remembering the ones already visited in this process is
        # cheap insurance against an unbounded run.
        visited: set[str] = {cursor}

        while True:
            self._raise_if_suspended()
            request_id = self.store.begin_provider_request(
                run_id=run_id,
                provider=Source.OPENALEX.value,
                operation="discover",
                request={"query": query.to_dict(), "cursor": cursor},
            )
            try:
                page = self.openalex.discover_page(query, cursor=cursor)
            except BudgetExhaustedError as exc:
                self.store.finish_provider_request(
                    request_id,
                    status="ERROR",
                    http_status=exc.http_status,
                    error_category=exc.category.value,
                    details=exc.suspension_details(),
                )
                # The cursor already on record still points at this page, so nothing is
                # lost: the resumed run re-reads it and dedup absorbs the repetition.
                raise SuspendedRun(exc) from exc
            except HarvesterError as exc:
                self.store.finish_provider_request(
                    request_id,
                    status=("TIMEOUT" if exc.category is ErrorCategory.TIMEOUT else "ERROR"),
                    http_status=exc.http_status,
                    error_category=exc.category.value,
                    details={"message": exc.message, "retryable": exc.retryable},
                )
                failure = exc.as_failure("discover", source=Source.OPENALEX.value)
                self.store.record_failure(run_id, failure)
                collector.stats.failures.append(failure.to_dict())
                raise

            pages += 1
            persisted_count = 0
            for document, source_record in page.records:
                if limit is not None and seen >= limit:
                    break
                seen += 1
                created, newly_linked = self.store.upsert_discovered_document(
                    run_id,
                    document,
                    source_record,
                    provider_request_id=request_id,
                )
                persisted_count += 1
                collector.increment("records_discovered")
                collector.increment("records_normalized")
                if created:
                    collector.increment("queued")
                else:
                    # Same logical document from another record/run: one document, more
                    # provenance, no second download (MASTER_SPEC section 12).
                    collector.increment("duplicates")
                    if newly_linked:
                        collector.increment("queued")

            self.store.finish_provider_request(
                request_id,
                status="HIT" if page.raw_count else "NO_HIT",
                http_status=page.http_status,
                result_count=page.raw_count,
                details={
                    "normalized_count": len(page.records),
                    "persisted_count": persisted_count,
                    "provider_total_count": page.total_count,
                    "next_cursor_present": bool(page.next_cursor),
                },
            )

            collector.set("discovery_pages", pages)
            reached_limit = limit is not None and seen >= limit
            complete = reached_limit or not page.next_cursor or page.raw_count == 0
            next_cursor = None if complete else page.next_cursor

            # Checkpoint only after the page's records are committed. An interruption
            # therefore re-reads at most one page and never skips one.
            self.store.update_run_cursor(
                run_id, next_cursor, complete=complete, pages=pages, seen=seen
            )
            LOGGER.info(
                "run %s: discovery page %d, %d records (%d total seen)",
                run_id,
                pages,
                page.raw_count,
                seen,
            )
            if complete:
                return
            if next_cursor in visited:
                LOGGER.warning(
                    "run %s: provider cursor repeated (%s); ending discovery to avoid a loop",
                    run_id,
                    "same as current" if next_cursor == cursor else "seen earlier",
                )
                collector.add_note(
                    "discovery stopped early: the provider's pagination cursor repeated"
                )
                self.store.update_run_cursor(run_id, None, complete=True, pages=pages, seen=seen)
                return
            cursor = next_cursor  # type: ignore[assignment]
            visited.add(cursor)

    # ------------------------------------------------------------------ acquisition

    def _acquire_all(self, run_id: str, collector: StatsCollector) -> None:
        document_ids = self.store.pending_documents_for_run(run_id)
        if not document_ids:
            LOGGER.info("run %s: nothing pending for acquisition", run_id)
            return
        LOGGER.info("run %s: acquiring %d documents", run_id, len(document_ids))

        workers = max(1, self.config.downloads.concurrency)
        if workers == 1:
            for document_id in document_ids:
                if self._stop.is_set():
                    break
                self._acquire_one(run_id, document_id, collector)
        else:
            executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="harvest")
            futures = [
                executor.submit(self._acquire_one, run_id, document_id, collector)
                for document_id in document_ids
            ]
            try:
                for future in futures:
                    future.result()
            except BaseException:
                # Suspension or interruption: stop the workers promptly instead of
                # waiting for every queued document. Each already-running worker
                # checks ``_stop`` and finishes its current document cleanly.
                self._stop.set()
                executor.shutdown(wait=True, cancel_futures=True)
                raise
            finally:
                executor.shutdown(wait=True)

        suspension = self._suspension or self.clients.suspended_error()
        if suspension is not None:
            raise SuspendedRun(suspension)

    def _acquire_one(self, run_id: str, document_id: str, collector: StatsCollector) -> None:
        if self._stop.is_set():
            return
        worker_id = f"{self._worker_prefix}:{threading.get_ident()}"
        try:
            if self._is_already_complete(document_id):
                collector.increment("already_complete")
                return
            if not self.store.claim_document(document_id, worker_id):
                LOGGER.debug("document %s is claimed by another worker; skipping", document_id)
                return
        except HarvesterError as exc:
            self._record_document_failure(run_id, document_id, exc, "claim", collector)
            return

        collector.increment("attempted")
        try:
            self._process_document(run_id, document_id, collector)
        except SuspendedRun:
            raise
        except BudgetExhaustedError as exc:
            self._begin_suspension(exc)
            self.store.set_status(document_id, DocumentStatus.QUEUED, release_claim=True)
        except HarvesterError as exc:
            self._fail_document(run_id, document_id, exc, collector)
        except Exception as exc:  # unexpected defect: still recorded, never swallowed
            LOGGER.exception("unexpected error while acquiring %s", document_id)
            wrapped = HarvesterError(
                f"unexpected error: {type(exc).__name__}: {exc}",
                category=ErrorCategory.UNKNOWN_ERROR,
            )
            self._fail_document(run_id, document_id, wrapped, collector)
        finally:
            self.store.release_claim(document_id)

    def _process_document(
        self, run_id: str, document_id: str, collector: StatsCollector
    ) -> None:
        metadata = self.store.get_metadata(document_id)
        row = self.store.get_document_row(document_id)
        attempts = int(row["attempts"]) if row is not None else 0
        canonical_doi = normalize_doi(metadata.get("doi"))

        artifacts = self.store.get_artifacts(document_id)
        # Reconcile a file that was published just before the process died: adopt it
        # rather than downloading it again (MASTER_SPEC section 48).
        adopted = self._adopt_orphan_files(
            run_id, document_id, artifacts, metadata, collector
        )
        artifacts.update(adopted)

        candidates = _candidates_from_metadata(metadata)

        if canonical_doi and self.config.europe_pmc.enabled:
            cross_candidates = self._cross_check_europe_pmc(
                run_id, document_id, canonical_doi, metadata, collector
            )
            candidates.extend(cross_candidates)
            metadata = self.store.get_metadata(document_id)

        pdf_error: HarvesterError | None = None
        tried_urls: set[str] = set()
        if ArtifactKind.PDF.value not in artifacts:
            tried_urls = {c.url for c in _order_candidates(candidates, ArtifactKind.PDF)}
            record, pdf_error = self._acquire_pdf(run_id, document_id, candidates, collector)
            if record is not None:
                artifacts[ArtifactKind.PDF.value] = record

        # Unpaywall is a fallback: consulted only when the providers already consulted
        # produced no usable PDF (MASTER_SPEC sections 13 and 43).
        if (
            ArtifactKind.PDF.value not in artifacts
            and canonical_doi
            and self.config.unpaywall.enabled
        ):
            fallback = self._unpaywall_candidates(run_id, document_id, canonical_doi, collector)
            if fallback:
                metadata = self.store.get_metadata(document_id)
                record, fallback_error = self._acquire_pdf(
                    run_id, document_id, fallback, collector, already_tried=tried_urls
                )
                if record is not None:
                    artifacts[ArtifactKind.PDF.value] = record
                    pdf_error = None
                elif fallback_error is not None:
                    pdf_error = fallback_error

        if self.config.xml_policy != "disabled" and ArtifactKind.XML.value not in artifacts:
            record = self._acquire_xml(run_id, document_id, candidates, collector)
            if record is not None:
                artifacts[ArtifactKind.XML.value] = record

        self._finish_document(
            run_id, document_id, artifacts, pdf_error, collector, attempts=attempts
        )

    # ------------------------------------------------------------- acquisition steps

    def _acquire_pdf(
        self,
        run_id: str,
        document_id: str,
        candidates: list[FulltextCandidate],
        collector: StatsCollector,
        *,
        already_tried: set[str] | None = None,
    ) -> tuple[ArtifactRecord | None, HarvesterError | None]:
        ordered = _order_candidates(candidates, ArtifactKind.PDF)
        offered = len(ordered)
        if already_tried:
            # The fallback pass usually re-offers locations the first pass already
            # rejected — providers agree about where a PDF lives far more often than
            # they disagree. Downloading them a second time cannot succeed and spends
            # requests on hosts that were plausibly throttling us to begin with.
            ordered = [c for c in ordered if c.url not in already_tried]
        if not ordered:
            if offered:
                # Locations were offered, just no new ones. The caller keeps the
                # failure the first pass produced rather than being told, wrongly,
                # that nobody offered anything.
                return None, None
            return None, HarvesterError(
                "no Open-Access PDF location was offered by any configured provider",
                category=ErrorCategory.NO_OA_LOCATION,
            )
        last_error: HarvesterError | None = None
        for index, candidate in enumerate(ordered, start=1):
            self._raise_if_suspended()
            try:
                # One candidate location is one logical provider operation, whether it
                # ends in validated bytes on disk or in a rejection.
                with self._provider_operation(
                    run_id=run_id,
                    provider=candidate.source.value,
                    operation="acquire.pdf",
                    request=_candidate_request(candidate, document_id, index, len(ordered)),
                ) as ledger:
                    outcome = self.acquirer.acquire(
                        document_id=document_id,
                        candidate=candidate,
                        on_attempt=self._attempt_recorder(
                            run_id, document_id, "acquire.pdf", candidate, ledger
                        ),
                    )
                    ledger.hit(**_acquisition_evidence(outcome))
            except BudgetExhaustedError:
                raise
            except HarvesterError as exc:
                last_error = exc
                # Candidate-level failures are recorded, then the next fallback
                # location is tried (AC-010). Nothing is silently discarded.
                self.store.record_failure(
                    run_id,
                    exc.as_failure(
                        "acquire.pdf",
                        source=candidate.source.value,
                        document_id=document_id,
                        attempt=index,
                    ),
                )
                LOGGER.info(
                    "document %s: PDF candidate %d/%d failed (%s): %s",
                    document_id,
                    index,
                    len(ordered),
                    exc.category.value,
                    exc.message,
                )
                continue

            self.store.record_artifact(document_id, outcome.artifact, run_id=run_id)
            collector.increment("downloaded")
            collector.increment("validated")
            collector.increment("pdf_count")
            collector.increment("bytes_downloaded", outcome.artifact.size_bytes)
            LOGGER.info(
                "document %s: PDF acquired from %s (%d bytes)",
                document_id,
                candidate.source.value,
                outcome.artifact.size_bytes,
            )
            return outcome.artifact, None
        if last_error is not None:
            last_error.details["candidates_tried"] = len(ordered)
        return None, last_error

    def _acquire_xml(
        self,
        run_id: str,
        document_id: str,
        candidates: list[FulltextCandidate],
        collector: StatsCollector,
    ) -> ArtifactRecord | None:
        ordered = _order_candidates(candidates, ArtifactKind.XML)
        for index, candidate in enumerate(ordered, start=1):
            self._raise_if_suspended()
            try:
                with self._provider_operation(
                    run_id=run_id,
                    provider=candidate.source.value,
                    operation="acquire.xml",
                    request=_candidate_request(candidate, document_id, index, len(ordered)),
                ) as ledger:
                    outcome = self.acquirer.acquire(
                        document_id=document_id,
                        candidate=candidate,
                        on_attempt=self._attempt_recorder(
                            run_id, document_id, "acquire.xml", candidate, ledger
                        ),
                    )
                    ledger.hit(**_acquisition_evidence(outcome))
            except BudgetExhaustedError:
                raise
            except HarvesterError as exc:
                self.store.record_failure(
                    run_id,
                    exc.as_failure(
                        "acquire.xml",
                        source=candidate.source.value,
                        document_id=document_id,
                        attempt=index,
                    ),
                )
                LOGGER.info(
                    "document %s: XML candidate %d failed (%s): %s",
                    document_id,
                    index,
                    exc.category.value,
                    exc.message,
                )
                continue

            self.store.record_artifact(document_id, outcome.artifact, run_id=run_id)
            collector.increment("downloaded")
            collector.increment("validated")
            collector.increment("xml_count")
            collector.increment("bytes_downloaded", outcome.artifact.size_bytes)
            return outcome.artifact
        return None

    @contextmanager
    def _provider_operation(
        self,
        *,
        run_id: str,
        provider: str,
        operation: str,
        request: dict[str, Any],
    ):
        """Open one logical provider request and guarantee its terminal outcome.

        *request* is the caller's deliberately bounded, secret-safe description of what
        was asked. Nothing derived from transport credentials, headers or configuration
        reaches it: URLs go through :func:`redact_url` first, and identifying values are
        limited to what an auditor needs in order to re-trace the operation.
        """
        ledger = _ProviderOperation(
            self.store,
            self.store.begin_provider_request(
                run_id=run_id, provider=provider, operation=operation, request=request
            ),
        )
        try:
            yield ledger
        except BaseException as exc:
            _record_terminal_outcome(ledger, exc)
            raise
        finally:
            # A request without an outcome means "the process died mid-operation". No
            # ordinary path may leave that trace behind by merely forgetting to report.
            if not ledger.finished:
                _record_terminal_outcome(
                    ledger,
                    HarvesterError(
                        "provider operation ended without a recorded outcome",
                        category=ErrorCategory.UNKNOWN_ERROR,
                    ),
                )

    def _attempt_recorder(
        self,
        run_id: str,
        document_id: str,
        operation: str,
        candidate: FulltextCandidate,
        ledger: _ProviderOperation | None = None,
    ):
        """Record every individual HTTP attempt an acquisition makes.

        This is what makes the run report's ``retry_count`` mean "requests that were
        retries" rather than "candidates tried".
        """

        def record(attempt: int, error: HarvesterError | None, http_status: int | None) -> None:
            if ledger is not None:
                # Internal retries stay inside the one logical provider request; how
                # many it took is outcome detail, never a second request.
                ledger.note(http_attempts=attempt)
            self.store.record_attempt(
                run_id=run_id,
                document_id=document_id,
                operation=operation,
                source=candidate.source.value,
                attempt_no=attempt,
                ok=error is None,
                error_category=error.category.value if error else None,
                http_status=http_status,
                message=error.message if error else None,
                url=redact_url(candidate.url),
                retryable=error.retryable if error else None,
            )

        return record

    def _cross_check_europe_pmc(
        self,
        run_id: str,
        document_id: str,
        canonical_doi: str,
        metadata: dict[str, Any],
        collector: StatsCollector,
    ) -> list[FulltextCandidate]:
        """Cross-check identity/metadata and collect Europe PMC full-text candidates."""
        existing = {check.get("source") for check in metadata.get("cross_checks") or []}
        if Source.EUROPE_PMC.value in existing:
            return _candidates_for_source(metadata, Source.EUROPE_PMC)

        record, lookup_error, resolved_by, request_id = self._resolve_europe_pmc_record(
            run_id, document_id, canonical_doi, metadata
        )
        if lookup_error is not None:
            self.store.record_failure(
                run_id,
                lookup_error.as_failure(
                    "cross_check", source=Source.EUROPE_PMC.value, document_id=document_id
                ),
            )
            LOGGER.info(
                "document %s: Europe PMC cross-check failed: %s",
                document_id,
                lookup_error.message,
            )
            return []

        # The cross-check is recorded whatever it found. "Europe PMC was consulted and
        # knew nothing" is a fact about the run, and leaving it out of the ledger is
        # what let a working provider look like one that was never configured.
        self.store.record_attempt(
            run_id=run_id,
            document_id=document_id,
            operation="cross_check",
            source=Source.EUROPE_PMC.value,
            attempt_no=1,
            ok=record is not None,
            message=None if record is not None else "no Europe PMC record for this work",
        )

        if record is None:
            check = CrossCheck(
                source=Source.EUROPE_PMC,
                matched_on=[],
                identifiers={},
                metadata_differences={},
                fulltext_availability={"known_to_provider": False},
                checked_at=utc_now_iso(),
            )
            self._merge_cross_check(
                document_id,
                check,
                [],
                None,
                run_id=run_id,
                candidate_observation_id=None,
            )
            return []

        check = self.europe_pmc.cross_check(canonical_doi, record, metadata)
        if resolved_by != "doi":
            # How the record was found is part of its provenance: a record reached
            # through a derived PMCID rests on a weaker claim than one the provider
            # returned for the DOI itself, and the difference must stay visible.
            check.fulltext_availability["resolved_by"] = resolved_by
        # Observation, field decisions and compatibility projection share one SQLite
        # transaction. A crash therefore cannot expose only one side of the dual write.
        with self.store.transaction():
            observation_id = self.store.add_source_record(
                document_id, record, run_id=run_id, provider_request_id=request_id
            )
            self._merge_cross_check(
                document_id,
                check,
                record.candidates,
                record,
                run_id=run_id,
                candidate_observation_id=observation_id,
            )
        if check.metadata_differences:
            LOGGER.info(
                "document %s: provider metadata disagreement preserved for %s",
                document_id,
                ", ".join(sorted(check.metadata_differences)),
            )
        return list(record.candidates)

    def _resolve_europe_pmc_record(
        self, run_id: str, document_id: str, canonical_doi: str, metadata: dict[str, Any]
    ) -> tuple[SourceRecord | None, HarvesterError | None, str, str | None]:
        """Find the Europe PMC record by DOI, then by identifier.

        The DOI query is primary but not dependable — it has been observed answering
        "nothing here" for articles Europe PMC demonstrably holds — and treating that
        answer as final costs the document its only remaining full-text route. So when
        the DOI yields nothing, the identifiers already in hand are tried before giving
        up: a provider-supplied PMCID first, then a PMCID derived from the PMC URLs the
        other providers gave us, then the PMID.

        Every lookup that is actually executed is one logical provider operation and
        appears in the Evidence Ledger as exactly one request with exactly one outcome.

        Returns the record (or ``None``), the error that ended the search (or ``None``),
        which identifier produced the answer, and the ledger request that produced it.
        """
        attempts: list[
            tuple[str, dict[str, Any], Callable[[], SourceRecord | None]]
        ] = [
            (
                "doi",
                {"document_id": document_id, "lookup": "doi", "doi": canonical_doi},
                lambda: self.europe_pmc.lookup_by_doi(canonical_doi),
            )
        ]

        identifiers = metadata.get("identifiers") or {}
        stated_pmcid = str(identifiers.get("pmcid") or "").strip()
        if stated_pmcid:
            attempts.append(
                (
                    "pmcid",
                    {"document_id": document_id, "lookup": "pmcid", "pmcid": stated_pmcid},
                    lambda: self.europe_pmc.lookup_by_pmcid(stated_pmcid),
                )
            )
        else:
            # Not stated by any provider, but present inside the full-text URLs they
            # supplied. Derived, therefore never written into ``identifiers``: this
            # resolves a lookup, it does not become metadata the harvester asserts.
            derived_pmcid = pmcid_from_urls(
                c.url for c in _candidates_from_metadata(metadata)
            )
            if derived_pmcid:
                attempts.append(
                    (
                        "derived_pmcid",
                        {
                            "document_id": document_id,
                            "lookup": "derived_pmcid",
                            "pmcid": derived_pmcid,
                        },
                        lambda: self.europe_pmc.lookup_by_pmcid(derived_pmcid),
                    )
                )

        pmid = str(identifiers.get("pmid") or "").strip()
        if pmid:
            attempts.append(
                (
                    "pmid",
                    {"document_id": document_id, "lookup": "pmid", "pmid": pmid},
                    lambda: self.europe_pmc.lookup_by_pmid(pmid),
                )
            )

        first_error: HarvesterError | None = None
        for label, request, lookup in attempts:
            with self._provider_operation(
                run_id=run_id,
                provider=Source.EUROPE_PMC.value,
                # The DOI query *is* the cross-check; the identifier queries that only
                # run when it comes back empty are separate resolve operations. Each is
                # one external call, so each is one logical provider request — and a
                # cross-check that never had to fall back leaves exactly one.
                operation="cross_check" if label == "doi" else "resolve",
                request=request,
            ) as ledger:
                try:
                    record = lookup()
                except BudgetExhaustedError:
                    raise
                except HarvesterError as exc:
                    ledger.failed(exc)
                    # One lookup failing is not the provider's verdict on the article;
                    # the remaining identifiers are still worth asking about. The first
                    # error is kept so a total failure is still reported truthfully.
                    if first_error is None:
                        first_error = exc
                    LOGGER.debug(
                        "document %s: Europe PMC lookup by %s failed: %s",
                        document_id,
                        label,
                        exc.message,
                    )
                    continue
                if record is not None:
                    ledger.hit(
                        result_count=1,
                        details={"lookup": label, "source_id": record.source_id},
                    )
                    if label != "doi":
                        LOGGER.info(
                            "document %s: Europe PMC resolved by %s after the DOI lookup "
                            "returned nothing",
                            document_id,
                            label,
                        )
                    return record, None, label, ledger.request_id
                # Every lookup answering "nothing here" is an answer, not a failure.
                ledger.no_hit(details={"lookup": label})
        # A total absence is recorded as known_to_provider=false by the caller. An
        # error is reported only when one actually occurred and nothing else resolved.
        return None, first_error, "none", None

    def _unpaywall_candidates(
        self, run_id: str, document_id: str, canonical_doi: str, collector: StatsCollector
    ) -> list[FulltextCandidate]:
        with self._provider_operation(
            run_id=run_id,
            provider=Source.UNPAYWALL.value,
            operation="resolve",
            # The contact address the request carries is credential-like and stays out
            # of the ledger; the DOI is the whole auditable input to this operation.
            request={"document_id": document_id, "doi": canonical_doi},
        ) as ledger:
            try:
                record = self.unpaywall.resolve(canonical_doi)
            except BudgetExhaustedError:
                raise
            except HarvesterError as exc:
                ledger.failed(exc)
                self.store.record_failure(
                    run_id,
                    exc.as_failure(
                        "resolve", source=Source.UNPAYWALL.value, document_id=document_id
                    ),
                )
                LOGGER.info(
                    "document %s: Unpaywall fallback failed: %s", document_id, exc.message
                )
                return []
            if record is None:
                # Unpaywall knows the service, just not this DOI. That is an answer.
                ledger.no_hit()
                return []
            ledger.hit(
                result_count=1,
                details={
                    "pdf_candidate_count": len(record.candidates),
                    "oa_status": record.oa_status,
                    "is_oa": record.is_oa,
                },
            )

        with self.store.transaction():
            observation_id = self.store.add_source_record(
                document_id, record, run_id=run_id, provider_request_id=ledger.request_id
            )
            metadata = self.store.get_metadata(document_id)
            metadata = _merge_candidates_into_metadata(metadata, record.candidates)
            if record.oa_status and not metadata.get("oa_status"):
                # SPEC_PATCH section 6: preserve a provider-supplied oa_status and its source.
                metadata["oa_status"] = record.oa_status
                metadata["oa_status_source"] = Source.UNPAYWALL.value
            discovered = list(metadata.get("discovered_via") or [])
            if Source.UNPAYWALL.value not in discovered:
                discovered.append(Source.UNPAYWALL.value)
            metadata["discovered_via"] = discovered

            candidate_metadata = _source_record_metadata(record)
            candidate_metadata["discovered_via"] = [Source.UNPAYWALL.value]
            self.store.update_metadata(
                document_id,
                metadata,
                run_id=run_id,
                candidate_observation_id=observation_id,
                candidate_metadata=candidate_metadata,
            )
        return list(record.candidates)

    def _merge_cross_check(
        self,
        document_id: str,
        check: CrossCheck,
        candidates: list[FulltextCandidate],
        record: SourceRecord | None,
        *,
        run_id: str,
        candidate_observation_id: str | None,
    ) -> None:
        metadata = self.store.get_metadata(document_id)
        checks = list(metadata.get("cross_checks") or [])
        checks = [c for c in checks if c.get("source") != check.source.value]
        checks.append(check.to_dict())
        metadata["cross_checks"] = checks

        identifiers = dict(metadata.get("identifiers") or {})
        for key, value in check.identifiers.items():
            identifiers.setdefault(key, value)
        metadata["identifiers"] = identifiers

        if record is not None:
            # Fill only genuinely missing values; disagreements stay recorded, never
            # overwritten (MASTER_SPEC section 44).
            for field_name in ("title", "publication_year", "journal", "abstract"):
                if metadata.get(field_name) in (None, "", []):
                    value = getattr(record, field_name)
                    if value not in (None, "", []):
                        metadata[field_name] = value
            if metadata.get("is_oa") is None and record.is_oa is not None:
                metadata["is_oa"] = record.is_oa

        metadata = _merge_candidates_into_metadata(metadata, candidates)

        candidate_metadata = _source_record_metadata(record) if record is not None else {}
        candidate_identifiers = dict(candidate_metadata.get("identifiers") or {})
        for key, value in check.identifiers.items():
            candidate_identifiers.setdefault(key, value)
        candidate_metadata["identifiers"] = candidate_identifiers
        candidate_metadata["cross_checks"] = [check.to_dict()]
        self.store.update_metadata(
            document_id,
            metadata,
            run_id=run_id,
            candidate_observation_id=candidate_observation_id,
            candidate_metadata=candidate_metadata,
            policy_overrides={"cross_checks": "replace_latest_cross_check_per_source"},
            origin="native" if candidate_observation_id is not None else "constructed",
        )

    # ------------------------------------------------------------------- completion

    def _finish_document(
        self,
        run_id: str,
        document_id: str,
        artifacts: dict[str, ArtifactRecord],
        pdf_error: HarvesterError | None,
        collector: StatsCollector,
        *,
        attempts: int,
    ) -> None:
        has_pdf = ArtifactKind.PDF.value in artifacts
        has_xml = ArtifactKind.XML.value in artifacts

        if not has_pdf:
            # Each rejected candidate already produced its own failure record. The
            # document-level record summarises the outcome rather than repeating the
            # last candidate's message verbatim, so the two are not confusable
            # (MASTER_SPEC section 30).
            if pdf_error is None:
                error = HarvesterError(
                    "no usable Open-Access PDF could be acquired",
                    category=ErrorCategory.NO_OA_LOCATION,
                )
            elif pdf_error.category is ErrorCategory.NO_OA_LOCATION:
                error = pdf_error
            else:
                tried = int(pdf_error.details.get("candidates_tried", 1))
                error = HarvesterError(
                    f"no usable Open-Access PDF after {tried} candidate location(s); "
                    f"last failure was {pdf_error.category.value}: {pdf_error.message}",
                    category=pdf_error.category,
                    retryable=pdf_error.retryable,
                    http_status=pdf_error.http_status,
                    details={"candidates_tried": tried, "summary_of_candidate_failures": True},
                )
            self._fail_document(run_id, document_id, error, collector)
            return
        if self.config.xml_policy == "required" and not has_xml:
            self._fail_document(
                run_id,
                document_id,
                HarvesterError(
                    "xml_policy=required but no XML full text was available",
                    category=ErrorCategory.NOT_FOUND,
                ),
                collector,
            )
            return

        self._publish_sidecar(
            document_id,
            artifacts,
            run_id=run_id,
            status=DocumentStatus.COMPLETED.value,
            attempts=attempts + 1,
        )
        self.store.set_status(
            document_id,
            DocumentStatus.COMPLETED,
            release_claim=True,
            increment_attempts=True,
        )
        collector.increment("completed")
        LOGGER.info("document %s: COMPLETED", document_id)

    def _publish_sidecar(
        self,
        document_id: str,
        artifacts: dict[str, ArtifactRecord],
        *,
        run_id: str,
        status: str,
        attempts: int,
    ) -> None:
        """Write the mandatory JSON sidecar describing this document's artifacts.

        The sidecar is what makes a document *findable*: the flat A1 corpus is read
        through the sidecars (``list_corpus_documents``), so a validated artifact
        without one is on disk and invisible to every downstream consumer
        (SPEC_PATCH section 3). It therefore describes whatever was actually
        validated, under the document's real status — never a status the document
        did not reach.
        """
        write_sidecar(
            self.config.storage_root,
            document_id,
            build_sidecar(
                SidecarInputs(
                    document_id=document_id,
                    metadata=self.store.get_metadata(document_id),
                    artifacts=artifacts,
                    run_id=run_id,
                    status=status,
                    attempts=attempts,
                )
            ),
        )

    def _fail_document(
        self,
        run_id: str,
        document_id: str,
        error: HarvesterError,
        collector: StatsCollector,
    ) -> None:
        row = self.store.get_document_row(document_id)
        attempts = int(row["attempts"]) if row is not None else 0
        exhausted = attempts + 1 >= self.config.retry.max_attempts
        target = (
            DocumentStatus.FAILED_RETRYABLE
            if error.retryable and not exhausted
            else DocumentStatus.FAILED_PERMANENT
        )
        failure = error.as_failure(
            "acquire", document_id=document_id, attempt=attempts + 1
        )
        failure.retryable = target is DocumentStatus.FAILED_RETRYABLE
        self.store.record_failure(run_id, failure)
        self.store.set_status(
            document_id,
            target,
            release_claim=True,
            last_error=failure,
            increment_attempts=True,
        )
        # The document did not reach COMPLETED, but bytes may still have been
        # downloaded, validated and hashed — an XML full text while every PDF location
        # failed, for example. Those artifacts stay in the corpus, so they get the
        # mandatory sidecar too; without it they are unreachable through the documented
        # layout, which is silent data loss rather than an honest failure.
        artifacts = self.store.get_artifacts(document_id)
        if artifacts:
            try:
                self._publish_sidecar(
                    document_id,
                    artifacts,
                    run_id=run_id,
                    status=target.value,
                    attempts=attempts + 1,
                )
            except StorageError as exc:
                # A disk problem must not replace the failure actually being reported,
                # so it is recorded next to it instead of raised over it.
                LOGGER.warning(
                    "document %s: could not write the sidecar for its partial "
                    "artifacts: %s",
                    document_id,
                    exc.message,
                )
                self.store.record_failure(
                    run_id, exc.as_failure("sidecar", document_id=document_id)
                )
            if (
                ArtifactKind.PDF.value not in artifacts
                and ArtifactKind.XML.value in artifacts
            ):
                collector.increment("partial_fulltext")

        collector.stats.failures.append(failure.to_dict())
        collector.increment(
            "failed_retryable"
            if target is DocumentStatus.FAILED_RETRYABLE
            else "failed_permanent"
        )
        LOGGER.warning(
            "document %s: %s (%s) %s",
            document_id,
            target.value,
            error.category.value,
            error.message,
        )

    def _record_document_failure(
        self,
        run_id: str,
        document_id: str,
        error: HarvesterError,
        operation: str,
        collector: StatsCollector,
    ) -> None:
        failure = error.as_failure(operation, document_id=document_id)
        self.store.record_failure(run_id, failure)
        collector.stats.failures.append(failure.to_dict())

    # --------------------------------------------------------------- idempotency

    def _is_already_complete(self, document_id: str) -> bool:
        """True when the corpus already holds this document's validated artifacts.

        This is the idempotency gate (MASTER_SPEC section 25, AC-003): a second run
        over the same inputs re-downloads nothing. It is also half of reconciliation —
        if state claims COMPLETED but the file is gone, the document is demoted back to
        QUEUED and re-acquired instead of being reported as a success (section 48).
        """
        status = self.store.get_document_status(document_id)
        if status is not DocumentStatus.COMPLETED:
            return False
        artifacts = self.store.get_artifacts(document_id)
        pdf = artifacts.get(ArtifactKind.PDF.value)
        if pdf is None:
            self.store.set_status(document_id, DocumentStatus.QUEUED, release_claim=True)
            return False
        path = artifact_path(self.config.storage_root, document_id, ArtifactKind.PDF.value)
        if not path.exists():
            LOGGER.warning(
                "document %s: state says COMPLETED but %s is missing; re-queueing",
                document_id,
                path.name,
            )
            self.store.delete_artifact(document_id, ArtifactKind.PDF)
            self.store.set_status(document_id, DocumentStatus.QUEUED, release_claim=True)
            return False
        sidecar = artifact_path(self.config.storage_root, document_id, "json")
        if not sidecar.exists():
            LOGGER.warning(
                "document %s: sidecar missing; rewriting from state", document_id
            )
            metadata = self.store.get_metadata(document_id)
            row = self.store.get_document_row(document_id)
            write_sidecar(
                self.config.storage_root,
                document_id,
                build_sidecar(
                    SidecarInputs(
                        document_id=document_id,
                        metadata=metadata,
                        artifacts=artifacts,
                        run_id=(row["first_run_id"] if row is not None else ""),
                        status=DocumentStatus.COMPLETED.value,
                        attempts=int(row["attempts"]) if row is not None else 0,
                    )
                ),
            )
        return True

    def _adopt_orphan_files(
        self,
        run_id: str,
        document_id: str,
        artifacts: dict[str, ArtifactRecord],
        metadata: dict[str, Any],
        collector: StatsCollector,
    ) -> dict[str, ArtifactRecord]:
        """Adopt an artifact published just before the process died.

        The file was renamed into place, but the process terminated before the state
        row was written. The bytes are re-validated here — adoption never assumes a
        file is good merely because it exists (MASTER_SPEC sections 3.1 and 48).
        """
        adopted: dict[str, ArtifactRecord] = {}
        for kind in (ArtifactKind.PDF, ArtifactKind.XML):
            if kind.value in artifacts:
                continue
            path = artifact_path(self.config.storage_root, document_id, kind.value)
            if not path.exists():
                continue
            try:
                if kind is ArtifactKind.PDF:
                    result = validate_pdf(
                        path, min_size_bytes=self.config.downloads.min_pdf_size_bytes
                    )
                else:
                    result = validate_xml(
                        path, min_size_bytes=self.config.downloads.min_xml_size_bytes
                    )
            except HarvesterError as exc:
                LOGGER.warning(
                    "document %s: discarding unvalidatable orphan %s (%s)",
                    document_id,
                    path.name,
                    exc.message,
                )
                try:
                    path.unlink()
                except OSError:  # pragma: no cover
                    pass
                continue

            record = ArtifactRecord(
                kind=kind,
                filename=path.name,
                sha256=result.sha256,
                size_bytes=result.size_bytes,
                retrieved_at=utc_now_iso(),
                source=Source.OPENALEX,
                original_url="",
                resolved_url="",
                http_status=0,
                content_type=None,
            )
            # Recover the acquisition provenance from the recorded candidates when the
            # file's origin can be identified unambiguously; otherwise leave it empty
            # rather than inventing a source.
            self.store.record_artifact(document_id, record, run_id=run_id)
            adopted[kind.value] = record
            collector.increment("reconciled")
            LOGGER.info(
                "document %s: adopted pre-existing validated %s artifact",
                document_id,
                kind.value,
            )
        return adopted

    # ------------------------------------------------------------- reconciliation

    def _sweep_parts(self, run_id: str, collector: StatsCollector) -> None:
        """Remove temporary files abandoned by a terminated process."""
        removed = sweep_stale_parts(self.config.storage_root)
        if removed:
            LOGGER.info("run %s: removed %d stale .part files", run_id, len(removed))
            collector.add_note(f"removed {len(removed)} stale temporary files")

    def _reconcile(self, run_id: str, collector: StatsCollector) -> None:
        """Recover documents abandoned mid-acquisition by a terminated process."""
        stale = self.store.stale_claims()
        if stale:
            count = self.store.requeue_stale(stale)
            collector.increment("reconciled", count)
            LOGGER.info("run %s: re-queued %d documents left mid-acquisition", run_id, count)

    # ---------------------------------------------------------------- suspension

    def _begin_suspension(self, error: BudgetExhaustedError) -> None:
        with self._suspension_lock:
            if self._suspension is None:
                self._suspension = error
        self._stop.set()

    def _raise_if_suspended(self) -> None:
        with self._suspension_lock:
            suspension = self._suspension
        if suspension is None:
            suspension = self.clients.suspended_error()
            if suspension is not None:
                self._begin_suspension(suspension)
        if suspension is not None:
            raise SuspendedRun(suspension)

    def _record_suspension(
        self, run_id: str, error: BudgetExhaustedError, collector: StatsCollector
    ) -> None:
        details = error.suspension_details()
        collector.set("suspend_reason", error.message)
        collector.set("suspend_details", details)
        collector.add_note(
            "provider daily budget exhausted; run suspended cleanly and can be resumed "
            f"with: harvester resume {run_id}"
        )
        failure = error.as_failure("budget", source=error.provider)
        self.store.record_failure(run_id, failure)
        LOGGER.warning(
            "run %s SUSPENDED: %s (reset_in_seconds=%s)",
            run_id,
            error.message,
            details.get("reset_in_seconds"),
        )

    # ------------------------------------------------------------------ finalise

    def _finalise(
        self, run_id: str, status: RunStatus, collector: StatsCollector, started: float
    ) -> HarvestResult:
        stats = collector.snapshot()
        stats.status = status.value
        stats.finished_at = utc_now_iso()
        stats.duration_seconds = round(time.monotonic() - started, 3)
        stats.retry_count = self.store.retry_count_for_run(run_id)

        counts = self.store.status_counts(run_id)
        stats.skipped = counts.get(DocumentStatus.SKIPPED.value, 0)
        artifact_counts = self.store.artifact_counts(run_id)
        stats.pdf_count = artifact_counts.get("pdf", 0)
        stats.xml_count = artifact_counts.get("xml", 0)

        # The state store holds every failure; the report embeds a bounded sample so a
        # large run cannot produce an unusable multi-gigabyte JSON file. Nothing is
        # lost — the full set stays queryable in the ``failures`` table.
        all_failures = self.store.failures_for_run(run_id)
        stats.failure_count = len(all_failures)
        stats.failures = all_failures[:MAX_REPORTED_FAILURES]
        if stats.failure_count > MAX_REPORTED_FAILURES:
            collector.add_note(
                f"run report lists the first {MAX_REPORTED_FAILURES} of "
                f"{stats.failure_count} failures; all are in the state database"
            )
            stats.notes = collector.snapshot().notes

        if stats.partial_fulltext:
            # Without this, a report showing downloaded=1 next to completed=0 reads as
            # a bug in the counters rather than as the outcome it actually describes.
            collector.add_note(
                f"{stats.partial_fulltext} document(s) acquired validated XML full text "
                "but no PDF; a PDF is required for COMPLETED, so they are reported as "
                "failed while their XML and its JSON sidecar remain in the corpus"
            )
            stats.notes = collector.snapshot().notes

        self.store.finish_run(
            run_id,
            status,
            stats=stats.to_dict(),
            suspend_reason=stats.suspend_reason,
            suspend_details=stats.suspend_details,
        )
        report_path = write_report(self.config.reports_dir, stats)
        LOGGER.info("run %s finished with status %s", run_id, status.value)
        return HarvestResult(
            run_id=run_id, status=status, stats=stats, report_path=report_path
        )


# --------------------------------------------------------------------- helpers


def _new_run_id() -> str:
    return f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"


def _record_terminal_outcome(ledger: _ProviderOperation, error: BaseException) -> None:
    """Close a provider request that is unwinding, without masking what unwound it.

    A ledger write that fails while an exception is in flight must not replace that
    exception — the operator would then be told about SQLite instead of about the
    provider. It is logged loudly and the original failure continues to propagate.
    """
    try:
        ledger.failed(error)
    except Exception:  # pragma: no cover - only reachable on a failing state store
        LOGGER.exception(
            "could not record the terminal outcome of provider request %s",
            ledger.request_id,
        )


def _candidate_request(
    candidate: FulltextCandidate, document_id: str, index: int, offered: int
) -> dict[str, Any]:
    """The auditable, secret-safe description of one acquisition candidate.

    Only what the provider itself offered about the location, with every URL redacted:
    an auditor can re-trace which location was tried, in which order, on whose word —
    and nothing in here can carry a credential.
    """
    return {
        "document_id": document_id,
        "artifact_kind": candidate.kind.value,
        "url": redact_url(candidate.url),
        "landing_page_url": (
            redact_url(candidate.landing_page_url) if candidate.landing_page_url else None
        ),
        "offered_by": candidate.source.value,
        "host_type": candidate.host_type,
        "version": candidate.version,
        "license": candidate.license,
        "candidate_index": index,
        "candidate_count": offered,
    }


def _acquisition_evidence(outcome: AcquisitionOutcome) -> dict[str, Any]:
    """Outcome fields for a candidate that produced a validated artifact."""
    artifact = outcome.artifact
    return {
        "http_status": artifact.http_status,
        "result_count": 1,
        "details": {
            "artifact_kind": artifact.kind.value,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "content_type": artifact.content_type,
            # Recorded already redacted when the artifact record was built.
            "resolved_url": artifact.resolved_url,
            "http_attempts": outcome.attempts,
        },
    }


def _candidates_from_metadata(metadata: dict[str, Any]) -> list[FulltextCandidate]:
    candidates: list[FulltextCandidate] = []
    for raw in metadata.get("candidates") or []:
        candidate = _candidate_from_dict(raw)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _candidates_for_source(metadata: dict[str, Any], source: Source) -> list[FulltextCandidate]:
    return [c for c in _candidates_from_metadata(metadata) if c.source is source]


def _candidate_from_dict(raw: Any) -> FulltextCandidate | None:
    if not isinstance(raw, dict):
        return None
    url = raw.get("url")
    kind = raw.get("kind")
    source = raw.get("source")
    if not isinstance(url, str) or not url:
        return None
    try:
        return FulltextCandidate(
            url=url,
            kind=ArtifactKind(kind),
            source=Source(source),
            host_type=raw.get("host_type"),
            version=raw.get("version"),
            license=raw.get("license"),
            landing_page_url=raw.get("landing_page_url"),
        )
    except ValueError:
        return None


def _source_record_metadata(record: SourceRecord) -> dict[str, Any]:
    """Map one provider view onto the canonical fields used by merge decisions."""
    return {
        "doi": record.doi,
        "title": record.title,
        "authors": list(record.authors),
        "publication_year": record.publication_year,
        "journal": record.journal,
        "abstract": record.abstract,
        "is_oa": record.is_oa,
        "oa_status": record.oa_status,
        "oa_status_source": record.source.value if record.oa_status else None,
        "domain_tags": list(record.domain_tags),
        "topics": list(record.topics),
        "identifiers": dict(record.identifiers),
        "candidates": [candidate.to_dict() for candidate in record.candidates],
        "discovered_via": [],
        "cross_checks": [],
    }


def _merge_candidates_into_metadata(
    metadata: dict[str, Any], candidates: list[FulltextCandidate]
) -> dict[str, Any]:
    existing = list(metadata.get("candidates") or [])
    seen = {(c.get("url"), c.get("kind")) for c in existing}
    for candidate in candidates:
        key = (candidate.url, candidate.kind.value)
        if key in seen:
            continue
        seen.add(key)
        existing.append(candidate.to_dict())
    metadata["candidates"] = existing
    return metadata


def _order_candidates(
    candidates: list[FulltextCandidate], kind: ArtifactKind
) -> list[FulltextCandidate]:
    """Deterministic acquisition order for one artifact kind.

    Locations that answer automated clients with a challenge sort last, whatever
    offered them. Then provider priority (OpenAlex, then Europe PMC, then Unpaywall),
    then the order in which the provider offered them, then the URL as a final
    tie-break so the sequence is a total order and never depends on dictionary
    iteration.
    """
    selected = [c for c in candidates if c.kind is kind]
    decorated = [
        (
            1 if _is_challenged_location(c.url) else 0,
            _SOURCE_PRIORITY.get(c.source.value, len(_SOURCE_PRIORITY)),
            index,
            c.url,
            c,
        )
        for index, c in enumerate(selected)
    ]
    decorated.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    ordered: list[FulltextCandidate] = []
    seen: set[str] = set()
    for *_, url, candidate in decorated:
        if url in seen:
            continue
        seen.add(url)
        ordered.append(candidate)
    return ordered
