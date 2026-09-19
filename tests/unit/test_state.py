"""Unit tests: persistent state, transitions, claiming and deduplication (AC-002)."""

from __future__ import annotations

from pathlib import Path

import pytest

from harvester.errors import Failure, ErrorCategory, StateError
from harvester.identity import document_id_for_doi
from harvester.models import (
    ArtifactKind,
    ArtifactRecord,
    DocumentStatus,
    LogicalDocument,
    RunStatus,
    Source,
    SourceRecord,
    transition_allowed,
)
from harvester.state import StateStore
from harvester.util import utc_now_iso


def make_document(doi: str, *, title: str = "Title", source: Source = Source.OPENALEX):
    document_id = document_id_for_doi(doi)
    record = SourceRecord(
        source=source,
        source_id=f"{source.value}-{doi}",
        fetched_at=utc_now_iso(),
        doi=doi,
        title=title,
        authors=["Ada Lovelace"],
        publication_year=2021,
        journal="Journal of Mock Studies",
    )
    document = LogicalDocument(
        document_id=document_id,
        doi=doi,
        title=title,
        authors=["Ada Lovelace"],
        publication_year=2021,
        journal="Journal of Mock Studies",
        discovered_via=[source.value],
        source_records=[record],
    )
    return document, record


@pytest.fixture
def state(tmp_path: Path):
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.create_run("run-1", query={"filter": "topics.id:T1"}, config={})
        yield store


# ------------------------------------------------------------------ AC-002


def test_ac002_same_doi_from_multiple_providers_is_one_logical_document(state: StateStore):
    """AC-002: the same DOI discovered from several providers yields one document."""
    doi = "10.1234/shared"
    for source in (Source.OPENALEX, Source.EUROPE_PMC, Source.UNPAYWALL):
        document, record = make_document(doi, source=source)
        state.upsert_discovered_document("run-1", document, record)

    assert state.all_document_ids() == [document_id_for_doi(doi)]
    rows = state.connection.execute(
        "SELECT source FROM source_records WHERE document_id = ? ORDER BY source",
        (document_id_for_doi(doi),),
    ).fetchall()
    assert [row["source"] for row in rows] == ["europe_pmc", "openalex", "unpaywall"]


def test_duplicate_flag_distinguishes_new_from_repeat_discovery(state: StateStore):
    document, record = make_document("10.1234/dup")
    created, linked = state.upsert_discovered_document("run-1", document, record)
    assert (created, linked) == (True, True)

    created2, linked2 = state.upsert_discovered_document("run-1", document, record)
    assert created2 is False
    assert linked2 is False  # already part of this run


def test_document_discovered_in_a_later_run_links_without_duplicating(state: StateStore):
    document, record = make_document("10.1234/again")
    state.upsert_discovered_document("run-1", document, record)
    state.create_run("run-2", query={}, config={})
    created, linked = state.upsert_discovered_document("run-2", document, record)
    assert created is False
    assert linked is True
    assert len(state.all_document_ids()) == 1
    assert state.documents_for_run("run-2") == [document.document_id]


def test_conflicting_doi_binding_is_refused(state: StateStore):
    document, record = make_document("10.1234/one")
    state.upsert_discovered_document("run-1", document, record)
    impostor = LogicalDocument(document_id="doi_forged_0123456789ab", doi="10.1234/one")
    with pytest.raises(StateError, match="already bound"):
        state.upsert_discovered_document("run-1", impostor, record)


def test_metadata_merge_is_first_value_wins_and_never_overwrites(state: StateStore):
    doi = "10.1234/merge"
    first, first_record = make_document(doi, title="Canonical title")
    state.upsert_discovered_document("run-1", first, first_record)

    second, second_record = make_document(doi, title="Different title", source=Source.EUROPE_PMC)
    second.abstract = "Provided later"
    second_record.abstract = "Provided later"
    state.upsert_discovered_document("run-1", second, second_record)

    metadata = state.get_metadata(first.document_id)
    assert metadata["title"] == "Canonical title"   # not overwritten
    assert metadata["abstract"] == "Provided later"  # genuinely missing before
    assert set(metadata["discovered_via"]) == {"openalex", "europe_pmc"}


# ------------------------------------------------------------- state machine


