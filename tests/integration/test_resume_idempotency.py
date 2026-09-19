"""Integration: resume, idempotency, reconciliation and budget suspension.

Covers AC-003 (idempotency), AC-004 (resume), AC-019 (reconciliation) and
AC-022 (production auth + budget suspension).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from harvester.identity import document_id_for_doi, sha256_file
from harvester.models import DocumentStatus, RunStatus
from harvester.state import StateStore
from mocks import Behavior, MockProviders, make_pdf_bytes, openalex_work


def build_providers(count: int = 4, page_size: int = 2) -> MockProviders:
    return MockProviders(
        works=[openalex_work(i) for i in range(1, count + 1)],
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, count + 1)},
        page_size=page_size,
    )


def corpus_snapshot(root: Path) -> dict[str, str]:
    return {path.name: sha256_file(path) for path in sorted(root.glob("*.pdf"))}


# ================================================================== AC-003


def test_ac003_rerunning_a_completed_harvest_creates_no_duplicate_artifacts(
    config, store, harvester_factory, query
):
    """AC-003: second run downloads nothing and produces the identical corpus."""
    providers = build_providers(3)
    first = harvester_factory(providers).harvest(query)
    root = Path(config.storage_root)
    before = corpus_snapshot(root)
    downloads_after_first = sum(
        providers.count(f"file:/W{2000000 + i}.pdf") for i in range(1, 4)
    )

    second = harvester_factory(providers).harvest(query)
    after = corpus_snapshot(root)
    downloads_after_second = sum(
        providers.count(f"file:/W{2000000 + i}.pdf") for i in range(1, 4)
    )

    assert first.stats.completed == 3
    assert second.stats.records_discovered == 3
    assert second.stats.duplicates == 3          # all already known
    assert second.stats.already_complete == 3    # nothing re-acquired
    assert second.stats.downloaded == 0
    assert downloads_after_second == downloads_after_first
    assert before == after                       # byte-identical corpus
    assert len(list(root.glob("*.pdf"))) == 3
    assert len(store.all_document_ids()) == 3


def test_idempotent_rerun_keeps_the_sidecar_stable(config, store, harvester_factory, query):
    providers = build_providers(1)
    harvester_factory(providers).harvest(query)
    document_id = document_id_for_doi("10.1234/mock.0001")
    path = Path(config.storage_root) / f"{document_id}.json"
    first = json.loads(path.read_text("utf-8"))

    harvester_factory(providers).harvest(query)
    second = json.loads(path.read_text("utf-8"))

    for key in ("document_id", "doi", "title", "artifacts", "abstract", "domain_tags"):
        assert first[key] == second[key]


# ================================================================== AC-004


def test_ac004_run_interrupted_during_acquisition_resumes_without_losing_work(
    config, store, harvester_factory, query
):
    """AC-004: interrupt mid-acquisition, resume, keep completed work, finish the rest."""
    providers = build_providers(4)

    # Fail the third and fourth downloads hard, simulating the process dying part-way.
    providers.script_route(
        "file:/W2000003.pdf", [Behavior(raise_exc=lambda: httpx.ConnectError("killed"))] * 5
    )
    providers.script_route(
        "file:/W2000004.pdf", [Behavior(raise_exc=lambda: httpx.ConnectError("killed"))] * 5
    )

    first = harvester_factory(providers).harvest(query)
    assert first.stats.completed == 2
    root = Path(config.storage_root)
    completed_before = corpus_snapshot(root)
    assert len(completed_before) == 2

    # The transport recovers; resuming must finish the remaining two only.
    healthy = build_providers(4)
    resumed = harvester_factory(healthy).resume(first.run_id)

    assert resumed.status is RunStatus.COMPLETED
    assert resumed.stats.already_complete == 2      # untouched
    assert resumed.stats.completed == 2             # newly finished
    assert len(list(root.glob("*.pdf"))) == 4
    # Previously completed artifacts are byte-identical.
    after = corpus_snapshot(root)
    for name, digest in completed_before.items():
        assert after[name] == digest
    # No document was downloaded twice.
    assert healthy.count("file:/W2000001.pdf") == 0
    assert healthy.count("file:/W2000002.pdf") == 0


def test_ac004_discovery_cursor_survives_interruption(config, store, harvester_factory, query):
    """Pagination state is checkpointed after each committed page."""
    providers = build_providers(6, page_size=2)
    # Fail the third discovery page with a permanent error to stop discovery early.
    providers.script_route(
        "openalex.works",
        [Behavior(), Behavior(), Behavior(status=400, json={"error": "bad request"})],
    )
    first = harvester_factory(providers).harvest(query)
    assert first.status is RunStatus.FAILED

    run = store.get_run(first.run_id)
    assert run.discovery_cursor is not None      # checkpointed mid-stream
    assert run.discovery_complete is False
    assert run.discovery_seen == 4               # two committed pages

    healthy = build_providers(6, page_size=2)
    resumed = harvester_factory(healthy).resume(first.run_id)

    assert resumed.status is RunStatus.COMPLETED
    assert len(store.all_document_ids()) == 6
    # Discovery restarted from the checkpoint: one further page covered records 5-6,
    # rather than three pages replaying the whole result set from the beginning.
    assert healthy.count("openalex.works") == 1


def test_resume_of_an_unknown_run_is_a_configuration_error(config, store, harvester_factory):
    from harvester.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="unknown run"):
        harvester_factory(build_providers(1)).resume("run-does-not-exist")


def test_resume_after_completion_is_safe_and_changes_nothing(
    config, store, harvester_factory, query
):
    providers = build_providers(2)
    first = harvester_factory(providers).harvest(query)
    before = corpus_snapshot(Path(config.storage_root))

    resumed = harvester_factory(providers).resume(first.run_id)
    assert resumed.status is RunStatus.COMPLETED
    assert resumed.stats.downloaded == 0
    assert corpus_snapshot(Path(config.storage_root)) == before


# ================================================================== AC-019


def test_ac019_missing_artifact_file_is_detected_and_re_acquired(
    config, store, harvester_factory, query
):
    """AC-019 / MASTER_SPEC section 48: state says COMPLETED but the file is gone."""
    providers = build_providers(1)
    harvester_factory(providers).harvest(query)
    document_id = document_id_for_doi("10.1234/mock.0001")
    pdf_path = Path(config.storage_root) / f"{document_id}.pdf"
    assert pdf_path.exists()

    pdf_path.unlink()  # the filesystem and the database now disagree

    healthy = build_providers(1)
    second = harvester_factory(healthy).harvest(query)

    assert second.stats.already_complete == 0
    assert second.stats.completed == 1
    assert pdf_path.exists()
    assert store.get_document_status(document_id) is DocumentStatus.COMPLETED


def test_ac019_orphan_file_written_before_the_state_update_is_adopted(
    config, store, harvester_factory, query
):
    """The process died between the atomic rename and the database write."""
    providers = build_providers(1)
    harvester_factory(providers).harvest(query)
    document_id = document_id_for_doi("10.1234/mock.0001")

    # Simulate the lost state write: drop the artifact row and demote the document,
    # leaving a perfectly good file behind.
    store.delete_artifact(document_id, __import__("harvester.models", fromlist=["ArtifactKind"]).ArtifactKind.PDF)
    store.set_status(document_id, DocumentStatus.QUEUED)
    pdf_path = Path(config.storage_root) / f"{document_id}.pdf"
    digest_before = sha256_file(pdf_path)

    healthy = build_providers(1)
    result = harvester_factory(healthy).harvest(query)

    assert result.stats.reconciled >= 1
    assert healthy.count("file:/W2000001.pdf") == 0   # adopted, not re-downloaded
    assert sha256_file(pdf_path) == digest_before
    assert store.get_artifacts(document_id)["pdf"].sha256 == digest_before
    assert store.get_document_status(document_id) is DocumentStatus.COMPLETED


def test_ac019_unvalidatable_orphan_is_discarded_not_adopted(
    config, store, harvester_factory, query
):
    """An orphan is re-validated; a corrupt one is never adopted as a success."""
    providers = build_providers(1)
    harvester_factory(providers).harvest(query)
    document_id = document_id_for_doi("10.1234/mock.0001")
    from harvester.models import ArtifactKind

    store.delete_artifact(document_id, ArtifactKind.PDF)
    store.set_status(document_id, DocumentStatus.QUEUED)
    pdf_path = Path(config.storage_root) / f"{document_id}.pdf"
    pdf_path.write_bytes(b"<html>not a pdf at all</html>")

    healthy = build_providers(1)
    result = harvester_factory(healthy).harvest(query)

    assert result.stats.completed == 1
    assert healthy.count("file:/W2000001.pdf") == 1   # re-downloaded properly
    assert pdf_path.read_bytes().startswith(b"%PDF-")


def test_stale_part_files_are_swept_on_resume(config, store, harvester_factory, query):
    """MASTER_SPEC section 17: stale temporary files are detectable and recoverable."""
    import os
    import time

    providers = build_providers(1)
    result = harvester_factory(providers).harvest(query)

    root = Path(config.storage_root)
    stale = root / "doi_something.pdf.abcdef.part"
    stale.write_bytes(b"half a download")
    old = time.time() - 3600
    os.utime(stale, (old, old))

    harvester_factory(build_providers(1)).resume(result.run_id)
    assert not stale.exists()


def test_a_fresh_part_file_is_left_alone(config, store, harvester_factory, query):
    """A concurrent process's in-flight download must not be deleted underneath it."""
    providers = build_providers(1)
    result = harvester_factory(providers).harvest(query)

    in_flight = Path(config.storage_root) / "doi_other.pdf.fedcba.part"
    in_flight.write_bytes(b"a download happening right now")

    harvester_factory(build_providers(1)).resume(result.run_id)
    assert in_flight.exists()


