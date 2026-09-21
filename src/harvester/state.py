# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""Persistent state (MASTER_SPEC sections 22, 46, 47, 49).

SQLite in WAL mode. One connection per thread; every state transition that could leave
inconsistent state runs inside a transaction. Claiming uses ``BEGIN IMMEDIATE`` so two
workers — in the same process or in two processes — can never both acquire the same
logical document (MASTER_SPEC section 49, AC-020).

The database is *not* the authoritative copy of artifact content. Files are. The
reconciliation helpers exist precisely because the filesystem and the database are two
separate consistency domains (MASTER_SPEC section 48).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import Failure, StateError
from .models import (
    ArtifactKind,
    ArtifactRecord,
    DocumentStatus,
    LogicalDocument,
    RunStatus,
    Source,
    SourceRecord,
    transition_allowed,
)
from .util import parse_json, to_json, utc_now_iso

#: Version 4 adds the append-only Evidence Ledger described by ADR 0001. The V1
#: current-state tables stay in place as compatibility projections.
LOGGER = logging.getLogger("harvester.state")

SCHEMA_VERSION = "4"

#: Versions this build can open and upgrade in place. Every step so far has been
#: additive. Version 4 also performs an idempotent, explicitly lossy-labelled backfill
#: from the latest legacy snapshot; it never pretends to reconstruct overwritten data.
_UPGRADABLE_FROM = frozenset({"1", "2", "3"})

PROVIDER_OUTCOME_STATUSES = frozenset({"HIT", "NO_HIT", "TIMEOUT", "ERROR"})

#: Columns added after the initial schema, applied with ``ALTER TABLE`` when absent.
#: ``CREATE TABLE IF NOT EXISTS`` cannot add a column to a table that already exists.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("runs", "search_provenance_json", "TEXT"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id               TEXT PRIMARY KEY,
    started_at           TEXT NOT NULL,
    finished_at          TEXT,
    status               TEXT NOT NULL,
    query_json           TEXT NOT NULL,
    config_json          TEXT NOT NULL,
    search_provenance_json TEXT,
    discovery_cursor     TEXT,
    discovery_complete   INTEGER NOT NULL DEFAULT 0,
    discovery_pages      INTEGER NOT NULL DEFAULT 0,
    discovery_seen       INTEGER NOT NULL DEFAULT 0,
    dry_run              INTEGER NOT NULL DEFAULT 0,
    record_limit         INTEGER,
    suspend_reason       TEXT,
    suspend_details_json TEXT,
    stats_json           TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    document_id      TEXT PRIMARY KEY,
    doi              TEXT UNIQUE,
    status           TEXT NOT NULL,
    title            TEXT,
    publication_year INTEGER,
    journal          TEXT,
    metadata_json    TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    first_run_id     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    claimed_by       TEXT,
    claimed_at       TEXT,
    last_error_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

CREATE TABLE IF NOT EXISTS run_documents (
    run_id        TEXT NOT NULL,
    document_id   TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    preexisting   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, document_id)
);
CREATE INDEX IF NOT EXISTS idx_run_documents_run ON run_documents(run_id);