def test_declared_transitions_match_the_specification():
    assert transition_allowed(DocumentStatus.QUEUED, DocumentStatus.ACQUIRING)
    assert transition_allowed(DocumentStatus.ACQUIRING, DocumentStatus.COMPLETED)
    assert transition_allowed(DocumentStatus.ACQUIRING, DocumentStatus.FAILED_RETRYABLE)
    assert transition_allowed(DocumentStatus.FAILED_RETRYABLE, DocumentStatus.QUEUED)
    # Reconciliation may demote a COMPLETED document whose file vanished.
    assert transition_allowed(DocumentStatus.COMPLETED, DocumentStatus.QUEUED)
    # Same-state writes are idempotent.
    assert transition_allowed(DocumentStatus.COMPLETED, DocumentStatus.COMPLETED)


def test_impossible_transitions_are_rejected(state: StateStore):
    document, record = make_document("10.1234/fsm")
    state.upsert_discovered_document("run-1", document, record)
    with pytest.raises(StateError, match="illegal state transition"):
        state.set_status(document.document_id, DocumentStatus.COMPLETED)


def test_transition_on_unknown_document_is_an_error(state: StateStore):
    with pytest.raises(StateError, match="unknown document"):
        state.set_status("doi_missing_0123456789ab", DocumentStatus.QUEUED)


# ------------------------------------------------------------------ claiming


def test_claim_is_exclusive(state: StateStore):
    document, record = make_document("10.1234/claim")
    state.upsert_discovered_document("run-1", document, record)
    assert state.claim_document(document.document_id, "worker-a") is True
    assert state.claim_document(document.document_id, "worker-b") is False
    state.release_claim(document.document_id)
    assert state.claim_document(document.document_id, "worker-b") is False  # already ACQUIRING


def test_claim_moves_the_document_to_acquiring(state: StateStore):
    document, record = make_document("10.1234/claim2")
    state.upsert_discovered_document("run-1", document, record)
    state.claim_document(document.document_id, "worker-a")
    assert state.get_document_status(document.document_id) is DocumentStatus.ACQUIRING


# ------------------------------------------------------ artifacts and failures


def test_artifacts_are_upserted_per_kind(state: StateStore):
    document, record = make_document("10.1234/artifact")
    state.upsert_discovered_document("run-1", document, record)
    artifact = ArtifactRecord(
        kind=ArtifactKind.PDF,
        filename=f"{document.document_id}.pdf",
        sha256="a" * 64,
        size_bytes=1234,
        retrieved_at=utc_now_iso(),
        source=Source.OPENALEX,
        original_url="https://files.invalid/a.pdf",
        resolved_url="https://files.invalid/a.pdf",
        http_status=200,
    )
    state.record_artifact(document.document_id, artifact)
    state.record_artifact(document.document_id, artifact)
    stored = state.get_artifacts(document.document_id)
    assert list(stored) == ["pdf"]
    assert stored["pdf"].sha256 == "a" * 64
    assert state.artifact_counts("run-1")["pdf"] == 1


def test_failures_are_persisted_and_never_discarded(state: StateStore):
    failure = Failure(
        category=ErrorCategory.NOT_FOUND,
        message="artifact gone",
        operation="acquire.pdf",
        source="openalex",
        document_id="doi_x_0123456789ab",
        http_status=404,
    )
    state.record_failure("run-1", failure)
    stored = state.failures_for_run("run-1")
    assert len(stored) == 1
    assert stored[0]["category"] == "NOT_FOUND"
    assert stored[0]["http_status"] == 404


def test_run_lifecycle_and_cursor_checkpoint(state: StateStore):
    state.update_run_cursor("run-1", "cursor-abc", complete=False, pages=1, seen=25)
    run = state.get_run("run-1")
    assert run is not None and run.discovery_cursor == "cursor-abc"
    assert run.discovery_seen == 25
    assert run.status is RunStatus.RUNNING

    state.finish_run(
        "run-1",
        RunStatus.SUSPENDED,
        stats={"completed": 3},
        suspend_reason="budget exhausted",
        suspend_details={"provider": "openalex", "reset_in_seconds": 3600},
    )
    run = state.get_run("run-1")
    assert run is not None
    assert run.status is RunStatus.SUSPENDED
    assert run.suspend_reason == "budget exhausted"
    assert run.suspend_details["provider"] == "openalex"


def test_state_survives_process_restart(tmp_path: Path):
    path = tmp_path / "persist.sqlite3"
    document, record = make_document("10.1234/persist")
    with StateStore(path) as store:
        store.create_run("run-x", query={}, config={})
        store.upsert_discovered_document("run-x", document, record)

    with StateStore(path) as store:
        assert store.all_document_ids() == [document.document_id]
        assert store.get_run("run-x") is not None