def test_documents_left_acquiring_by_a_dead_process_are_requeued(
    config, store, harvester_factory, query
):
    providers = build_providers(2)
    result = harvester_factory(providers).harvest(query, dry_run=True)
    document_id = store.all_document_ids()[0]
    store.claim_document(document_id, "dead-worker")
    assert store.get_document_status(document_id) is DocumentStatus.ACQUIRING

    resumed = harvester_factory(build_providers(2)).resume(result.run_id)
    assert resumed.stats.reconciled >= 1
    # The dry run stays dry on resume, but the abandoned claim is released.
    assert store.get_document_status(document_id) is DocumentStatus.QUEUED

    # A subsequent real harvest then completes it normally.
    final = harvester_factory(build_providers(2)).harvest(query)
    assert final.stats.completed == 2
    assert store.get_document_status(document_id) is DocumentStatus.COMPLETED


# ================================================================== AC-022


def test_ac022_daily_budget_exhaustion_checkpoints_and_suspends_cleanly(
    config, store, harvester_factory, query
):
    """AC-022: exhaustion checkpoints state, records the reason and stays resumable."""
    providers = build_providers(6, page_size=2)
    # Serve one page normally, then report the daily credit budget as spent.
    providers.script_route(
        "openalex.works",
        [
            Behavior(),
            Behavior(
                status=429,
                json={"error": "You have exceeded your daily credit allowance"},
                headers={
                    "X-RateLimit-Limit": "100000",
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "3600",
                },
            ),
        ],
    )
    result = harvester_factory(providers).harvest(query)

    assert result.status is RunStatus.SUSPENDED
    assert "budget" in (result.stats.suspend_reason or "").lower()
    assert result.stats.suspend_details["provider"] == "openalex"
    assert result.stats.suspend_details["reset_in_seconds"] == 3600
    assert result.stats.suspend_details["limit"] == 100000

    run = store.get_run(result.run_id)
    assert run.status is RunStatus.SUSPENDED
    assert run.suspend_reason
    assert run.discovery_cursor is not None       # checkpointed
    assert run.discovery_complete is False

    # No uncontrolled retry loop: the provider was contacted exactly twice.
    assert providers.count("openalex.works") == 2


