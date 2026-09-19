"""OpenAlex discovery request/outcome and observation vertical slice."""

from __future__ import annotations

import httpx

from harvester.identity import document_id_for_doi
from harvester.models import RunStatus
from mocks import (
    FILES_BASE,
    Behavior,
    MockProviders,
    epmc_result,
    make_pdf_bytes,
    openalex_work,
    unpaywall_record,
)


def test_openalex_hit_links_request_outcome_and_native_observation(
    store, harvester_factory, query
):
    providers = MockProviders(works=[openalex_work(1)])
    result = harvester_factory(providers).harvest(query, dry_run=True)

    requests = store.provider_requests_for_run(result.run_id)
    assert len(requests) == 1
    assert requests[0]["provider"] == "openalex"
    assert requests[0]["request"]["cursor"] == "*"
    assert requests[0]["outcome"]["status"] == "HIT"
    assert requests[0]["outcome"]["result_count"] == 1
    assert requests[0]["outcome"]["details"]["persisted_count"] == 1

    document_id = document_id_for_doi("10.1234/mock.0001")
    observations = store.source_observations_for_document(document_id)
    assert len(observations) == 1
    assert observations[0]["request_id"] == requests[0]["request_id"]
    assert observations[0]["run_id"] == result.run_id
    assert observations[0]["raw"]["id"].endswith("W2000001")


def test_openalex_empty_page_is_no_hit(store, harvester_factory, query):
    result = harvester_factory(MockProviders(works=[])).harvest(query, dry_run=True)
    request = store.provider_requests_for_run(result.run_id)[0]
    assert result.status is RunStatus.COMPLETED
    assert request["outcome"]["status"] == "NO_HIT"
    assert request["outcome"]["result_count"] == 0


def test_openalex_timeout_is_not_collapsed_into_generic_error(
    store, harvester_factory, query
):
    providers = MockProviders(works=[openalex_work(1)])
    providers.script_route(
        "openalex.works",
        [Behavior(raise_exc=lambda: httpx.ReadTimeout("slow")) for _ in range(3)],
    )
    result = harvester_factory(providers).harvest(query, dry_run=True)
    request = store.provider_requests_for_run(result.run_id)[0]
    assert result.status is RunStatus.FAILED
    assert request["outcome"]["status"] == "TIMEOUT"
    assert request["outcome"]["error_category"] == "TIMEOUT"


def test_openalex_provider_failure_is_error(store, harvester_factory, query):
    providers = MockProviders(works=[openalex_work(1)])
    providers.script_route(
        "openalex.works", [Behavior(status=400, json={"error": "bad request"})]
    )
    result = harvester_factory(providers).harvest(query, dry_run=True)
    request = store.provider_requests_for_run(result.run_id)[0]
    assert result.status is RunStatus.FAILED
    assert request["outcome"]["status"] == "ERROR"
    assert request["outcome"]["error_category"] == "HTTP_ERROR"


def test_europe_pmc_native_record_and_conflict_decision_are_linked(
    store, harvester_factory, query
):
    doi = "10.1234/mock.0001"
    providers = MockProviders(
        works=[openalex_work(1)],
        files={"/W2000001.pdf": make_pdf_bytes()},
        epmc_by_doi={doi: epmc_result(doi=doi, title="Conflicting Europe PMC title")},
    )
    harvester_factory(providers).harvest(query)

    document_id = document_id_for_doi(doi)
    observations = store.source_observations_for_document(document_id)
    epmc_observation = next(
        row for row in observations if row["provider"] == "europe_pmc"
    )
    assert epmc_observation["raw"]["title"] == "Conflicting Europe PMC title"
    assert epmc_observation["evidence_quality"] == "RAW_AND_NORMALIZED"

    title_decision = next(
        row
        for row in store.merge_decisions_for_document(document_id)
        if row["field_name"] == "title"
        and row["candidate_observation_id"] == epmc_observation["observation_id"]
    )
    assert title_decision["decision"] == "RETAINED"
    assert title_decision["incoming_value"] == "Conflicting Europe PMC title"
    assert title_decision["chosen_value"] == "Mock Open Access Article 1"


def test_unpaywall_fallback_observation_and_projection_merge_are_linked(
    store, harvester_factory, query
):
    doi = "10.1234/mock.0001"
    providers = MockProviders(
        works=[openalex_work(1)],
        files={
            "/W2000001.pdf": b"<html>not a PDF</html>",
            "/unpaywall.pdf": make_pdf_bytes(),
        },
        unpaywall_by_doi={
            doi: unpaywall_record(
                doi=doi, pdf_url=f"{FILES_BASE}/unpaywall.pdf", oa_status="green"
            )
        },
    )
    harvester_factory(providers).harvest(query)

    document_id = document_id_for_doi(doi)
    observations = store.source_observations_for_document(document_id)
    unpaywall_observation = next(
        row for row in observations if row["provider"] == "unpaywall"
    )
    assert unpaywall_observation["raw"]["oa_status"] == "green"
    decisions = [
        row
        for row in store.merge_decisions_for_document(document_id)
        if row["candidate_observation_id"] == unpaywall_observation["observation_id"]
    ]
    assert decisions
    assert next(row for row in decisions if row["field_name"] == "oa_status")[
        "decision"
    ] == "RETAINED"
    discovered = next(row for row in decisions if row["field_name"] == "discovered_via")
    assert "unpaywall" in discovered["chosen_value"]
