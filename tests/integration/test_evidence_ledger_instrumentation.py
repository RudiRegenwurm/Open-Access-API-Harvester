"""Every executed provider operation is one ledger request with one terminal outcome.

The gap this covers was found in a real roll-out smoke run: OpenAlex discovery was the
only instrumented operation, so a run that demonstrably talked to Europe PMC, Unpaywall
and two file hosts left a single ``provider_requests`` row behind. Cross-check, resolve
and every attempted full-text candidate are logical provider operations too, and the
ledger has to be able to say so.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from harvester.evidence import (
    build_evidence_export,
    read_evidence_export,
    restore_evidence_export,
    write_evidence_export,
)
from harvester.identity import document_id_for_doi
from harvester.state import StateStore
from mocks import (
    FILES_BASE,
    Behavior,
    MockProviders,
    epmc_result,
    make_html_bytes,
    make_pdf_bytes,
    openalex_work,
    unpaywall_record,
)

DOI = "10.1234/mock.0001"


def _signature(store, run_id: str) -> list[tuple[str, str, str]]:
    """(provider, operation, outcome status) in append order."""
    return [
        (row["provider"], row["operation"], (row["outcome"] or {}).get("status"))
        for row in store.provider_requests_for_run(run_id)
    ]


def _by_operation(store, run_id: str, operation: str) -> list[dict]:
    return [
        row for row in store.provider_requests_for_run(run_id) if row["operation"] == operation
    ]


def test_every_executed_provider_operation_is_recorded_once(store, harvester_factory, query):
    """The full fallback path: cross-check, a rejected PDF, resolve, PDF, XML."""
    providers = MockProviders(
        works=[openalex_work(1)],
        files={
            # The location OpenAlex offers is not a PDF, so the run has to fall through
            # to Unpaywall — the sequence the smoke run actually took.
            "/W2000001.pdf": make_html_bytes(),
            "/unpaywall.pdf": make_pdf_bytes(),
        },
        epmc_by_doi={DOI: epmc_result(doi=DOI)},
        unpaywall_by_doi={
            DOI: unpaywall_record(doi=DOI, pdf_url=f"{FILES_BASE}/unpaywall.pdf")
        },
    )
    result = harvester_factory(providers).harvest(query)

    assert _signature(store, result.run_id) == [
        ("openalex", "discover", "HIT"),
        ("europe_pmc", "cross_check", "HIT"),
        ("openalex", "acquire.pdf", "ERROR"),
        ("unpaywall", "resolve", "HIT"),
        ("unpaywall", "acquire.pdf", "HIT"),
        ("europe_pmc", "acquire.xml", "HIT"),
    ]
    # Every request is terminal: an outcome-less request means "the process died here",
    # and nothing in a completed run may claim that.
    assert all(row["outcome"] for row in store.provider_requests_for_run(result.run_id))


def test_successful_xml_candidate_is_one_request_and_one_hit(store, harvester_factory, query):
    providers = MockProviders(
        works=[openalex_work(1)],
        files={"/W2000001.pdf": make_pdf_bytes()},
        epmc_by_doi={DOI: epmc_result(doi=DOI)},
    )
    result = harvester_factory(providers).harvest(query)

    xml_requests = _by_operation(store, result.run_id, "acquire.xml")
    assert len(xml_requests) == 1
    request = xml_requests[0]
    assert request["provider"] == "europe_pmc"
    assert request["request"]["url"].endswith("/PMC1000001/fullTextXML")
    assert request["request"]["artifact_kind"] == "xml"
    assert request["outcome"]["status"] == "HIT"
    assert request["outcome"]["result_count"] == 1
    assert request["outcome"]["details"]["http_attempts"] == 1

    # The outcome and the artifact tell the same story about the same bytes.
    document_id = document_id_for_doi(DOI)
    xml_artifact = store.get_artifacts(document_id)["xml"]
    assert request["outcome"]["details"]["sha256"] == xml_artifact.sha256
    assert request["outcome"]["details"]["size_bytes"] == xml_artifact.size_bytes


def test_invalid_pdf_candidate_is_one_request_and_a_categorised_error(
    store, harvester_factory, query
):
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_html_bytes()}
    )
    result = harvester_factory(providers).harvest(query)

    pdf_requests = _by_operation(store, result.run_id, "acquire.pdf")
    assert len(pdf_requests) == 1
    outcome = pdf_requests[0]["outcome"]
    assert outcome["status"] == "ERROR"
    assert outcome["error_category"] == "INVALID_PDF"
    assert outcome["details"]["retryable"] is False
    # A rejected artifact is not retried, so the operation cost exactly one HTTP call.
    assert outcome["details"]["http_attempts"] == 1
    assert pdf_requests[0]["request"]["candidate_index"] == 1
    assert pdf_requests[0]["request"]["candidate_count"] == 1


def test_internal_retries_do_not_create_extra_logical_requests(
    store, harvester_factory, query
):
    providers = MockProviders(works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()})
    providers.script_route(
        "file:/W2000001.pdf", [Behavior(status=503), Behavior(status=503)]
    )
    result = harvester_factory(providers).harvest(query)

    pdf_requests = _by_operation(store, result.run_id, "acquire.pdf")
    assert len(pdf_requests) == 1
    assert pdf_requests[0]["outcome"]["status"] == "HIT"
    # Three transport attempts, one logical provider operation.
    assert pdf_requests[0]["outcome"]["details"]["http_attempts"] == 3
    assert providers.count("file:/W2000001.pdf") == 3
    assert store.retry_count_for_run(result.run_id) == 2


def test_no_hit_answers_are_terminal_for_both_secondary_providers(
    store, harvester_factory, query
):
    """Neither provider knows the work. "Nothing here" is an answer, and it is recorded."""
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_html_bytes()}
    )
    result = harvester_factory(providers).harvest(query)

    signature = _signature(store, result.run_id)
    assert ("europe_pmc", "cross_check", "NO_HIT") in signature
    assert ("unpaywall", "resolve", "NO_HIT") in signature
    cross_check = _by_operation(store, result.run_id, "cross_check")[0]
    assert cross_check["outcome"]["result_count"] == 0
    assert cross_check["outcome"]["details"]["lookup"] == "doi"


def test_europe_pmc_timeout_is_recorded_as_timeout_not_as_a_generic_error(
    store, harvester_factory, query
):
    providers = MockProviders(
        works=[openalex_work(1)],
        files={"/W2000001.pdf": make_pdf_bytes()},
        epmc_by_doi={DOI: epmc_result(doi=DOI)},
    )
    providers.script_route(
        "epmc.search", [Behavior(raise_exc=lambda: httpx.ReadTimeout("slow")) for _ in range(3)]
    )
    result = harvester_factory(providers).harvest(query)

    cross_checks = _by_operation(store, result.run_id, "cross_check")
    assert len(cross_checks) == 1
    assert cross_checks[0]["outcome"]["status"] == "TIMEOUT"
    assert cross_checks[0]["outcome"]["error_category"] == "TIMEOUT"
    assert cross_checks[0]["outcome"]["details"]["retryable"] is True


def test_unpaywall_resolve_failure_is_terminal_and_still_records_the_failure(
    store, harvester_factory, query
):
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_html_bytes()}
    )
    providers.script_route(
        "unpaywall.doi", [Behavior(status=500, json={"error": "boom"}) for _ in range(3)]
    )
    result = harvester_factory(providers).harvest(query)

    resolves = _by_operation(store, result.run_id, "resolve")
    assert len(resolves) == 1
    assert resolves[0]["provider"] == "unpaywall"
    assert resolves[0]["outcome"]["status"] == "ERROR"
    assert resolves[0]["outcome"]["error_category"] == "PROVIDER_ERROR"
    # The pre-existing structured failure record is untouched by the instrumentation.
    assert any(
        failure["operation"] == "resolve" and failure["source"] == "unpaywall"
        for failure in store.failures_for_run(result.run_id)
    )


def test_unexpected_defect_still_closes_the_request_without_leaking_its_message(
    store, harvester_factory, query, monkeypatch
):
    """A defect is not a reason to leave a request open — nor to persist its text.

    An arbitrary exception message is not a vetted secret-safe string (it can carry the
    URL that produced it, credentials and all), so the ledger records the exception type
    and nothing else. The traceback stays in the log where it belongs.
    """
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    harvester = harvester_factory(providers)

    def explode(**_kwargs):
        raise RuntimeError("boom while fetching ?api_key=super-secret-token")

    monkeypatch.setattr(harvester.acquirer, "acquire", explode)
    result = harvester.harvest(query)

    pdf_requests = _by_operation(store, result.run_id, "acquire.pdf")
    assert len(pdf_requests) == 1
    outcome = pdf_requests[0]["outcome"]
    assert outcome["status"] == "ERROR"
    assert outcome["error_category"] == "UNKNOWN_ERROR"
    assert outcome["details"]["message"] == "unexpected RuntimeError"
    assert "super-secret-token" not in json.dumps(outcome)


def test_ledger_holds_no_secret_from_url_header_or_configuration(
    store, harvester_factory, query, config
):
    """AC-017 applied to the new rows: nothing credential-bearing may be persisted."""
    secret_url = f"{FILES_BASE}/W2000001.pdf?api_key=super-secret-token"
    providers = MockProviders(
        works=[openalex_work(1, pdf_url=secret_url)],
        files={"/W2000001.pdf": make_pdf_bytes()},
        epmc_by_doi={DOI: epmc_result(doi=DOI)},
    )
    result = harvester_factory(providers).harvest(query)

    rows = store.provider_requests_for_run(result.run_id)
    serialised = json.dumps([{"r": row["request"], "o": row["outcome"]} for row in rows])
    for secret in (
        "super-secret-token",
        config.openalex.api_key,
        config.contact_email,
    ):
        assert secret
        assert secret not in serialised

    pdf_request = _by_operation(store, result.run_id, "acquire.pdf")[0]["request"]
    assert pdf_request["url"] == f"{FILES_BASE}/W2000001.pdf?api_key=REDACTED"


def test_new_requests_survive_evidence_export_and_restore(
    store, harvester_factory, query, tmp_path: Path
):
    providers = MockProviders(
        works=[openalex_work(1)],
        files={
            "/W2000001.pdf": make_html_bytes(),
            "/unpaywall.pdf": make_pdf_bytes(),
        },
        epmc_by_doi={DOI: epmc_result(doi=DOI)},
        unpaywall_by_doi={
            DOI: unpaywall_record(doi=DOI, pdf_url=f"{FILES_BASE}/unpaywall.pdf")
        },
    )
    harvester_factory(providers).harvest(query)

    export_path = tmp_path / "evidence.json"
    manifest = write_evidence_export(store, export_path)
    expected = build_evidence_export(store)
    assert manifest["row_counts"]["provider_requests"] == 6
    assert manifest["row_counts"]["provider_outcomes"] == 6

    with StateStore(tmp_path / "restored.sqlite3") as destination:
        restore_evidence_export(destination, read_evidence_export(export_path))
        restored = build_evidence_export(destination)

    assert restored["content_sha256"] == expected["content_sha256"]
    # The observations the new instrumentation links keep pointing at their request.
    linked = [
        row
        for row in restored["tables"]["source_observations"]
        if row["provider"] in ("europe_pmc", "unpaywall")
    ]
    assert linked
    assert all(row["request_id"] for row in linked)
