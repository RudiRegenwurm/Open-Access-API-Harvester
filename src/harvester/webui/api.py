# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""JSON API for the local control center.

Every handler is a thin adapter: it validates the request, calls an existing core
service, and shapes the result for display. No harvesting, validation, retry or
storage rule is implemented here.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..advisor import (
    MAX_QUERY_CHARS,
    MAX_QUESTION_CHARS,
    AdvisorError,
    AdvisorNotConfiguredError,
    advisor_readiness,
    build_advisor,
    normalise_question,
)
from ..config import Config
from ..errors import ConfigurationError, HarvesterError
from ..http import ClientPool
from ..identity import artifact_path, document_id_for_doi, normalize_doi
from ..models import ArtifactKind, DocumentStatus, RunStatus
from ..preview import PREVIEW_LIMIT, preview_discovery, query_fingerprint
from ..providers.openalex import OpenAlexQuery
from ..state import StateStore
from ..storage import read_sidecar
from ..util import to_json
from ..vocabulary import COUNTRIES, LANGUAGES, normalize_countries, normalize_languages
from . import settings_store
from .runmanager import BusyError, RunManager

LOGGER = logging.getLogger("harvester.webui.api")

#: Run states an operator can meaningfully continue.
RESUMABLE = {RunStatus.SUSPENDED, RunStatus.INTERRUPTED, RunStatus.RUNNING, RunStatus.FAILED}

#: The two ways a query can reach discovery (Assisted Search V1 section 21). Requests
#: that name neither are Conventional Search, which keeps every existing client — the
#: CLI, older bookmarks, scripts — working exactly as before.
SEARCH_MODES = ("conventional", "assisted")

#: How far back an identical search is still worth mentioning before a repeat run.
SIMILAR_RUN_WINDOW_DAYS = 14

#: How much run history the repeat check reads. Recent work, not the whole archive.
SIMILAR_RUN_SCAN = 200

#: Document states grouped by the question an operator is actually asking.
#:
#: "Is the full text in hand?" is no longer answerable from the state alone: a document
#: whose PDF locations all failed can still hold a validated XML, and FAILED_PERMANENT
#: covers both that case and the one where nothing was retrieved. That grouping lives in
#: ``StateStore.acquisition_state_counts``, which reads the artifacts. What remains here
#: are the states that describe work in flight, where no artifact is involved.
IN_FLIGHT_STATES = (DocumentStatus.ACQUIRING.value, DocumentStatus.VALIDATING.value)
PENDING_STATES = (
    DocumentStatus.DISCOVERED.value,
    DocumentStatus.NORMALIZED.value,
    DocumentStatus.QUEUED.value,
)