CREATE TABLE IF NOT EXISTS source_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    record_json TEXT NOT NULL,
    UNIQUE (document_id, source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_source_records_doc ON source_records(document_id);

CREATE TABLE IF NOT EXISTS artifacts (
    document_id  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    filename     TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    retrieved_at TEXT NOT NULL,
    source       TEXT NOT NULL,
    original_url TEXT NOT NULL,
    resolved_url TEXT NOT NULL,
    http_status  INTEGER NOT NULL,
    content_type TEXT,
    PRIMARY KEY (document_id, kind)
);

CREATE TABLE IF NOT EXISTS attempts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT,
    document_id    TEXT,
    operation      TEXT NOT NULL,
    source         TEXT,
    attempt_no     INTEGER NOT NULL,
    ts             TEXT NOT NULL,
    ok             INTEGER NOT NULL,
    error_category TEXT,
    http_status    INTEGER,
    message        TEXT,
    url            TEXT,
    retryable      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_attempts_doc ON attempts(document_id);

CREATE TABLE IF NOT EXISTS failures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT,
    document_id  TEXT,
    ts           TEXT NOT NULL,
    category     TEXT NOT NULL,
    operation    TEXT NOT NULL,
    source       TEXT,
    retryable    INTEGER NOT NULL,
    failure_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failures_run ON failures(run_id);
CREATE INDEX IF NOT EXISTS idx_failures_doc ON failures(document_id);

CREATE TABLE IF NOT EXISTS verifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at    TEXT NOT NULL,
    deep          INTEGER NOT NULL,
    ok            INTEGER NOT NULL,
    problem_count INTEGER NOT NULL,
    orphan_count  INTEGER NOT NULL,
    report_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verifications_time ON verifications(checked_at);

-- Evidence Ledger V1 (ADR 0001). These are append-only facts; the tables above remain
-- the mutable V1 projections consumed by the current CLI and web UI.
CREATE TABLE IF NOT EXISTS works (
    work_id            TEXT PRIMARY KEY,
    legacy_document_id TEXT NOT NULL UNIQUE,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    publication_id     TEXT PRIMARY KEY,
    work_id            TEXT NOT NULL,
    legacy_document_id TEXT NOT NULL UNIQUE,
    doi                TEXT,
    created_at         TEXT NOT NULL,
    FOREIGN KEY (work_id) REFERENCES works(work_id)
);
CREATE INDEX IF NOT EXISTS idx_publications_work ON publications(work_id);

CREATE TABLE IF NOT EXISTS provider_requests (
    request_id   TEXT PRIMARY KEY,
    run_id       TEXT,
    provider     TEXT NOT NULL,
    operation    TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    request_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_requests_run ON provider_requests(run_id);

CREATE TABLE IF NOT EXISTS provider_outcomes (
    outcome_id     TEXT PRIMARY KEY,
    request_id     TEXT NOT NULL UNIQUE,
    status         TEXT NOT NULL CHECK (status IN ('HIT', 'NO_HIT', 'TIMEOUT', 'ERROR')),
    completed_at   TEXT NOT NULL,
    http_status    INTEGER,
    result_count   INTEGER,
    error_category TEXT,
    details_json   TEXT NOT NULL,
    FOREIGN KEY (request_id) REFERENCES provider_requests(request_id)
);

CREATE TABLE IF NOT EXISTS source_observations (
    observation_id  TEXT PRIMARY KEY,
    publication_id  TEXT NOT NULL,
    run_id           TEXT,
    request_id      TEXT,
    provider        TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    raw_json        TEXT,
    raw_sha256      TEXT,
    evidence_quality TEXT NOT NULL,
    origin          TEXT NOT NULL,
    FOREIGN KEY (publication_id) REFERENCES publications(publication_id),
    FOREIGN KEY (request_id) REFERENCES provider_requests(request_id)
);
CREATE INDEX IF NOT EXISTS idx_source_observations_publication
    ON source_observations(publication_id, observed_at, observation_id);
CREATE INDEX IF NOT EXISTS idx_source_observations_source
    ON source_observations(provider, source_id);

CREATE TABLE IF NOT EXISTS merge_decisions (
    decision_id             TEXT PRIMARY KEY,
    publication_id          TEXT NOT NULL,
    run_id                  TEXT,
    field_name              TEXT NOT NULL,
    decision                TEXT NOT NULL,
    policy                  TEXT NOT NULL,
    candidate_observation_id TEXT,
    chosen_observation_id   TEXT,
    existing_value_json     TEXT NOT NULL,
    incoming_value_json     TEXT NOT NULL,
    chosen_value_json       TEXT NOT NULL,
    decided_at              TEXT NOT NULL,
    origin                  TEXT NOT NULL,
    FOREIGN KEY (publication_id) REFERENCES publications(publication_id),
    FOREIGN KEY (candidate_observation_id) REFERENCES source_observations(observation_id),
    FOREIGN KEY (chosen_observation_id) REFERENCES source_observations(observation_id)
);
CREATE INDEX IF NOT EXISTS idx_merge_decisions_publication
    ON merge_decisions(publication_id, field_name, decided_at, decision_id);

CREATE TABLE IF NOT EXISTS files (
    file_id       TEXT PRIMARY KEY,
    sha256        TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    UNIQUE (sha256, size_bytes)
);

CREATE TABLE IF NOT EXISTS acquisitions (
    acquisition_id   TEXT PRIMARY KEY,
    publication_id   TEXT NOT NULL,
    file_id          TEXT NOT NULL,
    run_id           TEXT,
    provider         TEXT NOT NULL,
    artifact_kind    TEXT NOT NULL,
    observed_filename TEXT NOT NULL,
    original_url     TEXT NOT NULL,
    resolved_url     TEXT NOT NULL,
    retrieved_at     TEXT NOT NULL,
    http_status      INTEGER NOT NULL,
    content_type     TEXT,
    evidence_quality TEXT NOT NULL,
    origin           TEXT NOT NULL,
    FOREIGN KEY (publication_id) REFERENCES publications(publication_id),
    FOREIGN KEY (file_id) REFERENCES files(file_id)
);
CREATE INDEX IF NOT EXISTS idx_acquisitions_publication
    ON acquisitions(publication_id, retrieved_at, acquisition_id);
"""


def _ensure_wal(connection: sqlite3.Connection, *, attempts: int = 40) -> None:
    """Put the database into WAL mode, tolerating concurrent openers.

    Switching journal mode needs an exclusive lock and — unlike ordinary statements —
    SQLite refuses it immediately with SQLITE_BUSY rather than waiting for
    ``busy_timeout`` while other connections exist. Two harvesters starting at the same
    moment would therefore fail to open the database at all.

    Journal mode is a persistent property of the file, so it only has to be set once.
    This reads the current mode first, retries briefly if another opener is setting it
    concurrently, and gives up quietly if the database is simply busy: WAL improves
    reader/writer concurrency but is not required for correctness, and refusing to open
    the store would be far worse than running in the default journal mode.
    """
    for attempt in range(attempts):
        try:
            row = connection.execute("PRAGMA journal_mode").fetchone()
            if row is not None and str(row[0]).lower() == "wal":
                return
            connection.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc).lower():
                raise
            time.sleep(0.05 * (attempt + 1))
    try:
        row = connection.execute("PRAGMA journal_mode").fetchone()
        mode = str(row[0]) if row else "unknown"
    except sqlite3.OperationalError:  # pragma: no cover - database wedged
        mode = "unknown"
    LOGGER.warning(
        "could not switch the state database to WAL mode (currently %s); "
        "continuing with reduced read/write concurrency",
        mode,
    )


def _add_missing_columns(connection: sqlite3.Connection) -> None:
    """Apply the additive column migrations to a database created by an older build.

    Tolerates the race where two processes open a fresh database at the same moment:
    ``executescript`` commits before this runs, so both can reach the ``ALTER`` and one
    of them loses. Losing means the column already exists, which is the desired state.
    """
    for table, column, declaration in _ADDED_COLUMNS:
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column in existing:
            continue
        try:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        except sqlite3.OperationalError as exc:  # pragma: no cover - narrow race
            if "duplicate column" not in str(exc).lower():
                raise


@dataclass(slots=True)
class RunRecord:
    run_id: str
    started_at: str
    finished_at: str | None
    status: RunStatus
    query: dict[str, Any]
    config: dict[str, Any]
    discovery_cursor: str | None
    discovery_complete: bool
    discovery_pages: int
    discovery_seen: int
    dry_run: bool
    record_limit: int | None
    suspend_reason: str | None
    suspend_details: dict[str, Any] | None
    stats: dict[str, Any] | None
    #: How the run's query was constructed (Assisted Search V1 sections 31-32).
    #: ``None`` for runs recorded before the feature existed. Never holds a secret.
    search_provenance: dict[str, Any] | None = None


class StateStore:
    """Thread-safe SQLite state store."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        try:
            self._initialise()
        except BaseException:
            # A rejected schema or failed migration must not leave an unreachable
            # connection holding locks after construction aborts.
            self.close()
            raise

    # ------------------------------------------------------------------ plumbing

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=30.0, isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        _ensure_wal(connection)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        with self._connections_lock:
            self._connections.append(connection)
        return connection

    @property
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "connection", None)
        if conn is None:
            conn = self._connect()
            self._local.connection = conn
        return conn

    def _initialise(self) -> None:
        # DDL is idempotent and intentionally precedes the version transaction: an
        # older database needs the destination tables before its rows can be migrated.
        # ``busy_timeout`` plus IF NOT EXISTS makes concurrent first openers safe.
        conn = self.connection
        conn.executescript(_SCHEMA)
        _add_missing_columns(conn)
        with self.transaction() as conn:
            # INSERT OR IGNORE makes stamping a fresh database safe when several
            # processes initialise it at the same time.
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row["value"] in _UPGRADABLE_FROM:
                _backfill_evidence_ledger(conn)
                conn.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                    (SCHEMA_VERSION,),
                )
            elif row["value"] != SCHEMA_VERSION:
                raise StateError(
                    f"state database {self.path} has schema version {row['value']}, "
                    f"this build requires {SCHEMA_VERSION}"
                )

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Run a block inside a single transaction.

        ``BEGIN IMMEDIATE`` takes the write lock up front, which is what makes the
        claim operation safe against concurrent workers and concurrent processes.
        """
        conn = self.connection
        if conn.in_transaction:
            # Nested use: the outermost caller owns the commit boundary.
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    def close(self) -> None:
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:  # pragma: no cover - best effort teardown
                pass
        self._local = threading.local()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------------- runs

    def create_run(
        self,
        run_id: str,
        *,
        query: dict[str, Any],
        config: dict[str, Any],
        dry_run: bool = False,
        record_limit: int | None = None,
        search_provenance: dict[str, Any] | None = None,
    ) -> RunRecord:
        now = utc_now_iso()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO runs (run_id, started_at, status, query_json, config_json,
                                     dry_run, record_limit, search_provenance_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    now,
                    RunStatus.RUNNING.value,
                    to_json(query),
                    to_json(config),
                    int(dry_run),
                    record_limit,
                    to_json(search_provenance) if search_provenance else None,
                ),
            )
        run = self.get_run(run_id)
        assert run is not None
        return run

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self.connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return _row_to_run(row) if row else None

    def latest_run(self, *, statuses: Sequence[RunStatus] | None = None) -> RunRecord | None:
        sql = "SELECT * FROM runs"
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" WHERE status IN ({placeholders})"
            params.extend(status.value for status in statuses)
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT 1"
        row = self.connection.execute(sql, params).fetchone()
        return _row_to_run(row) if row else None

    def list_runs(self, limit: int = 20) -> list[RunRecord]:
        rows = self.connection.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_run(row) for row in rows]

    def update_run_cursor(
        self, run_id: str, cursor: str | None, *, complete: bool, pages: int, seen: int
    ) -> None:
        """Persist discovery progress.

        Called only after the page's records have been committed, so a crash re-reads
        at most one page and never skips one (MASTER_SPEC section 10.3).
        """
        with self.transaction() as conn:
            conn.execute(
                """UPDATE runs
                      SET discovery_cursor = ?, discovery_complete = ?,
                          discovery_pages = ?, discovery_seen = ?
                    WHERE run_id = ?""",
                (cursor, int(complete), pages, seen, run_id),
            )

    def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        *,
        stats: dict[str, Any] | None = None,
        suspend_reason: str | None = None,
        suspend_details: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """UPDATE runs
                      SET status = ?, finished_at = ?, stats_json = ?,
                          suspend_reason = ?, suspend_details_json = ?
                    WHERE run_id = ?""",
                (
                    status.value,
                    utc_now_iso(),
                    to_json(stats) if stats is not None else None,
                    suspend_reason,
                    to_json(suspend_details) if suspend_details is not None else None,
                    run_id,
                ),
            )

    # ---------------------------------------------------------- provider evidence

    def begin_provider_request(
        self,
        *,
        run_id: str | None,
        provider: str,
        operation: str,
        request: dict[str, Any],
    ) -> str:
        """Append one secret-safe logical provider request and return its identity.

        The caller passes a deliberately bounded request description (for example the
        canonical query and cursor), never transport credentials. A missing terminal
        outcome is meaningful evidence that the process stopped between both writes.
        """
        request_id = _new_evidence_id("request")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO provider_requests
                       (request_id, run_id, provider, operation, requested_at, request_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    request_id,
                    run_id,
                    provider,
                    operation,
                    utc_now_iso(),
                    to_json(request),
                ),
            )
        return request_id

    def finish_provider_request(
        self,
        request_id: str,
        *,
        status: str,
        http_status: int | None = None,
        result_count: int | None = None,
        error_category: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        """Append the single terminal outcome for a logical provider request."""
        normalized_status = str(status).upper()
        if normalized_status not in PROVIDER_OUTCOME_STATUSES:
            raise StateError(
                f"invalid provider outcome {status!r}; expected one of "
                f"{sorted(PROVIDER_OUTCOME_STATUSES)}"
            )
        outcome_id = _new_evidence_id("outcome")
        try:
            with self.transaction() as conn:
                request = conn.execute(
                    "SELECT 1 FROM provider_requests WHERE request_id = ?", (request_id,)
                ).fetchone()
                if request is None:
                    raise StateError(f"unknown provider request: {request_id}")
                conn.execute(
                    """INSERT INTO provider_outcomes
                           (outcome_id, request_id, status, completed_at, http_status,
                            result_count, error_category, details_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        outcome_id,
                        request_id,
                        normalized_status,
                        utc_now_iso(),
                        http_status,
                        result_count,
                        error_category,
                        to_json(details or {}),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StateError(f"provider request {request_id} already has an outcome") from exc
        return outcome_id

    def provider_requests_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Return request/outcome pairs in append order for inspection and tests."""
        rows = self.connection.execute(
            """SELECT r.request_id, r.provider, r.operation, r.requested_at,
                      r.request_json, o.outcome_id, o.status, o.completed_at,
                      o.http_status, o.result_count, o.error_category, o.details_json
                 FROM provider_requests r
                 LEFT JOIN provider_outcomes o ON o.request_id = r.request_id
                WHERE r.run_id = ?
                ORDER BY r.rowid""",
            (run_id,),
        ).fetchall()
        return [
            {
                "request_id": row["request_id"],
                "provider": row["provider"],
                "operation": row["operation"],
                "requested_at": row["requested_at"],
                "request": parse_json(row["request_json"], {}),
                "outcome": (
                    {
                        "outcome_id": row["outcome_id"],
                        "status": row["status"],
                        "completed_at": row["completed_at"],
                        "http_status": row["http_status"],
                        "result_count": row["result_count"],
                        "error_category": row["error_category"],
                        "details": parse_json(row["details_json"], {}),
                    }
                    if row["outcome_id"] is not None
                    else None
                ),
            }
            for row in rows
        ]

    # ----------------------------------------------------------------- documents

    def upsert_discovered_document(
        self,
        run_id: str,
        document: LogicalDocument,
        source_record: SourceRecord,
        *,
        provider_request_id: str | None = None,
    ) -> tuple[bool, bool]:
        """Register a discovered record.

        Returns ``(created, newly_linked_to_run)``.

        Deduplication happens here, before any acquisition work (MASTER_SPEC section
        12): a document already known — from this run or an earlier one — collapses
        into the existing row and merely gains another source record.
        """
        now = utc_now_iso()
        incoming_metadata = _document_metadata(document)
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT document_id, status, metadata_json FROM documents WHERE document_id = ?",
                (document.document_id,),
            ).fetchone()

            if existing is None:
                existing_metadata: dict[str, Any] = {}
                if document.doi:
                    clash = conn.execute(
                        "SELECT document_id FROM documents WHERE doi = ?", (document.doi,)
                    ).fetchone()
                    if clash is not None:
                        raise StateError(
                            f"DOI {document.doi} is already bound to document "
                            f"{clash['document_id']}, refusing to bind it to "
                            f"{document.document_id}"
                        )
                conn.execute(
                    """INSERT INTO documents (document_id, doi, status, title,
                                              publication_year, journal, metadata_json,
                                              first_run_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        document.document_id,
                        document.doi,
                        DocumentStatus.NORMALIZED.value,
                        document.title,
                        document.publication_year,
                        document.journal,
                        to_json(incoming_metadata),
                        run_id,
                        now,
                        now,
                    ),
                )
                created = True
                canonical_metadata = incoming_metadata
            else:
                created = False
                existing_metadata = parse_json(existing["metadata_json"], {})
                canonical_metadata = _merge_metadata(existing_metadata, incoming_metadata)
                conn.execute(
                    """UPDATE documents
                          SET metadata_json = ?, updated_at = ?,
                              title = COALESCE(title, ?),
                              publication_year = COALESCE(publication_year, ?),
                              journal = COALESCE(journal, ?)
                        WHERE document_id = ?""",
                    (
                        to_json(canonical_metadata),
                        now,
                        document.title,
                        document.publication_year,
                        document.journal,
                        document.document_id,
                    ),
                )

            _, publication_id = _ensure_evidence_identities(
                conn,
                document.document_id,
                doi=document.doi,
                created_at=now,
            )
            observation_id = _insert_source_observation(
                conn,
                publication_id,
                source_record,
                run_id=run_id,
                request_id=provider_request_id,
                origin="native",
            )
            _record_merge_decisions(
                conn,
                publication_id=publication_id,
                run_id=run_id,
                existing=existing_metadata,
                incoming=incoming_metadata,
                chosen=canonical_metadata,
                candidate_observation_id=observation_id,
                origin="native",
            )

            conn.execute(
                """INSERT INTO source_records (document_id, source, source_id, fetched_at,
                                               record_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(document_id, source, source_id)
                   DO UPDATE SET fetched_at = excluded.fetched_at,
                                 record_json = excluded.record_json""",
                (
                    document.document_id,
                    source_record.source.value,
                    source_record.source_id,
                    source_record.fetched_at,
                    to_json(source_record.to_dict()),
                ),
            )

            cursor = conn.execute(
                """INSERT OR IGNORE INTO run_documents
                       (run_id, document_id, discovered_at, preexisting)
                   VALUES (?, ?, ?, ?)""",
                (run_id, document.document_id, now, int(not created)),
            )
            newly_linked = cursor.rowcount > 0
        return created, newly_linked

    def add_source_record(
        self,
        document_id: str,
        source_record: SourceRecord,
        *,
        run_id: str | None = None,
        provider_request_id: str | None = None,
    ) -> str:
        with self.transaction() as conn:
            _, publication_id = _ensure_evidence_identities(conn, document_id)
            observation_id = _insert_source_observation(
                conn,
                publication_id,
                source_record,
                run_id=run_id,
                request_id=provider_request_id,
                origin="native",
            )
            conn.execute(
                """INSERT INTO source_records (document_id, source, source_id, fetched_at,
                                               record_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(document_id, source, source_id)
                   DO UPDATE SET fetched_at = excluded.fetched_at,
                                 record_json = excluded.record_json""",
                (
                    document_id,
                    source_record.source.value,
                    source_record.source_id,
                    source_record.fetched_at,
                    to_json(source_record.to_dict()),
                ),
            )
        return observation_id

    def get_document_row(self, document_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()

    def get_document_status(self, document_id: str) -> DocumentStatus | None:
        row = self.connection.execute(
            "SELECT status FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        return DocumentStatus(row["status"]) if row else None

    def get_metadata(self, document_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT metadata_json FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise StateError(f"unknown document: {document_id}")
        return parse_json(row["metadata_json"], {})

    def update_metadata(
        self,
        document_id: str,
        metadata: dict[str, Any],
        *,
        run_id: str | None = None,
        candidate_observation_id: str | None = None,
        candidate_metadata: dict[str, Any] | None = None,
        policy_overrides: dict[str, str] | None = None,
        origin: str = "native",
    ) -> None:
        """Update the mutable projection and optionally explain the merge.

        Callers that also append a source observation wrap both calls in
        :meth:`transaction`. Nested transactions share the outer commit boundary, so
        the immutable evidence, merge decisions and compatibility projection either
        all become visible or none of them do.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            if row is None:
                raise StateError(f"unknown document: {document_id}")
            existing_metadata = parse_json(row["metadata_json"], {})
            now = utc_now_iso()
            conn.execute(
                """UPDATE documents
                      SET metadata_json = ?, title = ?, publication_year = ?, journal = ?,
                          updated_at = ?
                    WHERE document_id = ?""",
                (
                    to_json(metadata),
                    metadata.get("title"),
                    metadata.get("publication_year"),
                    metadata.get("journal"),
                    now,
                    document_id,
                ),
            )
            if candidate_metadata is not None:
                _, publication_id = _ensure_evidence_identities(conn, document_id)
                _record_merge_decisions(
                    conn,
                    publication_id=publication_id,
                    run_id=run_id,
                    existing=existing_metadata,
                    incoming=candidate_metadata,
                    chosen=metadata,
                    candidate_observation_id=candidate_observation_id,
                    origin=origin,
                    policy_overrides=policy_overrides,
                )

    def set_status(
        self,
        document_id: str,
        target: DocumentStatus,
        *,
        release_claim: bool = False,
        last_error: Failure | None = None,
        increment_attempts: bool = False,
    ) -> None:
        """Apply a state transition, rejecting impossible ones (MASTER_SPEC section 23)."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM documents WHERE document_id = ?", (document_id,)
            ).fetchone()
            if row is None:
                raise StateError(f"unknown document: {document_id}")
            current = DocumentStatus(row["status"])
            if not transition_allowed(current, target):
                raise StateError(
                    f"illegal state transition for {document_id}: "
                    f"{current.value} -> {target.value}"
                )
            conn.execute(
                f"""UPDATE documents
                       SET status = ?, updated_at = ?,
                           last_error_json = ?,
                           attempts = attempts + ?
                           {", claimed_by = NULL, claimed_at = NULL" if release_claim else ""}
                     WHERE document_id = ?""",
                (
                    target.value,
                    utc_now_iso(),
                    to_json(last_error.to_dict()) if last_error is not None else None,
                    1 if increment_attempts else 0,
                    document_id,
                ),
            )

    def claim_document(self, document_id: str, worker_id: str) -> bool:
        """Atomically claim one document for acquisition.

        Returns ``False`` when another worker already holds it. The ``claimed_by IS
        NULL`` predicate combined with ``BEGIN IMMEDIATE`` is the mutual-exclusion
        mechanism required by MASTER_SPEC section 49.
        """
        now = utc_now_iso()
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE documents
                      SET status = ?, claimed_by = ?, claimed_at = ?, updated_at = ?
                    WHERE document_id = ?
                      AND claimed_by IS NULL
                      AND status IN (?, ?, ?, ?)""",
                (
                    DocumentStatus.ACQUIRING.value,
                    worker_id,
                    now,
                    now,
                    document_id,
                    DocumentStatus.DISCOVERED.value,
                    DocumentStatus.NORMALIZED.value,
                    DocumentStatus.QUEUED.value,
                    DocumentStatus.FAILED_RETRYABLE.value,
                ),
            )
            return cursor.rowcount == 1

    def release_claim(self, document_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE documents SET claimed_by = NULL, claimed_at = NULL WHERE document_id = ?",
                (document_id,),
            )

    def pending_documents_for_run(self, run_id: str) -> list[str]:
        """Documents of *run_id* the acquisition phase must consider.

        COMPLETED documents are included deliberately. They are not re-downloaded —
        the orchestrator's idempotency gate short-circuits them — but they must still
        be examined so that a COMPLETED record whose file has since vanished is
        detected and repaired rather than silently trusted (MASTER_SPEC section 48,
        AC-019). FAILED_PERMANENT and SKIPPED are excluded so a permanent failure
        never turns into an endless retry (AC-009).
        """
        statuses = (*PENDING_ORDER, DocumentStatus.COMPLETED.value)
        placeholders = ",".join("?" for _ in statuses)
        rows = self.connection.execute(
            f"""SELECT d.document_id
                  FROM documents d
                  JOIN run_documents rd ON rd.document_id = d.document_id
                 WHERE rd.run_id = ?
                   AND d.status IN ({placeholders})
                 ORDER BY d.document_id""",
            (run_id, *statuses),
        ).fetchall()
        return [row["document_id"] for row in rows]

    def documents_for_run(self, run_id: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT document_id FROM run_documents WHERE run_id = ? ORDER BY document_id",
            (run_id,),
        ).fetchall()
        return [row["document_id"] for row in rows]

    def all_document_ids(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT document_id FROM documents ORDER BY document_id"
        ).fetchall()
        return [row["document_id"] for row in rows]

    def status_counts(self, run_id: str | None = None) -> dict[str, int]:
        if run_id is None:
            rows = self.connection.execute(
                "SELECT status, COUNT(*) AS n FROM documents GROUP BY status"
            ).fetchall()
        else:
            rows = self.connection.execute(
                """SELECT d.status AS status, COUNT(*) AS n
                     FROM documents d
                     JOIN run_documents rd ON rd.document_id = d.document_id
                    WHERE rd.run_id = ?
                    GROUP BY d.status""",
                (run_id,),
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def acquisition_state_counts(self, run_id: str | None = None) -> dict[str, int]:
        """Documents grouped by *what full text is actually in hand*.

        ``status_counts`` answers a different question — which state machine cell a
        document sits in — and cannot tell a document with a validated XML from one
        with nothing at all, because both are FAILED_PERMANENT when no PDF could be
        acquired. Reporting the second as "full text unavailable" while its XML sits
        in the corpus is what this projection exists to prevent.

        Read-only and derived from the same ``artifacts`` rows the CLI and ``verify``
        consult. It classifies, it never decides: the persisted document status is
        untouched and COMPLETED still requires a PDF.

        The buckets are mutually exclusive and sum to the document total. Documents
        still moving take precedence over their artifacts — a half-finished document
        is reported as in progress, not as a result — and among settled documents the
        artifacts decide.
        """
        in_progress = (
            DocumentStatus.DISCOVERED.value,
            DocumentStatus.NORMALIZED.value,
            DocumentStatus.QUEUED.value,
            DocumentStatus.ACQUIRING.value,
            DocumentStatus.VALIDATING.value,
        )
        exists = (
            "EXISTS (SELECT 1 FROM artifacts a "
            "WHERE a.document_id = d.document_id AND a.kind = ?)"
        )
        bucket = f"""
            CASE
                WHEN d.status IN ({", ".join("?" for _ in in_progress)}) THEN 'in_progress'
                WHEN d.status = ? THEN 'retryable'
                WHEN {exists} THEN 'full_text_available'
                WHEN {exists} THEN 'partial_full_text'
                ELSE 'unavailable'
            END
        """
        params: list[Any] = [*in_progress, DocumentStatus.FAILED_RETRYABLE.value, "pdf", "xml"]
        if run_id is None:
            sql = f"SELECT {bucket} AS bucket, COUNT(*) AS n FROM documents d GROUP BY bucket"
        else:
            sql = (
                f"SELECT {bucket} AS bucket, COUNT(*) AS n FROM documents d "
                "JOIN run_documents rd ON rd.document_id = d.document_id "
                "WHERE rd.run_id = ? GROUP BY bucket"
            )
            params.append(run_id)

        counts = {
            "full_text_available": 0,
            "partial_full_text": 0,
            "unavailable": 0,
            "retryable": 0,
            "in_progress": 0,
        }
        for row in self.connection.execute(sql, params).fetchall():
            counts[row["bucket"]] = row["n"]
        counts["total"] = sum(counts.values())
        return counts

    def last_activity_at(self, run_id: str) -> str | None:
        """When this run last actually did something, or ``None`` if it never has.

        Liveness, not progress: a long-running harvest that is waiting on a slow
        provider has a total that has not moved for minutes, and the operator cannot
        tell that from a hung process. Both ledgers are consulted because either one
        can move on its own — an attempt is recorded for every provider request, a
        document timestamp changes on every state transition — and the later of the
        two is the honest answer.
        """
        row = self.connection.execute(
            """SELECT MAX(ts) AS ts FROM (
                   SELECT MAX(a.ts) AS ts FROM attempts a WHERE a.run_id = ?
                   UNION ALL
                   SELECT MAX(d.updated_at) AS ts
                     FROM documents d
                     JOIN run_documents rd ON rd.document_id = d.document_id
                    WHERE rd.run_id = ?
               )""",
            (run_id, run_id),
        ).fetchone()
        return row["ts"] if row else None

    def reset_documents_for_retry(self, document_ids: Sequence[str]) -> int:
        """Move failed documents back to QUEUED so a later run may retry them."""
        changed = 0
        with self.transaction() as conn:
            for document_id in document_ids:
                cursor = conn.execute(
                    """UPDATE documents
                          SET status = ?, claimed_by = NULL, claimed_at = NULL, updated_at = ?
                        WHERE document_id = ?
                          AND status IN (?, ?, ?)""",
                    (
                        DocumentStatus.QUEUED.value,
                        utc_now_iso(),
                        document_id,
                        DocumentStatus.FAILED_RETRYABLE.value,
                        DocumentStatus.FAILED_PERMANENT.value,
                        DocumentStatus.SKIPPED.value,
                    ),
                )
                changed += cursor.rowcount
        return changed

    # ----------------------------------------------------------------- artifacts

    def record_artifact(
        self, document_id: str, artifact: ArtifactRecord, *, run_id: str | None = None
    ) -> str:
        with self.transaction() as conn:
            _, publication_id = _ensure_evidence_identities(conn, document_id)
            acquisition_id = _insert_acquisition(
                conn,
                publication_id=publication_id,
                run_id=run_id,
                artifact=artifact,
                origin="native",
                evidence_quality="NATIVE",
            )
            conn.execute(
                """INSERT INTO artifacts (document_id, kind, filename, sha256, size_bytes,
                                          retrieved_at, source, original_url, resolved_url,
                                          http_status, content_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(document_id, kind) DO UPDATE SET
                       filename = excluded.filename,
                       sha256 = excluded.sha256,
                       size_bytes = excluded.size_bytes,
                       retrieved_at = excluded.retrieved_at,
                       source = excluded.source,
                       original_url = excluded.original_url,
                       resolved_url = excluded.resolved_url,
                       http_status = excluded.http_status,
                       content_type = excluded.content_type""",
                (
                    document_id,
                    artifact.kind.value,
                    artifact.filename,
                    artifact.sha256,
                    artifact.size_bytes,
                    artifact.retrieved_at,
                    artifact.source.value,
                    artifact.original_url,
                    artifact.resolved_url,
                    artifact.http_status,
                    artifact.content_type,
                ),
            )
        return acquisition_id

    def get_artifacts(self, document_id: str) -> dict[str, ArtifactRecord]:
        rows = self.connection.execute(
            "SELECT * FROM artifacts WHERE document_id = ? ORDER BY kind", (document_id,)
        ).fetchall()
        return {row["kind"]: _row_to_artifact(row) for row in rows}

    def delete_artifact(self, document_id: str, kind: ArtifactKind) -> None:
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM artifacts WHERE document_id = ? AND kind = ?",
                (document_id, kind.value),
            )

    def artifact_counts(self, run_id: str | None = None) -> dict[str, int]:
        if run_id is None:
            rows = self.connection.execute(
                "SELECT kind, COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes "
                "FROM artifacts GROUP BY kind"
            ).fetchall()
        else:
            rows = self.connection.execute(
                """SELECT a.kind AS kind, COUNT(*) AS n,
                          COALESCE(SUM(a.size_bytes), 0) AS bytes
                     FROM artifacts a
                     JOIN run_documents rd ON rd.document_id = a.document_id
                    WHERE rd.run_id = ?
                    GROUP BY a.kind""",
                (run_id,),
            ).fetchall()
        result = {"pdf": 0, "xml": 0, "bytes": 0}
        for row in rows:
            result[row["kind"]] = row["n"]
            result["bytes"] += row["bytes"]
        return result

    # -------------------------------------------------- attempts and failures

    def record_attempt(
        self,
        *,
        run_id: str | None,
        document_id: str | None,
        operation: str,
        attempt_no: int,
        ok: bool,
        source: str | None = None,
        error_category: str | None = None,
        http_status: int | None = None,
        message: str | None = None,
        url: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO attempts (run_id, document_id, operation, source, attempt_no,
                                         ts, ok, error_category, http_status, message, url,
                                         retryable)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    document_id,
                    operation,
                    source,
                    attempt_no,
                    utc_now_iso(),
                    int(ok),
                    error_category,
                    http_status,
                    message,
                    url,
                    None if retryable is None else int(retryable),
                ),
            )

    def record_failure(self, run_id: str | None, failure: Failure) -> None:
        """Persist a structured failure. Nothing is ever silently discarded."""
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO failures (run_id, document_id, ts, category, operation,
                                         source, retryable, failure_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    failure.document_id,
                    utc_now_iso(),
                    failure.category.value,
                    failure.operation,
                    failure.source,
                    int(failure.retryable),
                    to_json(failure.to_dict()),
                ),
            )

    def failures_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT failure_json, ts FROM failures WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [{**parse_json(row["failure_json"], {}), "ts": row["ts"]} for row in rows]

    def failures_for_document(self, document_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT failure_json, ts, run_id FROM failures "
            "WHERE document_id = ? ORDER BY id DESC LIMIT ?",
            (document_id, limit),
        ).fetchall()
        return [
            {**parse_json(row["failure_json"], {}), "ts": row["ts"], "run_id": row["run_id"]}
            for row in rows
        ]

    def attempts_for_document(self, document_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT operation, source, attempt_no, ts, ok, error_category,
                      http_status, message, url
                 FROM attempts WHERE document_id = ? ORDER BY id DESC LIMIT ?""",
            (document_id, limit),
        ).fetchall()
        return [
            {
                "operation": row["operation"],
                "source": row["source"],
                "attempt_no": row["attempt_no"],
                "ts": row["ts"],
                "ok": bool(row["ok"]),
                "error_category": row["error_category"],
                "http_status": row["http_status"],
                "message": row["message"],
                "url": row["url"],
            }
            for row in rows
        ]

    def source_records_for_document(self, document_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT source, source_id, fetched_at, record_json FROM source_records "
            "WHERE document_id = ? ORDER BY source",
            (document_id,),
        ).fetchall()
        return [
            {
                "source": row["source"],
                "source_id": row["source_id"],
                "fetched_at": row["fetched_at"],
                "record": parse_json(row["record_json"], {}),
            }
            for row in rows
        ]

    def source_observations_for_document(self, document_id: str) -> list[dict[str, Any]]:
        """Return immutable observations, oldest first, including native raw values."""
        rows = self.connection.execute(
            """SELECT o.observation_id, o.run_id, o.request_id, o.provider, o.source_id,
                      o.observed_at, o.normalized_json, o.raw_json, o.raw_sha256,
                      o.evidence_quality, o.origin
                 FROM source_observations o
                 JOIN publications p ON p.publication_id = o.publication_id
                WHERE p.legacy_document_id = ?
                ORDER BY o.rowid""",
            (document_id,),
        ).fetchall()
        return [
            {
                "observation_id": row["observation_id"],
                "run_id": row["run_id"],
                "request_id": row["request_id"],
                "provider": row["provider"],
                "source_id": row["source_id"],
                "observed_at": row["observed_at"],
                "normalized": parse_json(row["normalized_json"], {}),
                "raw": parse_json(row["raw_json"], None),
                "raw_sha256": row["raw_sha256"],
                "evidence_quality": row["evidence_quality"],
                "origin": row["origin"],
            }
            for row in rows
        ]

    def merge_decisions_for_document(self, document_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT m.* FROM merge_decisions m
                 JOIN publications p ON p.publication_id = m.publication_id
                WHERE p.legacy_document_id = ?
                ORDER BY m.rowid""",
            (document_id,),
        ).fetchall()
        return [
            {
                "decision_id": row["decision_id"],
                "run_id": row["run_id"],
                "field_name": row["field_name"],
                "decision": row["decision"],
                "policy": row["policy"],
                "candidate_observation_id": row["candidate_observation_id"],
                "chosen_observation_id": row["chosen_observation_id"],
                "existing_value": parse_json(row["existing_value_json"], None),
                "incoming_value": parse_json(row["incoming_value_json"], None),
                "chosen_value": parse_json(row["chosen_value_json"], None),
                "decided_at": row["decided_at"],
                "origin": row["origin"],
            }
            for row in rows
        ]

    def acquisitions_for_document(self, document_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT a.*, f.sha256, f.size_bytes
                 FROM acquisitions a
                 JOIN publications p ON p.publication_id = a.publication_id
                 JOIN files f ON f.file_id = a.file_id
                WHERE p.legacy_document_id = ?
                ORDER BY a.rowid""",
            (document_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def providers_for_run(self, run_id: str) -> list[str]:
        """Every provider that took part in *run_id*, from the recorded ledger.

        Read from ``source_records``, ``attempts`` and ``failures`` rather than from
        each document's ``discovered_via``: that field answers who *discovered* the
        work, which silently excluded Europe PMC — a cross-check provider that
        discovers nothing by design, however much it contributed. Participation is
        also independent of success, so a provider that was asked and knew nothing
        still appears.

        One query per table regardless of corpus size; the previous metadata scan
        both truncated at 200 documents and paid a JSON parse for each one.
        """
        sources: set[str] = set()
        for statement in (
            """SELECT DISTINCT s.source FROM source_records s
               JOIN run_documents rd ON rd.document_id = s.document_id
               WHERE rd.run_id = ? AND s.source IS NOT NULL""",
            "SELECT DISTINCT source FROM attempts WHERE run_id = ? AND source IS NOT NULL",
            "SELECT DISTINCT source FROM failures WHERE run_id = ? AND source IS NOT NULL",
        ):
            sources.update(row[0] for row in self.connection.execute(statement, (run_id,)))
        return sorted(sources)

    def failure_groups(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Failure counts grouped by category, newest occurrence first.

        A read-only projection for the operator UI's "retry failed" view. The
        grouping is descriptive; deciding what may be retried stays in
        :meth:`reset_documents_for_retry`.
        """
        sql = """SELECT category,
                        COUNT(*)                      AS occurrences,
                        COUNT(DISTINCT document_id)   AS documents,
                        MAX(ts)                       AS last_seen,
                        SUM(retryable)                AS retryable_occurrences
                   FROM failures
                  WHERE document_id IS NOT NULL"""
        params: list[Any] = []
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " GROUP BY category ORDER BY occurrences DESC, category"
        rows = self.connection.execute(sql, params).fetchall()
        return [
            {
                "category": row["category"],
                "occurrences": row["occurrences"],
                "documents": row["documents"],
                "last_seen": row["last_seen"],
                "retryable_occurrences": row["retryable_occurrences"] or 0,
            }
            for row in rows
        ]

    def documents_by_failure_category(
        self, category: str, *, run_id: str | None = None, limit: int = 500
    ) -> list[str]:
        """Documents whose most recent recorded failure is *category*."""
        sql = """SELECT DISTINCT f.document_id AS document_id
                   FROM failures f
                   JOIN documents d ON d.document_id = f.document_id
                  WHERE f.category = ?
                    AND f.document_id IS NOT NULL
                    AND d.status IN (?, ?, ?)"""
        params: list[Any] = [
            category,
            DocumentStatus.FAILED_RETRYABLE.value,
            DocumentStatus.FAILED_PERMANENT.value,
            DocumentStatus.SKIPPED.value,
        ]
        if run_id is not None:
            sql += " AND f.run_id = ?"
            params.append(run_id)
        sql += " ORDER BY f.document_id LIMIT ?"
        params.append(limit)
        return [row["document_id"] for row in self.connection.execute(sql, params).fetchall()]

    def failed_document_ids(self, run_id: str | None = None) -> list[str]:
        """Every document currently in a failed or skipped state."""
        sql = """SELECT d.document_id AS document_id FROM documents d"""
        params: list[Any] = []
        if run_id is not None:
            sql += " JOIN run_documents rd ON rd.document_id = d.document_id AND rd.run_id = ?"
            params.append(run_id)
        sql += " WHERE d.status IN (?, ?, ?) ORDER BY d.document_id"
        params.extend(
            (
                DocumentStatus.FAILED_RETRYABLE.value,
                DocumentStatus.FAILED_PERMANENT.value,
                DocumentStatus.SKIPPED.value,
            )
        )
        return [row["document_id"] for row in self.connection.execute(sql, params).fetchall()]

    def retry_count_for_run(self, run_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS n FROM attempts WHERE run_id = ? AND attempt_no > 1",
            (run_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    # -------------------------------------------------------- corpus browsing

    def query_documents(
        self,
        *,
        search: str | None = None,
        oa_status: str | None = None,
        has_pdf: bool | None = None,
        has_xml: bool | None = None,
        year: int | None = None,
        journal: str | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """Filtered, paginated corpus listing. Returns ``(rows, total_matching)``.

        A read-only projection for browsing. Artifact presence is derived from the
        ``artifacts`` table — the same rows the CLI and ``verify`` consult — so the UI
        can never disagree with them about what exists.
        """
        where: list[str] = []
        params: list[Any] = []

        if search:
            needle = f"%{search.strip().lower()}%"
            where.append(
                "(LOWER(COALESCE(d.title, '')) LIKE ?"
                " OR LOWER(COALESCE(d.doi, '')) LIKE ?"
                " OR LOWER(COALESCE(d.journal, '')) LIKE ?"
                " OR LOWER(d.document_id) LIKE ?)"
            )
            params.extend([needle] * 4)
        if oa_status:
            where.append("LOWER(COALESCE(json_extract(d.metadata_json, '$.oa_status'), '')) = ?")
            params.append(oa_status.strip().lower())
        if year is not None:
            where.append("d.publication_year = ?")
            params.append(year)
        if journal:
            where.append("LOWER(COALESCE(d.journal, '')) LIKE ?")
            params.append(f"%{journal.strip().lower()}%")
        if status:
            where.append("d.status = ?")
            params.append(status)
        for kind, wanted in (("pdf", has_pdf), ("xml", has_xml)):
            if wanted is None:
                continue
            clause = (
                "EXISTS (SELECT 1 FROM artifacts a "
                "WHERE a.document_id = d.document_id AND a.kind = ?)"
            )
            where.append(clause if wanted else f"NOT {clause}")
            params.append(kind)

        clause = f" WHERE {' AND '.join(where)}" if where else ""
        total = self.connection.execute(
            f"SELECT COUNT(*) AS n FROM documents d{clause}", params
        ).fetchone()["n"]

        rows = self.connection.execute(
            f"""SELECT d.document_id, d.doi, d.title, d.publication_year, d.journal,
                       d.status, d.created_at, d.updated_at, d.attempts,
                       json_extract(d.metadata_json, '$.oa_status')  AS oa_status,
                       json_extract(d.metadata_json, '$.authors')    AS authors_json,
                       (SELECT COUNT(*) FROM artifacts a
                         WHERE a.document_id = d.document_id AND a.kind = 'pdf') AS has_pdf,
                       (SELECT COUNT(*) FROM artifacts a
                         WHERE a.document_id = d.document_id AND a.kind = 'xml') AS has_xml,
                       (SELECT COALESCE(SUM(a.size_bytes), 0) FROM artifacts a
                         WHERE a.document_id = d.document_id) AS size_bytes
                  FROM documents d{clause}
                 ORDER BY d.updated_at DESC, d.document_id
                 LIMIT ? OFFSET ?""",
            [*params, max(1, limit), max(0, offset)],
        ).fetchall()

        documents = [
            {
                "document_id": row["document_id"],
                "doi": row["doi"],
                "title": row["title"],
                "publication_year": row["publication_year"],
                "journal": row["journal"],
                "status": row["status"],
                "oa_status": row["oa_status"],
                "authors": parse_json(row["authors_json"], []) or [],
                "has_pdf": bool(row["has_pdf"]),
                "has_xml": bool(row["has_xml"]),
                "size_bytes": row["size_bytes"] or 0,
                "first_seen": row["created_at"],
                "last_updated": row["updated_at"],
                "attempts": row["attempts"],
            }
            for row in rows
        ]
        return documents, int(total)

    def corpus_facets(self) -> dict[str, list[Any]]:
        """Distinct filter values actually present in the corpus."""
        oa = [
            row["value"]
            for row in self.connection.execute(
                "SELECT DISTINCT json_extract(metadata_json, '$.oa_status') AS value "
                "FROM documents WHERE value IS NOT NULL AND value != '' ORDER BY value"
            ).fetchall()
        ]
        years = [
            row["publication_year"]
            for row in self.connection.execute(
                "SELECT DISTINCT publication_year FROM documents "
                "WHERE publication_year IS NOT NULL ORDER BY publication_year DESC"
            ).fetchall()
        ]
        journals = [
            row["journal"]
            for row in self.connection.execute(
                "SELECT journal, COUNT(*) AS n FROM documents "
                "WHERE journal IS NOT NULL AND journal != '' "
                "GROUP BY journal ORDER BY n DESC, journal LIMIT 200"
            ).fetchall()
        ]
        statuses = sorted(self.status_counts().keys())
        return {"oa_status": oa, "year": years, "journal": journals, "status": statuses}

    # ------------------------------------------------- verification history

    def record_verification(
        self, report: dict[str, Any], *, deep: bool
    ) -> None:
        """Persist a verification result so the operator can see the last outcome."""
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO verifications
                       (checked_at, deep, ok, problem_count, orphan_count, report_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    report.get("checked_at") or utc_now_iso(),
                    int(deep),
                    int(bool(report.get("ok"))),
                    int(report.get("problem_count") or 0),
                    len(report.get("orphans") or []),
                    to_json(report),
                ),
            )

    def list_verifications(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM verifications ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {
                "id": row["id"],
                "checked_at": row["checked_at"],
                "deep": bool(row["deep"]),
                "ok": bool(row["ok"]),
                "problem_count": row["problem_count"],
                "orphan_count": row["orphan_count"],
                "report": parse_json(row["report_json"], {}),
            }
            for row in rows
        ]

    def latest_verification(self) -> dict[str, Any] | None:
        results = self.list_verifications(limit=1)
        return results[0] if results else None

    # ------------------------------------------------------------ reconciliation

    def stale_claims(self) -> list[str]:
        """Documents left claimed / mid-acquisition by a terminated process."""
        rows = self.connection.execute(
            "SELECT document_id FROM documents "
            "WHERE (claimed_by IS NOT NULL OR status IN (?, ?)) ORDER BY document_id",
            (DocumentStatus.ACQUIRING.value, DocumentStatus.VALIDATING.value),
        ).fetchall()
        return [row["document_id"] for row in rows]

    def requeue_stale(self, document_ids: Sequence[str]) -> int:
        changed = 0
        with self.transaction() as conn:
            for document_id in document_ids:
                cursor = conn.execute(
                    """UPDATE documents
                          SET status = ?, claimed_by = NULL, claimed_at = NULL, updated_at = ?
                        WHERE document_id = ?
                          AND status IN (?, ?)""",
                    (
                        DocumentStatus.QUEUED.value,
                        utc_now_iso(),
                        document_id,
                        DocumentStatus.ACQUIRING.value,
                        DocumentStatus.VALIDATING.value,
                    ),
                )
                changed += cursor.rowcount
            conn.execute(
                "UPDATE documents SET claimed_by = NULL, claimed_at = NULL "
                "WHERE claimed_by IS NOT NULL AND status NOT IN (?, ?)",
                (DocumentStatus.ACQUIRING.value, DocumentStatus.VALIDATING.value),
            )
        return changed


PENDING_ORDER: tuple[str, ...] = (
    DocumentStatus.DISCOVERED.value,
    DocumentStatus.NORMALIZED.value,
    DocumentStatus.QUEUED.value,
    DocumentStatus.FAILED_RETRYABLE.value,
)


# ---------------------------------------------------------------------- mapping


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=RunStatus(row["status"]),
        query=parse_json(row["query_json"], {}),
        config=parse_json(row["config_json"], {}),
        discovery_cursor=row["discovery_cursor"],
        discovery_complete=bool(row["discovery_complete"]),
        discovery_pages=row["discovery_pages"],
        discovery_seen=row["discovery_seen"],
        dry_run=bool(row["dry_run"]),
        record_limit=row["record_limit"],
        suspend_reason=row["suspend_reason"],
        suspend_details=parse_json(row["suspend_details_json"], None),
        stats=parse_json(row["stats_json"], None),
        search_provenance=parse_json(_optional(row, "search_provenance_json"), None),
    )


def _optional(row: sqlite3.Row, column: str) -> Any:
    """Read a column that older databases may not have yet."""
    try:
        return row[column]
    except (IndexError, KeyError):  # pragma: no cover - upgraded on open
        return None


def _row_to_artifact(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        kind=ArtifactKind(row["kind"]),
        filename=row["filename"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        retrieved_at=row["retrieved_at"],
        source=Source(row["source"]),
        original_url=row["original_url"],
        resolved_url=row["resolved_url"],
        http_status=row["http_status"],
        content_type=row["content_type"],
    )


def _document_metadata(document: LogicalDocument) -> dict[str, Any]:
    """The bibliographic/derived part of a document persisted as JSON."""
    return {
        "doi": document.doi,
        "title": document.title,
        "authors": list(document.authors),
        "publication_year": document.publication_year,
        "journal": document.journal,
        "abstract": document.abstract,
        "is_oa": document.is_oa,
        "oa_status": document.oa_status,
        "oa_status_source": document.oa_status_source,
        "domain_tags": list(document.domain_tags),
        "topics": list(document.topics),
        "identifiers": dict(document.identifiers),
        "candidates": [candidate.to_dict() for candidate in document.candidates],
        "discovered_via": list(document.discovered_via),
        "cross_checks": [check.to_dict() for check in document.cross_checks],
    }


#: Fields where an existing non-empty value wins over a later provider's value.
#: MASTER_SPEC section 44: precedence must be deterministic and must not silently
#: overwrite evidence. First non-empty value wins; every provider's own view is kept
#: verbatim in ``source_records`` regardless.
_FIRST_WINS = (
    "title",
    "publication_year",
    "journal",
    "abstract",
    "is_oa",
    "oa_status",
    "oa_status_source",
    "doi",
)


def _merge_metadata(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key in _FIRST_WINS:
        current = merged.get(key)
        if current in (None, "", []) and incoming.get(key) not in (None, "", []):
            merged[key] = incoming[key]

    for key in ("authors", "domain_tags", "discovered_via"):
        combined = list(merged.get(key) or [])
        for value in incoming.get(key) or []:
            if value not in combined:
                combined.append(value)
        merged[key] = combined

    identifiers = dict(merged.get("identifiers") or {})
    for key, value in (incoming.get("identifiers") or {}).items():
        identifiers.setdefault(key, value)
    merged["identifiers"] = identifiers

    candidates = list(merged.get("candidates") or [])
    seen = {(c.get("url"), c.get("kind")) for c in candidates}
    for candidate in incoming.get("candidates") or []:
        key = (candidate.get("url"), candidate.get("kind"))
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)
    merged["candidates"] = candidates

    topics = list(merged.get("topics") or [])
    topic_ids = {t.get("id") for t in topics}
    for topic in incoming.get("topics") or []:
        if topic.get("id") not in topic_ids:
            topic_ids.add(topic.get("id"))
            topics.append(topic)
    merged["topics"] = topics

    cross_checks = list(merged.get("cross_checks") or [])
    for check in incoming.get("cross_checks") or []:
        if check not in cross_checks:
            cross_checks.append(check)
    merged["cross_checks"] = cross_checks
    return merged


# --------------------------------------------------------- Evidence Ledger helpers


_MERGE_FIELDS = (
    *_FIRST_WINS,
    "authors",
    "domain_tags",
    "discovered_via",
    "identifiers",
    "candidates",
    "topics",
    "cross_checks",
)


def _new_evidence_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _stable_evidence_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(to_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def _ledger_ids(document_id: str) -> tuple[str, str]:
    return f"work_{document_id}", f"publication_{document_id}"


def _ensure_evidence_identities(
    conn: sqlite3.Connection,
    document_id: str,
    *,
    doi: str | None = None,
    created_at: str | None = None,
) -> tuple[str, str]:
    """Create the compatible V1 one-work/one-publication mapping when absent."""
    document = conn.execute(
        "SELECT doi, created_at FROM documents WHERE document_id = ?", (document_id,)
    ).fetchone()
    if document is None:
        raise StateError(f"unknown document: {document_id}")
    effective_doi = doi if doi is not None else document["doi"]
    effective_created_at = created_at or document["created_at"] or utc_now_iso()
    work_id, publication_id = _ledger_ids(document_id)
    conn.execute(
        """INSERT OR IGNORE INTO works (work_id, legacy_document_id, created_at)
           VALUES (?, ?, ?)""",
        (work_id, document_id, effective_created_at),
    )
    conn.execute(
        """INSERT OR IGNORE INTO publications
               (publication_id, work_id, legacy_document_id, doi, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (publication_id, work_id, document_id, effective_doi, effective_created_at),
    )
    if effective_doi is not None:
        conn.execute(
            "UPDATE publications SET doi = COALESCE(doi, ?) WHERE publication_id = ?",
            (effective_doi, publication_id),
        )
    return work_id, publication_id


def _insert_source_observation(
    conn: sqlite3.Connection,
    publication_id: str,
    source_record: SourceRecord,
    *,
    run_id: str | None,
    request_id: str | None,
    origin: str,
) -> str:
    observation_id = _new_evidence_id("observation")
    raw_json = to_json(source_record.raw_payload) if source_record.raw_payload is not None else None
    raw_sha256 = (
        hashlib.sha256(raw_json.encode("utf-8")).hexdigest() if raw_json is not None else None
    )
    quality = "RAW_AND_NORMALIZED" if raw_json is not None else "NORMALIZED_ONLY"
    conn.execute(
        """INSERT INTO source_observations
               (observation_id, publication_id, run_id, request_id, provider, source_id,
                observed_at, normalized_json, raw_json, raw_sha256,
                evidence_quality, origin)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            observation_id,
            publication_id,
            run_id,
            request_id,
            source_record.source.value,
            source_record.source_id,
            source_record.fetched_at,
            to_json(source_record.to_dict()),
            raw_json,
            raw_sha256,
            quality,
            origin,
        ),
    )
    return observation_id


def _merge_policy_for_field(field_name: str) -> str:
    if field_name in _FIRST_WINS:
        return "first_non_empty"
    if field_name in ("authors", "domain_tags", "discovered_via"):
        return "ordered_unique_union"
    if field_name == "identifiers":
        return "first_value_per_identifier"
    if field_name == "candidates":
        return "unique_url_and_kind_union"
    if field_name == "topics":
        return "unique_topic_id_union"
    if field_name == "cross_checks":
        return "unique_record_union"
    return "explicit"


def _empty_evidence_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _previous_chosen_observation(
    conn: sqlite3.Connection, publication_id: str, field_name: str
) -> str | None:
    row = conn.execute(
        """SELECT chosen_observation_id FROM merge_decisions
            WHERE publication_id = ? AND field_name = ?
              AND chosen_observation_id IS NOT NULL
            ORDER BY rowid DESC LIMIT 1""",
        (publication_id, field_name),
    ).fetchone()
    return row["chosen_observation_id"] if row else None


def _record_merge_decisions(
    conn: sqlite3.Connection,
    *,
    publication_id: str,
    run_id: str | None,
    existing: dict[str, Any],
    incoming: dict[str, Any],
    chosen: dict[str, Any],
    candidate_observation_id: str | None,
    origin: str,
    policy_overrides: dict[str, str] | None = None,
) -> None:
    """Explain every field-level result of the deterministic canonical merge."""
    for field_name in _MERGE_FIELDS:
        existing_value = existing.get(field_name)
        incoming_value = incoming.get(field_name)
        chosen_value = chosen.get(field_name)
        previous_observation = _previous_chosen_observation(
            conn, publication_id, field_name
        )

        if _empty_evidence_value(existing_value) and _empty_evidence_value(incoming_value):
            decision = "NO_VALUE"
            chosen_observation_id = previous_observation
        elif _empty_evidence_value(existing_value) and chosen_value == incoming_value:
            decision = "SELECTED"
            chosen_observation_id = candidate_observation_id
        elif chosen_value == existing_value and incoming_value == existing_value:
            decision = "CONFIRMED"
            chosen_observation_id = previous_observation or candidate_observation_id
        elif chosen_value == existing_value:
            decision = "RETAINED"
            chosen_observation_id = previous_observation
        elif chosen_value == incoming_value:
            decision = "SELECTED"
            chosen_observation_id = candidate_observation_id
        else:
            decision = "COMBINED"
            chosen_observation_id = None

        conn.execute(
            """INSERT INTO merge_decisions
                   (decision_id, publication_id, run_id, field_name, decision, policy,
                    candidate_observation_id, chosen_observation_id,
                    existing_value_json, incoming_value_json, chosen_value_json,
                    decided_at, origin)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _new_evidence_id("decision"),
                publication_id,
                run_id,
                field_name,
                decision,
                (policy_overrides or {}).get(
                    field_name, _merge_policy_for_field(field_name)
                ),
                candidate_observation_id,
                chosen_observation_id,
                to_json(existing_value),
                to_json(incoming_value),
                to_json(chosen_value),
                utc_now_iso(),
                origin,
            ),
        )


def _insert_acquisition(
    conn: sqlite3.Connection,
    *,
    publication_id: str,
    run_id: str | None,
    artifact: ArtifactRecord,
    origin: str,
    evidence_quality: str,
) -> str:
    file_id = f"sha256_{artifact.sha256.lower()}"
    conn.execute(
        """INSERT OR IGNORE INTO files
               (file_id, sha256, size_bytes, kind, first_seen_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            file_id,
            artifact.sha256.lower(),
            artifact.size_bytes,
            artifact.kind.value,
            artifact.retrieved_at,
        ),
    )
    stored_file = conn.execute(
        "SELECT sha256, size_bytes, kind FROM files WHERE file_id = ?", (file_id,)
    ).fetchone()
    if (
        stored_file is None
        or stored_file["sha256"] != artifact.sha256.lower()
        or stored_file["size_bytes"] != artifact.size_bytes
        or stored_file["kind"] != artifact.kind.value
    ):
        raise StateError(
            "file identity collision or inconsistent artifact metadata for "
            f"{artifact.sha256.lower()}"
        )
    identity = {
        "publication_id": publication_id,
        "run_id": run_id,
        "file_id": file_id,
        "provider": artifact.source.value,
        "artifact_kind": artifact.kind.value,
        "retrieved_at": artifact.retrieved_at,
        "resolved_url": artifact.resolved_url,
    }
    acquisition_id = _stable_evidence_id("acquisition", identity)
    conn.execute(
        """INSERT OR IGNORE INTO acquisitions
               (acquisition_id, publication_id, file_id, run_id, provider,
                artifact_kind, observed_filename, original_url, resolved_url,
                retrieved_at, http_status, content_type, evidence_quality, origin)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            acquisition_id,
            publication_id,
            file_id,
            run_id,
            artifact.source.value,
            artifact.kind.value,
            artifact.filename,
            artifact.original_url,
            artifact.resolved_url,
            artifact.retrieved_at,
            artifact.http_status,
            artifact.content_type,
            evidence_quality,
            origin,
        ),
    )
    return acquisition_id


def _backfill_evidence_ledger(conn: sqlite3.Connection) -> None:
    """Migrate the latest legacy snapshot without fabricating missing history."""
    documents = conn.execute(
        "SELECT document_id, doi, metadata_json, created_at FROM documents ORDER BY document_id"
    ).fetchall()
    for document in documents:
        document_id = document["document_id"]
        _, publication_id = _ensure_evidence_identities(
            conn,
            document_id,
            doi=document["doi"],
            created_at=document["created_at"],
        )

        source_rows = conn.execute(
            """SELECT id, source, source_id, fetched_at, record_json
                 FROM source_records WHERE document_id = ? ORDER BY id""",
            (document_id,),
        ).fetchall()
        for source_row in source_rows:
            normalized = parse_json(source_row["record_json"], {})
            observation_id = f"legacy_source_record_{source_row['id']}"
            conn.execute(
                """INSERT OR IGNORE INTO source_observations
                       (observation_id, publication_id, run_id, request_id, provider, source_id,
                        observed_at, normalized_json, raw_json, raw_sha256,
                        evidence_quality, origin)
                   VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, NULL, NULL,
                           'LEGACY_CURRENT_SNAPSHOT', 'legacy_v3_backfill')""",
                (
                    observation_id,
                    publication_id,
                    source_row["source"],
                    source_row["source_id"],
                    source_row["fetched_at"],
                    to_json(normalized),
                ),
            )

        metadata = parse_json(document["metadata_json"], {})
        for field_name in _MERGE_FIELDS:
            decision_id = _stable_evidence_id(
                "legacy_merge", [publication_id, field_name]
            )
            conn.execute(
                """INSERT OR IGNORE INTO merge_decisions
                       (decision_id, publication_id, run_id, field_name, decision, policy,
                        candidate_observation_id, chosen_observation_id,
                        existing_value_json, incoming_value_json, chosen_value_json,
                        decided_at, origin)
                   VALUES (?, ?, NULL, ?, 'MIGRATED_CURRENT_VALUE',
                           'legacy_current_snapshot', NULL, NULL, 'null', 'null', ?, ?,
                           'legacy_v3_backfill')""",
                (
                    decision_id,
                    publication_id,
                    field_name,
                    to_json(metadata.get(field_name)),
                    document["created_at"],
                ),
            )

        artifact_rows = conn.execute(
            "SELECT * FROM artifacts WHERE document_id = ? ORDER BY kind", (document_id,)
        ).fetchall()
        for artifact_row in artifact_rows:
            _insert_acquisition(
                conn,
                publication_id=publication_id,
                run_id=None,
                artifact=_row_to_artifact(artifact_row),
                origin="legacy_v3_backfill",
                evidence_quality="LEGACY_CURRENT_SNAPSHOT",
            )