def test_ac022_suspended_run_resumes_without_repeating_completed_work(
    config, store, harvester_factory, query
):
    providers = build_providers(6, page_size=2)
    providers.script_route(
        "openalex.works",
        [
            Behavior(),
            Behavior(
                status=429,
                json={"error": "daily credit limit exceeded"},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "60"},
            ),
        ],
    )
    suspended = harvester_factory(providers).harvest(query)
    assert suspended.status is RunStatus.SUSPENDED
    completed_before = corpus_snapshot(Path(config.storage_root))

    healthy = build_providers(6, page_size=2)
    resumed = harvester_factory(healthy).resume(suspended.run_id)

    assert resumed.status is RunStatus.COMPLETED
    assert len(store.all_document_ids()) == 6
    after = corpus_snapshot(Path(config.storage_root))
    for name, digest in completed_before.items():
        assert after[name] == digest, "already-completed work was redone or changed"
    for index in range(1, len(completed_before) + 1):
        assert healthy.count(f"file:/W{2000000 + index}.pdf") == 0


def test_ac022_local_credit_ceiling_suspends_the_run_the_same_way(
    config, store, harvester_factory, query
):
    """SPEC_PATCH section 2: an optional local safety ceiling behaves identically."""
    config.openalex.daily_credit_ceiling = 15  # one list call costs 10 credits
    providers = build_providers(8, page_size=2)
    result = harvester_factory(providers).harvest(query)

    assert result.status is RunStatus.SUSPENDED
    assert result.stats.suspend_details["provider"] == "openalex"
    assert "ceiling" in result.stats.suspend_reason.lower()
    run = store.get_run(result.run_id)
    assert run.status is RunStatus.SUSPENDED
    assert run.discovery_complete is False