def test_requeue_stale_recovers_documents_left_mid_acquisition(state: StateStore):
    document, record = make_document("10.1234/stale")
    state.upsert_discovered_document("run-1", document, record)
    state.claim_document(document.document_id, "dead-worker")

    stale = state.stale_claims()
    assert document.document_id in stale
    assert state.requeue_stale(stale) == 1
    assert state.get_document_status(document.document_id) is DocumentStatus.QUEUED
    assert state.claim_document(document.document_id, "new-worker") is True


def test_retry_reset_moves_failures_back_to_queued(state: StateStore):
    document, record = make_document("10.1234/retry")
    state.upsert_discovered_document("run-1", document, record)
    state.set_status(document.document_id, DocumentStatus.QUEUED)
    state.set_status(document.document_id, DocumentStatus.ACQUIRING)
    state.set_status(document.document_id, DocumentStatus.FAILED_PERMANENT)

    assert state.reset_documents_for_retry([document.document_id]) == 1
    assert state.get_document_status(document.document_id) is DocumentStatus.QUEUED


def test_a_version_two_database_is_upgraded_in_place(tmp_path: Path):
    """The search-provenance column is added to an existing database, not required."""
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    with StateStore(path) as store:
        store.create_run("run-legacy", query={"search": "x"}, config={})

    # Reproduce a v2 database: drop the column that version did not have.
    with sqlite3.connect(str(path)) as conn:
        conn.execute("ALTER TABLE runs DROP COLUMN search_provenance_json")
        conn.execute("UPDATE schema_meta SET value = '2'")
    with sqlite3.connect(str(path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        assert "search_provenance_json" not in columns

    with StateStore(path) as store:
        run = store.get_run("run-legacy")
        assert run is not None
        assert run.search_provenance is None      # nothing invented for an old run
        version = store.connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        assert version == "4"
        store.create_run(
            "run-new", query={"search": "y"}, config={},
            search_provenance={"search_mode": "assisted"},
        )
        assert store.get_run("run-new").search_provenance == {"search_mode": "assisted"}


def test_reopening_a_current_database_does_not_re_add_columns(tmp_path: Path):
    path = tmp_path / "stable.sqlite3"
    for _ in range(3):
        with StateStore(path) as store:
            store.create_run(
                f"run-{_}", query={}, config={}, search_provenance={"search_mode": "conventional"}
            )
    with StateStore(path) as store:
        assert len(store.list_runs(10)) == 3


def test_search_provenance_round_trips(tmp_path: Path):
    provenance = {
        "search_mode": "assisted",
        "research_question": "Can ADHD symptoms remit and recur during adulthood?",
        "generated_query": "ADHD adult remission recurrence",
        "effective_search_query": "ADHD adult remission recurrence longitudinal",
        "query_edited": True,
        "query_advisor": {"provider": "anthropic", "model": "m", "prompt_version": "v1"},
    }
    with StateStore(tmp_path / "provenance.sqlite3") as store:
        store.create_run("run-p", query={}, config={}, search_provenance=provenance)
        assert store.get_run("run-p").search_provenance == provenance


def test_schema_version_mismatch_is_detected(tmp_path: Path):
    path = tmp_path / "versioned.sqlite3"
    with StateStore(path) as store:
        store.connection.execute("UPDATE schema_meta SET value = '99'")
    with pytest.raises(StateError, match="schema version"):
        StateStore(path)


def test_last_activity_reads_both_ledgers(tmp_path: Path):
    """Liveness for a run in flight (UX hardening item 2).

    Either ledger can move on its own — an attempt is recorded for every provider
    request, a document timestamp changes on every state transition — so the answer is
    the later of the two. A run that has done nothing yet says nothing rather than
    inventing a time.
    """
    with StateStore(tmp_path / "activity.sqlite3") as store:
        store.create_run("run-a", query={}, config={})
        assert store.last_activity_at("run-a") is None

        store.record_attempt(
            run_id="run-a", document_id=None, operation="discover", attempt_no=1, ok=True
        )
        first = store.last_activity_at("run-a")
        assert first is not None

        # An attempt against a different run must not count as this one's liveness.
        store.create_run("run-b", query={}, config={})
        assert store.last_activity_at("run-b") is None
