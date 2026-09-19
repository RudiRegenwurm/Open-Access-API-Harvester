"""Evidence Ledger V1: immutable history, migration and portable transfer."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from harvester.evidence import (
    EVIDENCE_EXPORT_FORMAT,
    build_evidence_export,
    read_evidence_export,
    restore_evidence_export,
    write_evidence_export,
)
from harvester.errors import StateError
from harvester.models import ArtifactKind, ArtifactRecord, Source
from harvester.state import StateStore
from harvester.util import to_json

from tests.unit.test_state import make_document


def _artifact(document_id: str, *, digest: str, retrieved_at: str, source: Source):
    return ArtifactRecord(
        kind=ArtifactKind.PDF,
        filename=f"{document_id}.pdf",
        sha256=digest,
        size_bytes=1234,
        retrieved_at=retrieved_at,
        source=source,
        original_url=f"https://files.invalid/{digest[0]}.pdf",
        resolved_url=f"https://cdn.invalid/{digest[0]}.pdf",
        http_status=200,
        content_type="application/pdf",
    )


def test_reobservation_and_conflict_preserve_both_native_records(tmp_path: Path):
    path = tmp_path / "evidence.sqlite3"
    with StateStore(path) as store:
        store.create_run("run-1", query={}, config={})
        first, first_record = make_document("10.1234/history", title="Original title")
        first_record.raw_payload = {"id": "W-history", "title": "Original title"}
        store.upsert_discovered_document("run-1", first, first_record)

        store.create_run("run-2", query={}, config={})
        second, second_record = make_document("10.1234/history", title="Changed title")
        second_record.raw_payload = {"id": "W-history", "title": "Changed title"}
        store.upsert_discovered_document("run-2", second, second_record)

        # The compatibility projection remains current and singular.
        projected = store.source_records_for_document(first.document_id)
        assert len(projected) == 1
        assert projected[0]["record"]["title"] == "Changed title"

        observations = store.source_observations_for_document(first.document_id)
        assert [row["raw"]["title"] for row in observations] == [
            "Original title",
            "Changed title",
        ]
        assert all(row["evidence_quality"] == "RAW_AND_NORMALIZED" for row in observations)
        assert observations[0]["raw_sha256"] != observations[1]["raw_sha256"]

        # Canonical first-non-empty behaviour is unchanged, but now explained.
        assert store.get_metadata(first.document_id)["title"] == "Original title"
        title_decisions = [
            row
            for row in store.merge_decisions_for_document(first.document_id)
            if row["field_name"] == "title"
        ]
        assert [row["decision"] for row in title_decisions] == ["SELECTED", "RETAINED"]
        assert title_decisions[-1]["policy"] == "first_non_empty"
        assert title_decisions[-1]["candidate_observation_id"] == observations[1]["observation_id"]
        assert title_decisions[-1]["chosen_observation_id"] == observations[0]["observation_id"]
        assert title_decisions[-1]["incoming_value"] == "Changed title"
        assert title_decisions[-1]["chosen_value"] == "Original title"

        assert store.connection.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 1
        assert store.connection.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 1


def test_acquisitions_are_append_only_while_artifact_is_latest_projection(tmp_path: Path):
    with StateStore(tmp_path / "acquisitions.sqlite3") as store:
        store.create_run("run-1", query={}, config={})
        document, record = make_document("10.1234/files")
        store.upsert_discovered_document("run-1", document, record)

        first = _artifact(
            document.document_id,
            digest="a" * 64,
            retrieved_at="2026-09-01T00:00:00Z",
            source=Source.OPENALEX,
        )
        second = _artifact(
            document.document_id,
            digest="b" * 64,
            retrieved_at="2026-09-02T00:00:00Z",
            source=Source.UNPAYWALL,
        )
        first_id = store.record_artifact(document.document_id, first, run_id="run-1")
        # Repeating the exact state write is idempotent, not a fabricated acquisition.
        assert store.record_artifact(document.document_id, first, run_id="run-1") == first_id
        store.create_run("run-2", query={}, config={})
        store.record_artifact(document.document_id, second, run_id="run-2")

        assert store.get_artifacts(document.document_id)["pdf"].sha256 == "b" * 64
        acquisitions = store.acquisitions_for_document(document.document_id)
        assert len(acquisitions) == 2
        assert [row["sha256"] for row in acquisitions] == ["a" * 64, "b" * 64]
        assert [row["run_id"] for row in acquisitions] == ["run-1", "run-2"]
        assert store.connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2


def test_dual_write_rolls_back_observation_decisions_and_projection_together(
    tmp_path: Path,
):
    with StateStore(tmp_path / "atomic.sqlite3") as store:
        store.create_run("run-1", query={}, config={})
        document, record = make_document("10.1234/atomic")
        store.upsert_discovered_document("run-1", document, record)
        original_metadata = store.get_metadata(document.document_id)
        original_decision_count = len(
            store.merge_decisions_for_document(document.document_id)
        )

        _, later_record = make_document(
            "10.1234/atomic",
            title="Temporary provider title",
            source=Source.EUROPE_PMC,
        )
        later_record.raw_payload = {"id": "PMC-atomic", "title": later_record.title}

        with pytest.raises(RuntimeError, match="injected after dual write"):
            with store.transaction():
                observation_id = store.add_source_record(
                    document.document_id, later_record, run_id="run-1"
                )
                chosen = dict(original_metadata)
                chosen["abstract"] = "temporary value"
                store.update_metadata(
                    document.document_id,
                    chosen,
                    run_id="run-1",
                    candidate_observation_id=observation_id,
                    candidate_metadata={
                        "title": later_record.title,
                        "abstract": "temporary value",
                    },
                )
                raise RuntimeError("injected after dual write")

        assert store.get_metadata(document.document_id) == original_metadata
        assert len(store.source_observations_for_document(document.document_id)) == 1
        assert len(store.merge_decisions_for_document(document.document_id)) == (
            original_decision_count
        )
        assert [
            row["source"] for row in store.source_records_for_document(document.document_id)
        ] == [Source.OPENALEX.value]


def test_inconsistent_file_identity_rolls_back_without_rewriting_projection(
    tmp_path: Path,
):
    with StateStore(tmp_path / "file-identity.sqlite3") as store:
        store.create_run("run-1", query={}, config={})
        document, record = make_document("10.1234/file-identity")
        store.upsert_discovered_document("run-1", document, record)
        first = _artifact(
            document.document_id,
            digest="e" * 64,
            retrieved_at="2026-09-04T00:00:00Z",
            source=Source.OPENALEX,
        )
        store.record_artifact(document.document_id, first, run_id="run-1")
        inconsistent = ArtifactRecord(
            kind=first.kind,
            filename="inconsistent.pdf",
            sha256=first.sha256,
            size_bytes=first.size_bytes + 1,
            retrieved_at="2026-09-05T00:00:00Z",
            source=Source.UNPAYWALL,
            original_url=first.original_url,
            resolved_url=first.resolved_url,
            http_status=200,
            content_type=first.content_type,
        )

        with pytest.raises(StateError, match="file identity collision"):
            store.record_artifact(document.document_id, inconsistent, run_id="run-1")

        assert len(store.acquisitions_for_document(document.document_id)) == 1
        assert store.get_artifacts(document.document_id)["pdf"].size_bytes == first.size_bytes


def test_schema_three_backfill_is_labelled_and_idempotent(tmp_path: Path):
    path = tmp_path / "legacy-v3.sqlite3"
    with StateStore(path) as store:
        store.create_run("legacy-run", query={}, config={})
        document, record = make_document("10.1234/legacy")
        store.upsert_discovered_document("legacy-run", document, record)
        store.record_artifact(
            document.document_id,
            _artifact(
                document.document_id,
                digest="c" * 64,
                retrieved_at="2026-08-01T00:00:00Z",
                source=Source.OPENALEX,
            ),
        )

    # Reproduce the exact pre-ledger state: legacy projections remain, ledger is empty.
    with sqlite3.connect(path) as conn:
        for table in (
            "acquisitions",
            "files",
            "merge_decisions",
            "source_observations",
            "provider_outcomes",
            "provider_requests",
            "publications",
            "works",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE schema_meta SET value = '3'")

    with StateStore(path) as migrated:
        assert migrated.connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "4"
        observations = migrated.source_observations_for_document(document.document_id)
        assert len(observations) == 1
        assert observations[0]["raw"] is None
        assert observations[0]["evidence_quality"] == "LEGACY_CURRENT_SNAPSHOT"
        decisions = migrated.merge_decisions_for_document(document.document_id)
        assert decisions
        assert {row["decision"] for row in decisions} == {"MIGRATED_CURRENT_VALUE"}
        acquisitions = migrated.acquisitions_for_document(document.document_id)
        assert len(acquisitions) == 1
        assert acquisitions[0]["evidence_quality"] == "LEGACY_CURRENT_SNAPSHOT"
        counts = {
            table: migrated.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("works", "publications", "source_observations", "merge_decisions", "files", "acquisitions")
        }

    with StateStore(path) as reopened:
        assert {
            table: reopened.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in counts
        } == counts


def test_schema_three_migration_failure_rolls_back_every_ledger_row(tmp_path: Path):
    path = tmp_path / "migration-rollback.sqlite3"
    with StateStore(path) as store:
        store.create_run("legacy-run", query={}, config={})
        first_document, first_record = make_document("10.1234/migration-a")
        second_document, second_record = make_document("10.1234/migration-b")
        store.upsert_discovered_document("legacy-run", first_document, first_record)
        store.upsert_discovered_document("legacy-run", second_document, second_record)
        store.record_artifact(
            first_document.document_id,
            _artifact(
                first_document.document_id,
                digest="f" * 64,
                retrieved_at="2026-08-01T00:00:00Z",
                source=Source.OPENALEX,
            ),
        )
        store.record_artifact(
            second_document.document_id,
            _artifact(
                second_document.document_id,
                digest="0" * 64,
                retrieved_at="2026-08-02T00:00:00Z",
                source=Source.OPENALEX,
            ),
        )

    ledger_tables = (
        "acquisitions",
        "files",
        "merge_decisions",
        "source_observations",
        "provider_outcomes",
        "provider_requests",
        "publications",
        "works",
    )
    with sqlite3.connect(path) as conn:
        for table in ledger_tables:
            conn.execute(f"DELETE FROM {table}")
        # A legacy inconsistency that schema 3 could represent: the same claimed
        # digest with different sizes. Migration must reject it atomically.
        conn.execute(
            "UPDATE artifacts SET sha256 = ?, size_bytes = ? WHERE document_id = ?",
            ("f" * 64, 9999, second_document.document_id),
        )
        conn.execute("UPDATE schema_meta SET value = '3'")

    with pytest.raises(StateError, match="file identity collision"):
        StateStore(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "3"
        assert all(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            for table in ledger_tables
        )


def test_plain_json_export_restores_identical_ledger_content(tmp_path: Path):
    source_path = tmp_path / "source.sqlite3"
    export_path = tmp_path / "evidence.json"
    with StateStore(source_path) as source:
        source.create_run("run-export", query={}, config={})
        document, record = make_document("10.1234/export")
        record.raw_payload = {"id": "W-export", "title": record.title}
        request_id = source.begin_provider_request(
            run_id="run-export",
            provider=Source.OPENALEX.value,
            operation="discover",
            request={"cursor": "*", "query": {"topic_id": "T1"}},
        )
        source.upsert_discovered_document(
            "run-export", document, record, provider_request_id=request_id
        )
        source.finish_provider_request(
            request_id, status="HIT", http_status=200, result_count=1
        )
        source.record_artifact(
            document.document_id,
            _artifact(
                document.document_id,
                digest="d" * 64,
                retrieved_at="2026-09-03T00:00:00Z",
                source=Source.OPENALEX,
            ),
            run_id="run-export",
        )
        manifest = write_evidence_export(source, export_path)
        expected = build_evidence_export(source)

    # Independence check: ordinary JSON tooling can inspect every table and raw value.
    plain = json.loads(export_path.read_text(encoding="utf-8"))
    assert plain["format"] == EVIDENCE_EXPORT_FORMAT
    assert plain["tables"]["source_observations"][0]["raw_json"]
    assert manifest["content_sha256"] == plain["content_sha256"]

    with StateStore(tmp_path / "restored.sqlite3") as destination:
        restored = restore_evidence_export(destination, read_evidence_export(export_path))
        actual = build_evidence_export(destination)

    assert restored["content_sha256"] == expected["content_sha256"]
    assert actual["content_sha256"] == expected["content_sha256"]
    assert actual["tables"] == expected["tables"]


def test_restore_rejects_tampering_and_leaves_destination_empty(tmp_path: Path):
    with StateStore(tmp_path / "source.sqlite3") as source:
        bundle = build_evidence_export(source)
    bundle["tables"]["works"].append(
        {"work_id": "forged", "legacy_document_id": "forged", "created_at": "now"}
    )

    with StateStore(tmp_path / "destination.sqlite3") as destination:
        with pytest.raises(StateError, match="row count|SHA-256"):
            restore_evidence_export(destination, bundle)
        assert destination.connection.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 0


def test_restore_rejects_raw_record_whose_internal_digest_is_inconsistent(
    tmp_path: Path,
):
    with StateStore(tmp_path / "source-with-raw.sqlite3") as source:
        source.create_run("run-raw", query={}, config={})
        document, record = make_document("10.1234/raw-integrity")
        record.raw_payload = {"id": "W-raw", "title": record.title}
        source.upsert_discovered_document("run-raw", document, record)
        bundle = build_evidence_export(source)

    bundle["tables"]["source_observations"][0]["raw_json"] = to_json(
        {"id": "W-raw", "title": "modified after export"}
    )
    # Model an attacker or faulty producer that can recompute the outer manifest but
    # forgets/tries to contradict the observation's own immutable raw-record digest.
    content = {
        "format": bundle["format"],
        "format_version": bundle["format_version"],
        "tables": bundle["tables"],
    }
    bundle["content_sha256"] = hashlib.sha256(
        to_json(content).encode("utf-8")
    ).hexdigest()

    with StateStore(tmp_path / "destination-raw.sqlite3") as destination:
        with pytest.raises(StateError, match="raw JSON SHA-256"):
            restore_evidence_export(destination, bundle)
        assert destination.connection.execute(
            "SELECT COUNT(*) FROM source_observations"
        ).fetchone()[0] == 0