class ApiError(Exception):
    """An error with an HTTP status and an operator-readable message."""

    def __init__(self, status: int, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


class AppContext:
    """Shared, long-lived objects for the server process."""

    def __init__(
        self, config_path: Path, *, config: Config | None = None,
        config_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        # CLI overrides retain their precedence throughout this server session,
        # including reloads after settings saves. Never persist them to the file.
        self._config_overrides = dict(config_overrides or {})
        self._config = config or self._load_config()
        self._store: StateStore | None = None
        self.runs = RunManager(lambda: self.config)
        #: How a query advisor is built. Replaced by the tests with one that talks to
        #: the deterministic mock transport instead of a real provider.
        self.advisor_factory = build_advisor

    # -- configuration -------------------------------------------------------

    def _load_config(self) -> Config:
        path = self.config_path if self.config_path.exists() else None
        return Config.load(config_path=path, overrides=self._config_overrides)

    @property
    def config(self) -> Config:
        return self._config

    def reload_config(self) -> Config:
        """Re-read configuration after a settings change, reopening state if moved."""
        previous_db = self._config.state_db
        self._config = self._load_config()
        if self._store is not None and self._config.state_db != previous_db:
            self._store.close()
            self._store = None
        return self._config

    # -- state ---------------------------------------------------------------

    @property
    def store(self) -> StateStore:
        if self._store is None:
            self._store = StateStore(self.config.state_db)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None


# --------------------------------------------------------------------- helpers


def _int(params: dict[str, str], key: str, default: int | None = None) -> int | None:
    raw = params.get(key)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ApiError(400, f"{key} must be a whole number") from exc


def _bool(params: dict[str, str], key: str) -> bool | None:
    raw = params.get(key)
    if raw in (None, ""):
        return None
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _query_from_payload(payload: dict[str, Any]) -> OpenAlexQuery:
    """Build the discovery query from the form, letting the core validate it."""
    topic_id = (payload.get("topic_id") or "").strip() or None
    search = (payload.get("search") or "").strip() or None
    if not topic_id and not search:
        raise ApiError(
            400,
            "Enter a search term or an OpenAlex Topic ID so the harvester knows what "
            "to look for.",
        )
    extra = [f.strip() for f in (payload.get("extra_filters") or []) if str(f).strip()]
    try:
        query = OpenAlexQuery(
            topic_id=topic_id,
            primary_topic_only=bool(payload.get("primary_topic_only")),
            search=search,
            publication_year=payload.get("year") or None,
            from_publication_year=payload.get("from_year") or None,
            to_publication_year=payload.get("to_year") or None,
            is_oa=payload.get("is_oa", True) is not False,
            has_doi=payload.get("has_doi", True) is not False,
            oa_status=(payload.get("oa_status") or "").strip() or None,
            # Absent or empty means "any", which is exactly what these requests meant
            # before the constraints existed. The query object validates the codes.
            languages=payload.get("languages") or [],
            affiliation_countries=payload.get("affiliation_countries") or [],
            extra_filters=extra,
        )
        query.filter_string()  # fail fast on an unusable query
    except ConfigurationError as exc:
        raise ApiError(400, str(exc)) from exc
    return query


def _filters_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The structured discovery filters, without requiring a query.

    Used for advisor context, where the user may not have any query text yet. The keys
    match :class:`OpenAlexQuery` so one filter model serves both search modes
    (specification section 41).
    """
    extra = [f.strip() for f in (payload.get("extra_filters") or []) if str(f).strip()]
    return {
        "topic_id": (payload.get("topic_id") or "").strip() or None,
        "primary_topic_only": bool(payload.get("primary_topic_only")),
        "publication_year": payload.get("year") or None,
        "from_publication_year": payload.get("from_year") or None,
        "to_publication_year": payload.get("to_year") or None,
        "is_oa": payload.get("is_oa", True) is not False,
        "has_doi": payload.get("has_doi", True) is not False,
        "oa_status": (payload.get("oa_status") or "").strip() or None,
        "languages": normalize_languages(payload.get("languages")),
        "affiliation_countries": normalize_countries(payload.get("affiliation_countries")),
        "extra_filters": extra,
    }


def _search_mode(payload: dict[str, Any]) -> str:
    mode = (payload.get("search_mode") or "conventional").strip().lower()
    if mode not in SEARCH_MODES:
        raise ApiError(400, f"Search mode must be one of {', '.join(SEARCH_MODES)}.")
    return mode


def _text(payload: dict[str, Any], key: str, limit: int) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:limit] or None


def _search_provenance(payload: dict[str, Any], mode: str, query: OpenAlexQuery) -> dict[str, Any]:
    """Record how this run's query was built (specification sections 31-32).

    The effective query is read from the query object that discovery will actually
    execute — never from the request body — so what the report shows and what the
    harvester ran cannot drift apart. Nothing here may carry a credential: the advisor
    block is rebuilt from a fixed set of descriptive keys rather than copied.
    """
    provenance: dict[str, Any] = {
        "search_mode": mode,
        "effective_search_query": query.search,
    }
    if mode == "conventional":
        return provenance

    generated = _text(payload, "generated_query", MAX_QUERY_CHARS)
    provenance.update(
        {
            "research_question": _text(payload, "research_question", MAX_QUESTION_CHARS),
            "generated_query": generated,
            "query_edited": bool(generated) and generated != query.search,
        }
    )
    advisor = payload.get("advisor")
    if isinstance(advisor, dict):
        described = {
            key: str(advisor[key])[:200]
            for key in ("provider", "model", "prompt_version")
            if isinstance(advisor.get(key), str) and advisor[key].strip()
        }
        if described:
            provenance["query_advisor"] = described
    return provenance


def _sum_states(counts: dict[str, int], states: tuple[str, ...]) -> int:
    return sum(counts.get(state, 0) for state in states)


def _run_outcome(
    counts: dict[str, int],
    stats: dict[str, Any] | None,
    acquisition: dict[str, int],
) -> dict[str, Any]:
    """Two different questions, answered separately (UX hardening item 6).

    A run reporting "10 completed, 15 failed" while its own report says
    ``completed: 0, already_complete: 10`` is not contradicting itself: the first pair
    describes *the documents this run touched, as they are now*, and the second
    describes *what this run actually did*. Collapsing them into one row of numbers is
    what made that read as a bug. They are two blocks here, each labelled with the
    question it answers.

    ``corpus_state`` is recomputed live from the documents, so it follows a later
    retry or a re-queue. ``this_run`` is the frozen counter set the run wrote when it
    finished and never changes afterwards; it is ``None`` for a run still in flight
    and for runs recorded before counters were persisted. Nothing here is derived,
    estimated or filled in — every number is read from state.
    """
    # Grouped by what full text is in hand rather than by state-machine cell: a
    # document whose PDF locations all failed while Europe PMC served a valid XML has
    # partial full text, and calling that "unavailable" contradicts the file beside it.
    # The buckets are mutually exclusive and sum to the total (``state.py``).
    corpus_state = {
        "full_text_available": acquisition["full_text_available"],
        "partial_full_text": acquisition["partial_full_text"],
        "unavailable": acquisition["unavailable"],
        "retryable": acquisition["retryable"],
        "in_progress": acquisition["in_progress"],
        "total": acquisition["total"],
    }
    work: dict[str, Any] | None = None
    if stats:
        work = {
            "newly_acquired": stats.get("completed", 0),
            "already_available": stats.get("already_complete", 0),
            "attempted": stats.get("attempted", 0),
            "downloaded": stats.get("downloaded", 0),
            "bytes_downloaded": stats.get("bytes_downloaded", 0),
            "retries_attempted": stats.get("retry_count", 0),
            "failed_permanent": stats.get("failed_permanent", 0),
            "failed_retryable": stats.get("failed_retryable", 0),
            # Already recorded by the run itself; no new counter is needed for it.
            "partial_full_text": stats.get("partial_fulltext", 0),
            "discovered": stats.get("records_discovered", 0),
            "duplicates": stats.get("duplicates", 0),
        }
    return {"corpus_state": corpus_state, "this_run": work}


#: Acquisition settings a run records about itself, and the label to explain each by.
#: Read out of the redacted configuration snapshot the run already stores, so no new
#: state and no new write path is needed to compare them.
_COMPARABLE_SETTINGS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("xml_policy", ("xml_policy",), "full-text XML"),
    ("europe_pmc", ("europe_pmc", "enabled"), "Europe PMC"),
    ("unpaywall", ("unpaywall", "enabled"), "Unpaywall"),
)


def _comparable_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """The requested acquisition settings, in the shape a run records them."""
    return {
        "xml_policy": payload.get("xml_policy") or "preferred",
        "europe_pmc": payload.get("europe_pmc", True) is not False,
        "unpaywall": payload.get("unpaywall", True) is not False,
    }


def _describe_settings(run: Any) -> dict[str, Any]:
    """Read the comparable settings back out of a run's stored configuration."""
    snapshot = run.config if isinstance(run.config, dict) else {}
    result: dict[str, Any] = {}
    for name, path, _label in _COMPARABLE_SETTINGS:
        cursor: Any = snapshot
        for key in path:
            cursor = cursor.get(key) if isinstance(cursor, dict) else None
        result[name] = cursor
    return result


def _describe_differences(run: Any, wanted: dict[str, Any], limit: int | None) -> list[str]:
    """Plain-language differences between a past run and the one being requested."""
    differences: list[str] = []
    recorded = _describe_settings(run)
    for name, _path, label in _COMPARABLE_SETTINGS:
        was, now = recorded.get(name), wanted.get(name)
        if was is None or was == now:
            continue
        render = (lambda v: ("on" if v else "off")) if isinstance(now, bool) else str
        differences.append(f"{label}: {render(was)} then, {render(now)} now")
    if run.record_limit != limit:
        differences.append(
            f"document limit: {run.record_limit or 'none'} then, {limit or 'none'} now"
        )
    return differences


def _iso_days_ago(days: int) -> str:
    """The cut-off timestamp, in the same ISO-8601 form runs are stamped with."""
    moment = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=days)
    return moment.isoformat().replace("+00:00", "Z")


def _run_summary(run: Any, store: StateStore) -> dict[str, Any]:
    counts = store.status_counts(run.run_id)
    acquisition = store.acquisition_state_counts(run.run_id)
    artifacts = store.artifact_counts(run.run_id)
    stats = run.stats or {}
    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "dry_run": run.dry_run,
        "search_mode": (run.search_provenance or {}).get("search_mode", "conventional"),
        "record_limit": run.record_limit,
        "discovery_complete": run.discovery_complete,
        "discovery_pages": run.discovery_pages,
        "discovery_seen": run.discovery_seen,
        "suspend_reason": run.suspend_reason,
        "resumable": run.status in RESUMABLE,
        "query": run.query,
        "document_counts": counts,
        "artifacts": artifacts,
        "completed": counts.get(DocumentStatus.COMPLETED.value, 0),
        "failed": (
            counts.get(DocumentStatus.FAILED_PERMANENT.value, 0)
            + counts.get(DocumentStatus.FAILED_RETRYABLE.value, 0)
        ),
        "duration_seconds": stats.get("duration_seconds"),
        "bytes_downloaded": stats.get("bytes_downloaded"),
        "retry_count": stats.get("retry_count"),
        "outcome": _run_outcome(counts, run.stats, acquisition),
    }