def test_ac022_suspension_during_acquisition_preserves_completed_documents(
    config, store, harvester_factory, query
):
    providers = build_providers(4, page_size=10)
    providers.script_route(
        "file:/W2000003.pdf",
        [
            Behavior(
                status=429,
                json={"error": "daily quota exhausted"},
                headers={"X-RateLimit-Remaining": "0"},
            )
        ],
    )
    result = harvester_factory(providers).harvest(query)

    assert result.status is RunStatus.SUSPENDED
    root = Path(config.storage_root)
    # Everything already completed stayed completed and validated.
    for path in root.glob("*.pdf"):
        assert path.stat().st_size > 0
    assert list(root.glob("*.part")) == []
    # Documents that did not finish are resumable, not falsely marked complete.
    statuses = store.status_counts(result.run_id)
    assert statuses.get("COMPLETED", 0) >= 2
    assert "FAILED_PERMANENT" not in statuses


def test_suspension_is_visible_across_a_process_restart(
    config, store, harvester_factory, query
):
    providers = build_providers(4, page_size=2)
    providers.script_route(
        "openalex.works",
        [
            Behavior(),
            Behavior(
                status=409,
                json={"error": "daily credit allowance exhausted"},
                headers={"X-RateLimit-Reset": "120"},
            ),
        ],
    )
    result = harvester_factory(providers).harvest(query)
    assert result.status is RunStatus.SUSPENDED

    with StateStore(config.state_db) as reopened:
        run = reopened.get_run(result.run_id)
        assert run.status is RunStatus.SUSPENDED
        assert run.suspend_details["reset_in_seconds"] == 120
