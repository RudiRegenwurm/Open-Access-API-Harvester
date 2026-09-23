# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""Portable Evidence Ledger export and restore (ADR 0001).

The format is deliberately plain JSON and contains no mutable V1 projection tables.
Any JSON implementation can inspect it without importing OA-Harvester. Restore is
strict: identities and facts are preserved byte-for-byte at the value level, and an
import never merges into a non-empty ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .errors import StateError, StorageError
from .state import SCHEMA_VERSION, StateStore
from .util import to_json, to_pretty_json, utc_now_iso


EVIDENCE_EXPORT_FORMAT = "oa-harvester-evidence-ledger"
EVIDENCE_EXPORT_VERSION = 1

_JSON_COLUMNS: dict[str, dict[str, type[Any] | None]] = {
    "provider_requests": {"request_json": dict},
    "provider_outcomes": {"details_json": dict},
    "source_observations": {"normalized_json": dict, "raw_json": dict},
    "merge_decisions": {
        "existing_value_json": None,
        "incoming_value_json": None,
        "chosen_value_json": None,
    },
}

# Dependency order is also restore order. Explicit columns are a data-contract and a
# SQL-injection boundary; neither table nor column names ever come from an export.
EVIDENCE_TABLES: dict[str, tuple[str, ...]] = {
    "works": ("work_id", "legacy_document_id", "created_at"),
    "publications": (
        "publication_id",
        "work_id",
        "legacy_document_id",
        "doi",
        "created_at",
    ),
    "provider_requests": (
        "request_id",
        "run_id",
        "provider",
        "operation",
        "requested_at",
        "request_json",
    ),
    "provider_outcomes": (
        "outcome_id",
        "request_id",
        "status",
        "completed_at",
        "http_status",
        "result_count",
        "error_category",
        "details_json",
    ),
    "source_observations": (
        "observation_id",
        "publication_id",
        "run_id",
        "request_id",
        "provider",
        "source_id",
        "observed_at",
        "normalized_json",
        "raw_json",
        "raw_sha256",
        "evidence_quality",
        "origin",
    ),
    "merge_decisions": (
        "decision_id",
        "publication_id",
        "run_id",
        "field_name",
        "decision",
        "policy",
        "candidate_observation_id",
        "chosen_observation_id",
        "existing_value_json",
        "incoming_value_json",
        "chosen_value_json",
        "decided_at",
        "origin",
    ),
    "files": ("file_id", "sha256", "size_bytes", "kind", "first_seen_at"),
    "acquisitions": (
        "acquisition_id",
        "publication_id",
        "file_id",
        "run_id",
        "provider",
        "artifact_kind",
        "observed_filename",
        "original_url",
        "resolved_url",
        "retrieved_at",
        "http_status",
        "content_type",
        "evidence_quality",
        "origin",
    ),
}


def build_evidence_export(store: StateStore) -> dict[str, Any]:
    """Build a deterministic, independently readable ledger bundle."""
    tables: dict[str, list[dict[str, Any]]] = {}
    with store.transaction(immediate=False) as conn:
        for table, columns in EVIDENCE_TABLES.items():
            selected = ", ".join(columns)
            order_by = columns[0]
            rows = conn.execute(
                f"SELECT {selected} FROM {table} ORDER BY {order_by}"
            ).fetchall()
            tables[table] = [{column: row[column] for column in columns} for row in rows]

    content = {
        "format": EVIDENCE_EXPORT_FORMAT,
        "format_version": EVIDENCE_EXPORT_VERSION,
        "tables": tables,
    }
    digest = hashlib.sha256(to_json(content).encode("utf-8")).hexdigest()
    return {
        "format": EVIDENCE_EXPORT_FORMAT,
        "format_version": EVIDENCE_EXPORT_VERSION,
        "exported_at": utc_now_iso(),
        "source_schema_version": SCHEMA_VERSION,
        "row_counts": {table: len(rows) for table, rows in tables.items()},
        "content_sha256": digest,
        "tables": tables,
    }