def _live_progress(context: AppContext, run_id: str | None) -> dict[str, Any] | None:
    """Progress read from the authoritative state, never a parallel counter."""
    if not run_id:
        return None
    run = context.store.get_run(run_id)
    if run is None:
        return None
    counts = context.store.status_counts(run_id)
    acquisition = context.store.acquisition_state_counts(run_id)
    artifacts = context.store.artifact_counts(run_id)
    total = sum(counts.values())
    settled = (
        counts.get(DocumentStatus.COMPLETED.value, 0)
        + counts.get(DocumentStatus.FAILED_PERMANENT.value, 0)
        + counts.get(DocumentStatus.FAILED_RETRYABLE.value, 0)
        + counts.get(DocumentStatus.SKIPPED.value, 0)
    )
    stats = run.stats or {}
    return {
        "run_id": run_id,
        "status": run.status.value,
        "started_at": run.started_at,
        # What the run actually took, for a run that has stopped taking anything. Time
        # since it started is a different number, and once a run has finished it is the
        # wrong one: it keeps growing for as long as the page is left open.
        "finished_at": run.finished_at,
        "duration_seconds": stats.get("duration_seconds"),
        "discovery_complete": run.discovery_complete,
        "discovery_pages": run.discovery_pages,
        "discovered": run.discovery_seen,
        "document_counts": counts,
        "artifacts": artifacts,
        "settled": settled,
        "total": total,
        # A share of the documents discovered *so far*. While discovery is still
        # paging, "so far" keeps growing, which is why the caller is told whether
        # discovery has finished rather than being left to read this as a forecast.
        "percent": round(100 * settled / total) if total else 0,
        # Everything below is measured, never estimated (UX hardening item 2).
        # Grouped by the full text actually in hand, so a document holding a validated
        # XML is not counted among the ones nothing could be acquired for.
        "acquired": acquisition["full_text_available"],
        "partial_full_text": acquisition["partial_full_text"],
        "unavailable": acquisition["unavailable"],
        "retryable": acquisition["retryable"],
        "in_flight": _sum_states(counts, IN_FLIGHT_STATES),
        "pending": _sum_states(counts, PENDING_STATES),
        # The liveness signal. A harvest waiting on a slow provider looks identical to
        # a hung one until you can see when it last did something.
        "last_activity_at": context.store.last_activity_at(run_id),
    }


