"""Failure injection (MASTER_SPEC section 50 level 3).

Covers AC-005, AC-006, AC-007, AC-008, AC-009, AC-010 and AC-015 at the pipeline level.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from harvester.identity import document_id_for_doi
from harvester.models import DocumentStatus, RunStatus
from mocks import (
    FILES_BASE,
    Behavior,
    MockProviders,
    make_corrupt_pdf_bytes,
    make_html_bytes,
    make_malformed_xml_bytes,
    make_pdf_bytes,
    make_truncated_pdf_bytes,
    openalex_work,
    epmc_result,
    unpaywall_record,
)

DOI = "10.1234/mock.0001"
PDF_ROUTE = "file:/W2000001.pdf"


def one_work_providers(pdf: bytes | None = None, **kwargs) -> MockProviders:
    files = {} if pdf is None else {"/W2000001.pdf": pdf}
    return MockProviders(works=[openalex_work(1)], files=files, **kwargs)


def failures_of(store, run_id, category: str) -> list[dict]:
    return [f for f in store.failures_for_run(run_id) if f["category"] == category]


# --------------------------------------------------------------- bad artifacts


@pytest.mark.parametrize(
    "payload,category",
    [
        (make_html_bytes(), "INVALID_PDF"),
        (make_truncated_pdf_bytes(), "INVALID_PDF"),
        (make_corrupt_pdf_bytes(), "INVALID_PDF"),
        (b"", "DOWNLOAD_ERROR"),
        (b"plain text pretending to be a paper" * 50, "INVALID_PDF"),
    ],
)
def test_bad_payloads_never_become_successful_artifacts(
    config, store, harvester_factory, query, payload, category
):
    """AC-005 / AC-006 / AC-007: rejected downloads leave no artifact and no sidecar."""
    result = harvester_factory(one_work_providers(payload)).harvest(query)

    root = Path(config.storage_root)
    assert result.stats.completed == 0
    assert list(root.glob("*.pdf")) == []
    assert list(root.glob("*.json")) == []
    assert list(root.glob("*.part")) == []
    assert failures_of(store, result.run_id, category)
    assert store.get_document_status(document_id_for_doi(DOI)) in (
        DocumentStatus.FAILED_PERMANENT,
        DocumentStatus.FAILED_RETRYABLE,
    )


def test_html_error_page_is_recorded_with_a_precise_reason(
    config, store, harvester_factory, query
):
    result = harvester_factory(one_work_providers(make_html_bytes())).harvest(query)
    failures = failures_of(store, result.run_id, "INVALID_PDF")
    assert any("HTML" in f["message"] for f in failures)


# --------------------------------------------------------------- HTTP failures


def test_ac008_transient_429_retries_then_succeeds(config, store, harvester_factory, query):
    """AC-008 at pipeline level: throttling is absorbed by the retry policy."""
    providers = one_work_providers(make_pdf_bytes())
    providers.script_route(
        PDF_ROUTE,
        [
            Behavior(status=429, content=b"slow down", headers={"X-RateLimit-Remaining": "50"}),
            Behavior(status=429, content=b"slow down", headers={"X-RateLimit-Remaining": "49"}),
        ],
    )
    result = harvester_factory(providers).harvest(query)
    assert result.stats.completed == 1
    assert providers.count(PDF_ROUTE) == 3


def test_ac009_persistent_404_is_a_structured_permanent_failure(
    config, store, harvester_factory, query
):
    """AC-009: no endless retries; the failure is recorded, not discarded."""
    providers = one_work_providers()  # no file registered -> 404
    result = harvester_factory(providers).harvest(query)

    assert result.stats.failed_permanent == 1
    assert failures_of(store, result.run_id, "NOT_FOUND")
    assert store.get_document_status(document_id_for_doi(DOI)) is DocumentStatus.FAILED_PERMANENT
    assert providers.count(PDF_ROUTE) == 1  # a 404 is not retried


def test_permanent_failures_are_not_retried_by_a_later_run(
    config, store, harvester_factory, query
):
    providers = one_work_providers()
    first = harvester_factory(providers).harvest(query)
    assert first.stats.failed_permanent == 1

    second = harvester_factory(one_work_providers()).harvest(query)
    assert second.stats.attempted == 0  # left alone until retry-failed is invoked


def test_server_errors_are_retried_within_the_configured_budget(
    config, store, harvester_factory, query
):
    providers = one_work_providers(make_pdf_bytes())
    providers.script_route(PDF_ROUTE, [Behavior(status=503, content=b"unavailable")] * 2)
    result = harvester_factory(providers).harvest(query)
    assert result.stats.completed == 1
    assert providers.count(PDF_ROUTE) == 3


def test_exhausted_retry_budget_ends_as_a_recorded_failure(
    config, store, harvester_factory, query
):
    providers = one_work_providers(make_pdf_bytes())
    providers.script_route(PDF_ROUTE, [Behavior(status=503, content=b"unavailable")] * 10)
    result = harvester_factory(providers).harvest(query)

    assert result.stats.completed == 0
    assert providers.count(PDF_ROUTE) == config.retry.max_attempts
    assert failures_of(store, result.run_id, "PROVIDER_ERROR")


def test_timeouts_are_retried_and_then_recorded(config, store, harvester_factory, query):
    providers = one_work_providers(make_pdf_bytes())
    providers.script_route(
        PDF_ROUTE, [Behavior(raise_exc=lambda: httpx.ReadTimeout("timed out"))] * 10
    )
    result = harvester_factory(providers).harvest(query)
    assert result.stats.completed == 0
    assert failures_of(store, result.run_id, "TIMEOUT")


def test_connection_reset_is_retried_then_recorded(config, store, harvester_factory, query):
    providers = one_work_providers(make_pdf_bytes())
    providers.script_route(
        PDF_ROUTE, [Behavior(raise_exc=lambda: httpx.ConnectError("connection reset"))] * 10
    )
    result = harvester_factory(providers).harvest(query)
    assert result.stats.completed == 0
    assert failures_of(store, result.run_id, "NETWORK_ERROR")


def test_malformed_provider_response_is_recorded_not_crashed(
    config, store, harvester_factory, query
):
    providers = one_work_providers(make_pdf_bytes())
    providers.script_route("openalex.works", [Behavior(status=200, content=b"<<<not json>>>")])
    result = harvester_factory(providers).harvest(query)
    assert result.status is RunStatus.FAILED
    assert failures_of(store, result.run_id, "PROVIDER_ERROR")


# ------------------------------------------------------------------- AC-015


def test_ac015_oversized_declared_response_is_rejected_before_transfer(
    config, store, harvester_factory, query
):
    """AC-015: an unacceptable Content-Length is refused up front."""
    config.downloads.max_download_size_bytes = 1000
    providers = MockProviders(
        works=[openalex_work(1)],
        files={"/W2000001.pdf": make_pdf_bytes()},
        file_headers={"/W2000001.pdf": {"content-length": "99999999"}},
    )
    result = harvester_factory(providers).harvest(query)

    assert result.stats.completed == 0
    assert failures_of(store, result.run_id, "SIZE_LIMIT_EXCEEDED")
    assert list(Path(config.storage_root).glob("*.part")) == []


def test_ac015_limit_is_enforced_during_streaming_too(config, store, harvester_factory, query):
    """A lying or absent Content-Length cannot get past the streaming check."""
    config.downloads.max_download_size_bytes = 2000
    big = make_pdf_bytes(padding=50_000)
    providers = MockProviders(
        works=[openalex_work(1)],
        files={"/W2000001.pdf": big},
        file_headers={"/W2000001.pdf": {"content-length": "10"}},
    )
    result = harvester_factory(providers).harvest(query)

    assert result.stats.completed == 0
    assert failures_of(store, result.run_id, "SIZE_LIMIT_EXCEEDED")
    assert list(Path(config.storage_root).glob("*.pdf")) == []
    assert list(Path(config.storage_root).glob("*.part")) == []


# ------------------------------------------------------------------- AC-010


def test_ac010_full_fallback_chain_openalex_then_epmc_then_unpaywall(
    config, store, harvester_factory, query
):
    """AC-010: each unusable location is recorded, then the next source is tried."""
    providers = MockProviders(
        works=[openalex_work(1)],
        files={
            "/W2000001.pdf": make_html_bytes(),      # OpenAlex location: unusable
            "/epmc.pdf": make_truncated_pdf_bytes(),  # Europe PMC location: truncated
            "/unpaywall.pdf": make_pdf_bytes(),       # Unpaywall fallback: good
        },
        epmc_by_doi={DOI: epmc_result(doi=DOI, pdf_url=f"{FILES_BASE}/epmc.pdf")},
        unpaywall_by_doi={DOI: unpaywall_record(doi=DOI, pdf_url=f"{FILES_BASE}/unpaywall.pdf")},
    )
    result = harvester_factory(providers).harvest(query)

    assert result.stats.completed == 1
    assert providers.count("file:/W2000001.pdf") == 1
    assert providers.count("file:/epmc.pdf") == 1
    assert providers.count("file:/unpaywall.pdf") == 1
    # Every rejected candidate is recorded; nothing is silently discarded.
    assert len(failures_of(store, result.run_id, "INVALID_PDF")) == 2


def test_malformed_xml_does_not_fail_the_pdf_harvest(config, store, harvester_factory, query):
    """XML is optional: a broken XML artifact is recorded but the document completes."""
    providers = MockProviders(
        works=[openalex_work(1)],
        files={"/W2000001.pdf": make_pdf_bytes()},
        epmc_by_doi={DOI: epmc_result(doi=DOI)},
    )
    providers.script_route(
        "epmc.fulltextxml", [Behavior(status=200, content=make_malformed_xml_bytes())]
    )
    result = harvester_factory(providers).harvest(query)

    assert result.stats.completed == 1
    assert result.stats.xml_count == 0
    assert failures_of(store, result.run_id, "INVALID_XML")
    assert list(Path(config.storage_root).glob("*.xml")) == []


def test_europe_pmc_outage_does_not_fail_the_document(config, store, harvester_factory, query):
    providers = MockProviders(
        works=[openalex_work(1)],
        files={"/W2000001.pdf": make_pdf_bytes()},
        epmc_by_doi={DOI: epmc_result(doi=DOI)},
    )
    providers.script_route("epmc.search", [Behavior(status=500, content=b"boom")] * 10)
    result = harvester_factory(providers).harvest(query)

    assert result.stats.completed == 1
    assert failures_of(store, result.run_id, "PROVIDER_ERROR")


def test_unpaywall_outage_leaves_a_structured_failure(config, store, harvester_factory, query):
    providers = MockProviders(works=[openalex_work(1)], files={})
    providers.script_route("unpaywall.doi", [Behavior(status=500, content=b"boom")] * 10)
    result = harvester_factory(providers).harvest(query)

    assert result.stats.completed == 0
    categories = {f["category"] for f in store.failures_for_run(result.run_id)}
    assert "PROVIDER_ERROR" in categories
    assert "NOT_FOUND" in categories


# ------------------------------------------------------------- run integrity


def test_every_failure_is_present_in_the_machine_readable_report(
    config, store, harvester_factory, query
):
    """MASTER_SPEC section 3.2: failures are data, and they reach the operator."""
    import json

    providers = one_work_providers(make_html_bytes())
    result = harvester_factory(providers).harvest(query)
    report = json.loads(result.report_path.read_text("utf-8"))
    assert report["failures"], "the run report must carry the structured failures"
    assert any(f["category"] == "INVALID_PDF" for f in report["failures"])
    assert report["failed_permanent"] + report["failed_retryable"] == 1


def test_the_same_failure_is_not_recorded_twice_confusingly(
    config, store, harvester_factory, query
):
    """MASTER_SPEC section 30: the same failure must not be duplicated confusingly."""
    providers = one_work_providers(make_html_bytes())
    result = harvester_factory(providers).harvest(query)

    failures = store.failures_for_run(result.run_id)
    messages = [f["message"] for f in failures]
    assert len(messages) == len(set(messages)), f"duplicated failure text: {messages}"

    # One candidate-level record with the precise cause, one document-level summary.
    operations = sorted(f["operation"] for f in failures)
    assert operations == ["acquire", "acquire.pdf"]
    summary = next(f for f in failures if f["operation"] == "acquire")
    assert "candidate location(s)" in summary["message"]
    assert summary["details"]["candidates_tried"] == 1


def test_candidate_count_in_the_summary_reflects_the_fallback_chain(
    config, store, harvester_factory, query
):
    providers = MockProviders(
        works=[openalex_work(1)],
        files={"/W2000001.pdf": make_html_bytes(), "/epmc.pdf": make_html_bytes()},
        epmc_by_doi={DOI: epmc_result(doi=DOI, pdf_url=f"{FILES_BASE}/epmc.pdf")},
    )
    result = harvester_factory(providers).harvest(query)
    summary = next(
        f for f in store.failures_for_run(result.run_id) if f["operation"] == "acquire"
    )
    assert summary["details"]["candidates_tried"] == 2


def test_retry_failed_re_queues_and_a_later_run_succeeds(
    config, store, harvester_factory, query
):
    providers = one_work_providers()  # 404
    first = harvester_factory(providers).harvest(query)
    document_id = document_id_for_doi(DOI)
    assert store.get_document_status(document_id) is DocumentStatus.FAILED_PERMANENT

    assert store.reset_documents_for_retry([document_id]) == 1
    healthy = one_work_providers(make_pdf_bytes())
    second = harvester_factory(healthy).resume(first.run_id)
    assert second.stats.completed == 1
    assert store.get_document_status(document_id) is DocumentStatus.COMPLETED