def write_evidence_export(store: StateStore, destination: Path) -> dict[str, Any]:
    """Atomically write an export and return its manifest fields."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    bundle = build_evidence_export(store)
    handle, temp_name = tempfile.mkstemp(
        prefix=f"{destination.name}.", suffix=".part", dir=str(destination.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(to_pretty_json(bundle))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, destination)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort cleanup
            pass
        raise StorageError(f"could not write evidence export {destination}: {exc}") from exc
    return {
        "path": str(destination),
        "content_sha256": bundle["content_sha256"],
        "row_counts": bundle["row_counts"],
    }


def read_evidence_export(source: Path) -> dict[str, Any]:
    try:
        raw = Path(source).read_text(encoding="utf-8")
        bundle = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageError(f"could not read evidence export {source}: {exc}") from exc
    if not isinstance(bundle, dict):
        raise StateError("evidence export root must be a JSON object")
    return bundle


def restore_evidence_export(store: StateStore, bundle: dict[str, Any]) -> dict[str, Any]:
    """Restore a validated bundle into an empty Evidence Ledger transaction."""
    tables = _validate_bundle(bundle)
    with store.transaction() as conn:
        nonempty = [
            table
            for table in EVIDENCE_TABLES
            if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        ]
        if nonempty:
            raise StateError(
                "evidence restore requires an empty ledger; non-empty tables: "
                + ", ".join(nonempty)
            )

        try:
            for table, columns in EVIDENCE_TABLES.items():
                placeholders = ", ".join("?" for _ in columns)
                statement = (
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
                )
                conn.executemany(
                    statement,
                    ([row[column] for column in columns] for row in tables[table]),
                )
        except sqlite3.IntegrityError as exc:
            raise StateError(f"evidence restore violates ledger integrity: {exc}") from exc

        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            first = violations[0]
            raise StateError(
                "evidence restore contains broken references: "
                f"table={first[0]} rowid={first[1]} parent={first[2]}"
            )

    return {
        "content_sha256": bundle["content_sha256"],
        "row_counts": dict(bundle["row_counts"]),
    }


def _validate_bundle(bundle: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    if bundle.get("format") != EVIDENCE_EXPORT_FORMAT:
        raise StateError("unsupported evidence export format")
    if bundle.get("format_version") != EVIDENCE_EXPORT_VERSION:
        raise StateError(
            f"unsupported evidence export version {bundle.get('format_version')!r}"
        )

    tables = bundle.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(EVIDENCE_TABLES):
        raise StateError("evidence export has missing or additional tables")

    counts = bundle.get("row_counts")
    if not isinstance(counts, dict) or set(counts) != set(EVIDENCE_TABLES):
        raise StateError("evidence export row-count manifest is invalid")

    validated: dict[str, list[dict[str, Any]]] = {}
    for table, columns in EVIDENCE_TABLES.items():
        rows = tables.get(table)
        if not isinstance(rows, list):
            raise StateError(f"evidence table {table} must be a JSON array")
        expected_columns = set(columns)
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != expected_columns:
                raise StateError(
                    f"evidence table {table} row {index} has an invalid column set"
                )
        identity_column = columns[0]
        identities = [row[identity_column] for row in rows]
        if any(not isinstance(identity, str) or not identity for identity in identities):
            raise StateError(
                f"evidence table {table} has an invalid {identity_column} identity"
            )
        if identities != sorted(identities):
            raise StateError(f"evidence table {table} rows are not canonically ordered")
        if counts.get(table) != len(rows):
            raise StateError(f"evidence table {table} does not match its row count")
        validated[table] = rows

    content = {
        "format": EVIDENCE_EXPORT_FORMAT,
        "format_version": EVIDENCE_EXPORT_VERSION,
        "tables": validated,
    }
    actual_digest = hashlib.sha256(to_json(content).encode("utf-8")).hexdigest()
    if bundle.get("content_sha256") != actual_digest:
        raise StateError("evidence export content SHA-256 does not match")

    for table, columns in _JSON_COLUMNS.items():
        for index, row in enumerate(validated[table]):
            for column, expected_type in columns.items():
                encoded = row[column]
                if encoded is None and table == "source_observations" and column == "raw_json":
                    continue
                if not isinstance(encoded, str):
                    raise StateError(
                        f"evidence table {table} row {index} column {column} is not JSON text"
                    )
                try:
                    decoded = json.loads(encoded)
                except json.JSONDecodeError as exc:
                    raise StateError(
                        f"evidence table {table} row {index} column {column} is invalid JSON"
                    ) from exc
                if expected_type is not None and not isinstance(decoded, expected_type):
                    raise StateError(
                        f"evidence table {table} row {index} column {column} "
                        f"must contain a JSON {expected_type.__name__}"
                    )

    for index, observation in enumerate(validated["source_observations"]):
        raw_json = observation["raw_json"]
        expected_raw_sha = observation["raw_sha256"]
        if raw_json is None:
            if expected_raw_sha is not None:
                raise StateError(
                    f"source observation row {index} has a raw SHA-256 without raw JSON"
                )
            continue
        actual_raw_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        if expected_raw_sha != actual_raw_sha:
            raise StateError(
                f"source observation row {index} raw JSON SHA-256 does not match"
            )
    return validated