# -------------------------------------------------------------------- handlers


class Api:
    """Route handlers. Each returns a JSON-serialisable object."""

    def __init__(self, context: AppContext) -> None:
        self.context = context

    # -- overview ------------------------------------------------------------

    def dashboard(self, params: dict[str, str]) -> dict[str, Any]:
        store = self.context.store
        config = self.context.config
        counts = store.status_counts()
        acquisition = store.acquisition_state_counts()
        artifacts = store.artifact_counts()
        verification = store.latest_verification()
        ready, problems = settings_store.can_start_harvest(config)

        return {
            "cards": {
                "documents": sum(counts.values()),
                "completed": counts.get(DocumentStatus.COMPLETED.value, 0),
                "full_text_available": acquisition["full_text_available"],
                "partial_full_text": acquisition["partial_full_text"],
                "pdfs": artifacts.get("pdf", 0),
                "xmls": artifacts.get("xml", 0),
                "bytes": artifacts.get("bytes", 0),
                "failed": (
                    counts.get(DocumentStatus.FAILED_PERMANENT.value, 0)
                    + counts.get(DocumentStatus.FAILED_RETRYABLE.value, 0)
                ),
            },
            "document_counts": counts,
            "acquisition_counts": acquisition,
            "last_verification": (
                {
                    "checked_at": verification["checked_at"],
                    "ok": verification["ok"],
                    "deep": verification["deep"],
                    "problem_count": verification["problem_count"],
                    "orphan_count": verification["orphan_count"],
                }
                if verification
                else None
            ),
            "recent_runs": [_run_summary(r, store) for r in store.list_runs(6)],
            "activity": self.activity(params),
            "providers": settings_store.provider_readiness(config),
            "can_start": ready,
            "blockers": problems,
            "storage_root": str(config.storage_root),
        }

    def activity(self, params: dict[str, str]) -> dict[str, Any]:
        snapshot = self.context.runs.snapshot()
        run_id = snapshot.get("run_id") if snapshot else None
        if run_id is None and snapshot is None:
            latest = self.context.store.latest_run(statuses=(RunStatus.RUNNING,))
            run_id = latest.run_id if latest else None
        return {
            "busy": self.context.runs.busy,
            "operation": snapshot,
            "progress": _live_progress(self.context, run_id),
        }

    def providers(self, params: dict[str, str]) -> dict[str, Any]:
        config = self.context.config
        ready, problems = settings_store.can_start_harvest(config)
        return {
            "providers": settings_store.provider_readiness(config),
            "can_start": ready,
            "blockers": problems,
            # Reported alongside the providers, never inside them: the advisor is not
            # a discovery provider and can never block a harvest.
            "advisor": advisor_readiness(config),
        }

    def vocabulary(self, params: dict[str, str]) -> dict[str, Any]:
        """The value lists the search-constraint controls offer.

        Served rather than duplicated in the front end, so the browser, the API and
        the CLI validate against exactly the same tables. Sorted by display name
        because that is the order the operator reads them in.
        """
        return {
            "languages": [
                {"code": code, "name": name}
                for code, name in sorted(LANGUAGES.items(), key=lambda kv: kv[1])
            ],
            "countries": [
                {"code": code, "name": name}
                for code, name in sorted(COUNTRIES.items(), key=lambda kv: kv[1])
            ],
        }

    # -- assisted search -----------------------------------------------------

    def search_advice(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Turn a research question into one recommended query (section 35.1).

        No harvest, no discovery and no state change happens here. On every failure
        path the caller keeps its research question and filters, and Conventional
        Search stays available.
        """
        config = self.context.config
        try:
            question = normalise_question(payload.get("research_question"))
        except AdvisorError as exc:
            raise ApiError(400, str(exc), detail={"reason": exc.reason}) from exc

        filters = _filters_from_payload(payload)
        advisor = self.context.advisor_factory(config)
        try:
            advice = advisor.generate(question, filters)
        except AdvisorNotConfiguredError as exc:
            raise ApiError(503, str(exc), detail={"reason": exc.reason}) from exc
        except AdvisorError as exc:
            LOGGER.warning("query advisor rejected a response: %s", exc)
            raise ApiError(
                502,
                "Assisted Search is currently unavailable. Your research question has "
                "been preserved. You can retry or switch to Conventional Search.",
                detail={"reason": exc.reason, "category": exc.category.value},
            ) from exc
        except HarvesterError as exc:
            # Network, timeout, rate limit, auth: the message is already redacted by
            # the shared HTTP layer, so it is safe to show the category, not the text.
            LOGGER.warning("query advisor call failed: %s", exc)
            raise ApiError(
                502,
                "Assisted Search is currently unavailable. Your research question has "
                "been preserved. You can retry or switch to Conventional Search.",
                detail={"reason": "provider_unavailable", "category": exc.category.value},
            ) from exc
        finally:
            advisor.close()

        result = advice.to_dict()
        result["research_question"] = question
        result["filters"] = filters
        return result

    def search_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Discovery-only preview of the current query (section 35.2).

        Uses the real discovery implementation and writes nothing: no run, no corpus
        document, no seen marker, no retry state, no artifact.
        """
        config = self.context.config
        query = _query_from_payload(payload)
        limit = payload.get("limit")
        try:
            limit = int(limit) if limit not in (None, "", 0) else PREVIEW_LIMIT
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "Preview limit must be a whole number.") from exc

        try:
            with ClientPool(config) as clients:
                result = preview_discovery(config, clients, query, limit=limit)
        except HarvesterError as exc:
            LOGGER.warning("discovery preview failed: %s", exc)
            raise ApiError(
                502,
                f"The preview could not be completed: {exc}",
                detail={"category": exc.category.value},
            ) from exc

        response = result.to_dict()
        response["query"] = query.to_dict()
        return response

    def similar_runs(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Recent full harvests whose discovery inputs are identical to this request.

        Advisory only (UX hardening item 8). Nothing is started, nothing is written and
        no answer here can prevent a harvest: deliberately re-running the same search is
        how a result is reproduced, and this endpoint exists so that doing it by
        accident is visible, not so that doing it on purpose is harder.

        The match is :func:`query_fingerprint` — the effective query text, every
        structured discovery filter and the composed provider filter string — against
        the query each run recorded for itself. Two runs match exactly when they would
        send the same discovery request. Previews are excluded: they acquire nothing,
        so repeating one costs nothing worth warning about.

        Known limitation, deliberately not solved here: acquisition-side settings are
        *not* part of the match. A run whose XML policy, provider toggles or document
        limit differed still matches on its search, because the search is what the
        operator recognises. Those differences are reported per run in
        ``differences`` instead of silently hiding the run, so the decision stays with
        the person making it.
        """
        query = _query_from_payload(payload)
        fingerprint = query_fingerprint(query)
        limit = payload.get("limit")
        limit = int(limit) if limit not in (None, "", 0) else None
        wanted = _comparable_settings(payload)

        cutoff = _iso_days_ago(SIMILAR_RUN_WINDOW_DAYS)
        matches = []
        for run in self.context.store.list_runs(SIMILAR_RUN_SCAN):
            if run.dry_run or not isinstance(run.query, dict):
                continue
            if (run.started_at or "") < cutoff:
                continue
            if hashlib.sha256(to_json(run.query).encode("utf-8")).hexdigest() != fingerprint:
                continue
            summary = _run_summary(run, self.context.store)
            summary["differences"] = _describe_differences(run, wanted, limit)
            matches.append(summary)

        return {
            "fingerprint": fingerprint,
            "window_days": SIMILAR_RUN_WINDOW_DAYS,
            "runs": matches,
        }

    # -- harvesting ----------------------------------------------------------

    def start_harvest(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self.context.config
        dry_run = bool(payload.get("dry_run"))
        mode = _search_mode(payload)

        overrides = self._session_overrides(payload)
        if overrides:
            config = self._config_with(overrides)

        if not dry_run:
            ready, problems = settings_store.can_start_harvest(config)
            if not ready:
                raise ApiError(400, problems[0], detail={"blockers": problems})

        query = _query_from_payload(payload)
        limit = payload.get("limit")
        limit = int(limit) if limit not in (None, "", 0) else None

        if mode == "assisted":
            self._require_current_preview(payload, query)

        label = (
            f"Preview for “{query.search or query.topic_id}”"
            if dry_run
            else f"Harvesting “{query.search or query.topic_id}”"
        )
        try:
            operation = self.context.runs.start_harvest(
                query,
                limit=limit,
                dry_run=dry_run,
                label=label,
                search_provenance=_search_provenance(payload, mode, query),
            )
        except BusyError as exc:
            raise ApiError(409, str(exc)) from exc
        return {"started": True, "operation": operation.to_dict()}

    def _require_current_preview(self, payload: dict[str, Any], query: OpenAlexQuery) -> None:
        """Refuse an Assisted Search harvest unless its preview is still current.

        The gate is enforced here, not only in the browser: the fingerprint is
        recomputed from the query the harvest is actually about to run, so an approval
        that was granted for a different query or a different filter set cannot
        authorise this one (specification section 27).
        """
        expected = query_fingerprint(query)
        supplied = payload.get("preview_fingerprint")
        if not isinstance(supplied, str) or not supplied:
            raise ApiError(
                409,
                "Preview the results before starting an Assisted Search harvest.",
                detail={"reason": "preview_required", "fingerprint": expected},
            )
        if supplied != expected:
            raise ApiError(
                409,
                "The query or filters changed after the preview. Preview again before "
                "starting the harvest.",
                detail={"reason": "stale_preview", "fingerprint": expected},
            )

    def _session_overrides(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Per-harvest toggles from the form, applied without touching saved settings."""
        overrides: dict[str, Any] = {}
        if "europe_pmc" in payload:
            overrides["europe_pmc.enabled"] = bool(payload["europe_pmc"])
        if "unpaywall" in payload:
            overrides["unpaywall.enabled"] = bool(payload["unpaywall"])
        if payload.get("xml_policy"):
            policy = str(payload["xml_policy"])
            if policy not in ("preferred", "required", "disabled"):
                raise ApiError(400, "XML policy must be preferred, required or disabled")
            overrides["xml_policy"] = policy
        return overrides

    def _config_with(self, overrides: dict[str, Any]) -> Config:
        path = self.context.config_path if self.context.config_path.exists() else None
        try:
            config = Config.load(config_path=path, overrides=overrides)
        except ConfigurationError as exc:
            raise ApiError(400, str(exc)) from exc
        return config

    def resume_run(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        run = self.context.store.get_run(run_id)
        if run is None:
            raise ApiError(404, f"No run named {run_id}.")
        try:
            operation = self.context.runs.resume_run(run_id)
        except BusyError as exc:
            raise ApiError(409, str(exc)) from exc
        return {"started": True, "operation": operation.to_dict()}

    # -- runs ----------------------------------------------------------------

    def list_runs(self, params: dict[str, str]) -> dict[str, Any]:
        store = self.context.store
        limit = _int(params, "limit", 50) or 50
        return {"runs": [_run_summary(run, store) for run in store.list_runs(limit)]}

    def run_detail(self, run_id: str, params: dict[str, str]) -> dict[str, Any]:
        store = self.context.store
        run = store.get_run(run_id)
        if run is None:
            raise ApiError(404, f"No run named {run_id}.")

        document_ids = store.documents_for_run(run_id)
        documents = []
        for document_id in document_ids[:500]:
            row = store.get_document_row(document_id)
            if row is None:
                continue
            artifacts = store.get_artifacts(document_id)
            documents.append(
                {
                    "document_id": document_id,
                    "title": row["title"],
                    "doi": row["doi"],
                    "status": row["status"],
                    "attempts": row["attempts"],
                    "has_pdf": ArtifactKind.PDF.value in artifacts,
                    "has_xml": ArtifactKind.XML.value in artifacts,
                    "size_bytes": sum(a.size_bytes for a in artifacts.values()),
                }
            )

        report_path = Path(self.context.config.reports_dir) / f"{run_id}.json"
        summary = _run_summary(run, store)
        summary.update(
            {
                "search_provenance": run.search_provenance,
                "config": run.config,
                "suspend_details": run.suspend_details,
                "stats": run.stats,
                "failures": store.failures_for_run(run_id),
                "failure_groups": store.failure_groups(run_id),
                "documents": documents,
                "document_total": len(document_ids),
                "acquisition_counts": store.acquisition_state_counts(run_id),
                "storage_root": str(self.context.config.storage_root),
                "report_path": str(report_path) if report_path.exists() else None,
                "providers_used": store.providers_for_run(run_id),
            }
        )
        return summary

    def run_report(self, run_id: str, params: dict[str, str]) -> dict[str, Any]:
        path = Path(self.context.config.reports_dir) / f"{run_id}.json"
        if not path.exists():
            raise ApiError(404, f"No report file has been written for {run_id}.")
        from ..util import parse_json

        return parse_json(path.read_text(encoding="utf-8"), {})

    # -- corpus --------------------------------------------------------------

    def corpus(self, params: dict[str, str]) -> dict[str, Any]:
        store = self.context.store
        page = max(1, _int(params, "page", 1) or 1)
        size = min(200, max(1, _int(params, "page_size", 50) or 50))
        documents, total = store.query_documents(
            search=params.get("search") or None,
            oa_status=params.get("oa_status") or None,
            has_pdf=_bool(params, "has_pdf"),
            has_xml=_bool(params, "has_xml"),
            year=_int(params, "year"),
            journal=params.get("journal") or None,
            status=params.get("status") or None,
            offset=(page - 1) * size,
            limit=size,
        )
        return {
            "documents": documents,
            "total": total,
            "page": page,
            "page_size": size,
            "pages": max(1, -(-total // size)),
        }

    def corpus_facets(self, params: dict[str, str]) -> dict[str, Any]:
        return self.context.store.corpus_facets()

    def corpus_detail(self, document_id: str, params: dict[str, str]) -> dict[str, Any]:
        store = self.context.store
        document_id = self._resolve_document_id(document_id)
        row = store.get_document_row(document_id)
        if row is None:
            raise ApiError(404, "That document is not in the corpus.")

        metadata = store.get_metadata(document_id)
        artifacts = {kind: record.to_dict() for kind, record in store.get_artifacts(document_id).items()}
        sidecar = read_sidecar(self.context.config.storage_root, document_id)
        files = {}
        for kind in (ArtifactKind.PDF.value, ArtifactKind.XML.value, "json"):
            path = artifact_path(self.context.config.storage_root, document_id, kind)
            files[kind] = {"exists": path.exists(), "path": str(path)}

        return {
            "document_id": document_id,
            "status": row["status"],
            "attempts": row["attempts"],
            "first_seen": row["created_at"],
            "last_updated": row["updated_at"],
            "metadata": metadata,
            "artifacts": artifacts,
            "files": files,
            "sidecar": sidecar,
            "provenance": (sidecar or {}).get("provenance"),
            "cross_checks": metadata.get("cross_checks") or [],
            "source_records": store.source_records_for_document(document_id),
            "failures": store.failures_for_document(document_id),
            "attempts_log": store.attempts_for_document(document_id),
        }

    def _resolve_document_id(self, identifier: str) -> str:
        """Accept either a document_id or any DOI representation."""
        doi = normalize_doi(identifier)
        if doi is not None:
            return document_id_for_doi(doi)
        return identifier

    def artifact_file(self, document_id: str, kind: str) -> tuple[Path, str]:
        """Resolve an artifact path for download, reusing the core's path safety."""
        if kind not in ("pdf", "xml", "json"):
            raise ApiError(404, "Unknown artifact type.")
        document_id = self._resolve_document_id(document_id)
        try:
            path = artifact_path(self.context.config.storage_root, document_id, kind)
        except HarvesterError as exc:
            raise ApiError(400, str(exc)) from exc
        if not path.exists():
            raise ApiError(404, f"No {kind.upper()} file is stored for this document.")
        content_type = {
            "pdf": "application/pdf",
            "xml": "application/xml",
            "json": "application/json",
        }[kind]
        return path, content_type

    # -- verification --------------------------------------------------------

    def start_verify(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            operation = self.context.runs.start_verify(deep=bool(payload.get("deep")))
        except BusyError as exc:
            raise ApiError(409, str(exc)) from exc
        return {"started": True, "operation": operation.to_dict()}

    def verify_history(self, params: dict[str, str]) -> dict[str, Any]:
        limit = _int(params, "limit", 10) or 10
        history = self.context.store.list_verifications(limit)
        return {"latest": history[0] if history else None, "history": history}

    # -- failures ------------------------------------------------------------

    def failures(self, params: dict[str, str]) -> dict[str, Any]:
        store = self.context.store
        run_id = params.get("run_id") or None
        groups = store.failure_groups(run_id)
        failed_ids = store.failed_document_ids(run_id)
        documents = []
        for document_id in failed_ids[:300]:
            row = store.get_document_row(document_id)
            if row is None:
                continue
            recent = store.failures_for_document(document_id, limit=1)
            documents.append(
                {
                    "document_id": document_id,
                    "title": row["title"],
                    "doi": row["doi"],
                    "status": row["status"],
                    "attempts": row["attempts"],
                    "category": recent[0]["category"] if recent else None,
                    "message": recent[0].get("message") if recent else None,
                    "last_seen": recent[0].get("ts") if recent else None,
                }
            )
        return {
            "groups": groups,
            "documents": documents,
            "total_failed": len(failed_ids),
            "run_id": run_id,
        }

    def retry_failures(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Re-queue failed documents using the core's own transition rules."""
        store = self.context.store
        run_id = (payload.get("run_id") or "").strip() or None
        category = (payload.get("category") or "").strip() or None
        explicit = [str(d) for d in (payload.get("document_ids") or [])]

        if explicit:
            targets = explicit
        elif category:
            targets = store.documents_by_failure_category(category, run_id=run_id)
        else:
            targets = store.failed_document_ids(run_id)

        if not targets:
            return {"requeued": 0, "message": "There was nothing to re-queue."}

        changed = store.reset_documents_for_retry(targets)
        return {
            "requeued": changed,
            "message": (
                f"{changed} document(s) moved back to the queue. Start a harvest or "
                "resume a run to retry them."
            ),
        }

    # -- settings ------------------------------------------------------------

    def get_settings(self, params: dict[str, str]) -> dict[str, Any]:
        return settings_store.describe(self.context.config, self.context.config_path)

    def put_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        updates = payload.get("updates")
        if not isinstance(updates, dict) or not updates:
            raise ApiError(400, "No settings were supplied.")
        try:
            settings_store.apply_updates(self.context.config_path, updates)
        except ConfigurationError as exc:
            raise ApiError(400, str(exc)) from exc
        self.context.reload_config()
        result = settings_store.describe(self.context.config, self.context.config_path)
        result["saved"] = True
        result["message"] = "Settings saved."
        return result


__all__ = ["Api", "ApiError", "AppContext"]
