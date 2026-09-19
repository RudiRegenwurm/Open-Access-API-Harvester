"""Unit tests: provider adapters.

Covers AC-021 (active OpenAlex taxonomy), AC-024 (OA-status provenance),
AC-025 (Europe PMC cross-check semantics) and AC-026 (abstract handling).
"""

from __future__ import annotations

import httpx
import pytest

from harvester.config import EuropePmcConfig, OpenAlexConfig, ProviderConfig, RetryConfig, UnpaywallConfig
from harvester.errors import ConfigurationError
from harvester.http import ProviderClient
from harvester.models import ArtifactKind, Source
from harvester.providers.europepmc import EuropePmcAdapter
from harvester.providers.openalex import (
    OpenAlexAdapter,
    OpenAlexQuery,
    normalize_topic_id,
    reconstruct_abstract,
)
from harvester.providers.unpaywall import UnpaywallAdapter
from mocks import (
    EPMC_BASE,
    OPENALEX_BASE,
    UNPAYWALL_BASE,
    MockProviders,
    epmc_result,
    openalex_work,
    unpaywall_record,
)


def client_for(providers: MockProviders) -> ProviderClient:
    return ProviderClient(
        "test",
        ProviderConfig(requests_per_second=1000, concurrency=4),
        RetryConfig(max_attempts=2, backoff_initial_seconds=0, jitter_ratio=0),
        user_agent="test-agent",
        transport=providers.transport,
        sleeper=lambda _s: None,
    )


# ============================================================ OpenAlex: taxonomy


def test_ac021_topic_filters_use_current_semantics():
    """AC-021: discovery uses Topic filters; Concepts do not drive V1 discovery."""
    query = OpenAlexQuery(topic_id="T10159")
    assert "topics.id:T10159" in query.filter_string()
    assert "concept" not in query.filter_string().lower()

    primary = OpenAlexQuery(topic_id="T10159", primary_topic_only=True)
    assert "primary_topic.id:T10159" in primary.filter_string()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("T10159", "T10159"),
        ("t10159", "T10159"),
        ("https://openalex.org/T10159", "T10159"),
        ("https://openalex.org/T10159/", "T10159"),
        ("  T10159  ", "T10159"),
    ],
)
def test_topic_ids_are_normalized(raw, expected):
    assert normalize_topic_id(raw) == expected


@pytest.mark.parametrize("raw", ["C169760540", "T", "TXYZ", "10159", "", "https://openalex.org/"])
def test_deprecated_concept_ids_and_junk_are_refused(raw):
    with pytest.raises(ConfigurationError):
        normalize_topic_id(raw)


def test_filter_string_is_deterministic_and_includes_oa_and_doi_filters():
    query = OpenAlexQuery(topic_id="T1", from_publication_year=2015, to_publication_year=2020)
    first = query.filter_string()
    assert first == query.filter_string()
    assert "is_oa:true" in first
    assert "has_doi:true" in first
    assert "from_publication_date:2015-01-01" in first
    assert "to_publication_date:2020-12-31" in first


def test_query_without_any_filter_is_refused():
    query = OpenAlexQuery(is_oa=False, has_doi=False)
    with pytest.raises(ConfigurationError, match="at least one filter"):
        query.filter_string()


def test_query_round_trips_through_state():
    query = OpenAlexQuery(topic_id="T10159", primary_topic_only=True, publication_year=2022)
    restored = OpenAlexQuery.from_dict(query.to_dict())
    assert restored.filter_string() == query.filter_string()


# =========================================================== OpenAlex: abstracts


def test_ac026_inverted_index_is_reconstructed_deterministically():
    """AC-026: a supplied abstract_inverted_index is rebuilt exactly."""
    index = {"The": [0], "quick": [1], "brown": [2], "fox": [3, 6], "jumps": [4], "over": [5]}
    assert reconstruct_abstract(index) == "The quick brown fox jumps over fox"
    assert reconstruct_abstract(index) == reconstruct_abstract(dict(reversed(list(index.items()))))


@pytest.mark.parametrize("value", [None, {}, [], "text", 42, {"a": "not-a-list"}])
def test_ac026_missing_abstract_data_yields_null_never_invented_text(value):
    """AC-026: no usable data -> abstract is null. Nothing is fabricated."""
    assert reconstruct_abstract(value) is None


def test_abstract_reconstruction_ignores_invalid_positions():
    assert reconstruct_abstract({"a": [0], "b": [-1], "c": ["x"], "d": [2]}) == "a d"


def test_work_without_abstract_index_has_null_abstract():
    providers = MockProviders(works=[openalex_work(1, abstract=False)])
    adapter = OpenAlexAdapter(client_for(providers), OpenAlexConfig(base_url=OPENALEX_BASE))
    document, _ = adapter.normalize_work(providers.works[0])
    assert document.abstract is None


# ======================================================== OpenAlex: normalization


def test_normalized_work_carries_source_derived_hierarchy_tags():
    """SPEC_PATCH section 1: domain -> field -> subfield -> topic, source-supplied only."""
    providers = MockProviders(works=[openalex_work(1)])
    adapter = OpenAlexAdapter(client_for(providers), OpenAlexConfig(base_url=OPENALEX_BASE))
    document, record = adapter.normalize_work(providers.works[0])

    assert document.domain_tags == [
        "Social Sciences",
        "Psychology",
        "Developmental and Educational Psychology",
        "Moral Psychology",
    ]
    assert document.topics[0]["id"] == "T10159"
    assert document.topics[0]["is_primary"] is True
    assert record.source is Source.OPENALEX


def test_ac024_provider_oa_status_and_its_source_are_preserved():
    """AC-024: a supplied oa_status is preserved together with its source."""
    providers = MockProviders(works=[openalex_work(1, oa_status="hybrid")])
    adapter = OpenAlexAdapter(client_for(providers), OpenAlexConfig(base_url=OPENALEX_BASE))
    document, _ = adapter.normalize_work(providers.works[0])
    assert document.oa_status == "hybrid"
    assert document.oa_status_source == "openalex"
    assert document.is_oa is True


def test_ac024_missing_oa_status_stays_missing():
    work = openalex_work(1)
    work["open_access"] = {}
    providers = MockProviders(works=[work])
    adapter = OpenAlexAdapter(client_for(providers), OpenAlexConfig(base_url=OPENALEX_BASE))
    document, _ = adapter.normalize_work(work)
    assert document.oa_status is None
    assert document.oa_status_source is None
    assert document.is_oa is None  # not fabricated as False


def test_doi_is_normalized_and_drives_the_document_id():
    work = openalex_work(1, doi="10.1234/MiXeD.CaSe")
    providers = MockProviders(works=[work])
    adapter = OpenAlexAdapter(client_for(providers), OpenAlexConfig(base_url=OPENALEX_BASE))
    document, _ = adapter.normalize_work(work)
    assert document.doi == "10.1234/mixed.case"
    assert document.document_id.startswith("doi_10_1234_mixed_case_")


def test_work_without_doi_falls_back_to_the_provider_identifier():
    work = openalex_work(1, doi=None)
    work["doi"] = None
    work["ids"].pop("doi", None)
    providers = MockProviders(works=[work])
    adapter = OpenAlexAdapter(client_for(providers), OpenAlexConfig(base_url=OPENALEX_BASE))
    document, _ = adapter.normalize_work(work)
    assert document.doi is None
    assert document.document_id.startswith("openalex_w2000001_")


def test_non_oa_locations_are_not_offered_as_candidates():
    """No access control is worked around: closed locations are simply not used."""
    work = openalex_work(1)
    work["best_oa_location"] = None
    work["primary_location"] = {"is_oa": False, "pdf_url": "https://files.invalid/closed.pdf"}
    work["open_access"] = {"is_oa": False, "oa_status": "closed", "oa_url": None}
    providers = MockProviders(works=[work])
    adapter = OpenAlexAdapter(client_for(providers), OpenAlexConfig(base_url=OPENALEX_BASE))
    document, _ = adapter.normalize_work(work)
    assert document.candidates == []


# ============================================================ OpenAlex: paging


def test_cursor_pagination_walks_every_page():
    works = [openalex_work(i) for i in range(1, 8)]
    providers = MockProviders(works=works, page_size=3)
    adapter = OpenAlexAdapter(
        client_for(providers), OpenAlexConfig(base_url=OPENALEX_BASE, per_page=3)
    )
    query = OpenAlexQuery(topic_id="T10159")
    collected = []
    for page in adapter.iter_discovery(query):
        collected.extend(page.records)
    assert len(collected) == 7
    assert providers.count("openalex.works") == 3


def test_first_request_uses_the_cursor_wildcard_and_carries_the_api_key():
    providers = MockProviders(works=[openalex_work(1)])
    adapter = OpenAlexAdapter(
        client_for(providers), OpenAlexConfig(base_url=OPENALEX_BASE, api_key="secret-key")
    )
    adapter.discover_page(OpenAlexQuery(topic_id="T10159"))
    request = providers.requests[-1]
    assert request.url.params["cursor"] == "*"
    assert request.url.params["api_key"] == "secret-key"
    # SPEC_PATCH section 2: the retired polite-pool parameter is never sent.
    assert "mailto" not in dict(request.url.params)


def test_unusable_records_are_skipped_without_stopping_the_page():
    works = [openalex_work(1), {"no": "id"}, openalex_work(2)]
    providers = MockProviders(works=works)
    adapter = OpenAlexAdapter(client_for(providers), OpenAlexConfig(base_url=OPENALEX_BASE))
    page = adapter.discover_page(OpenAlexQuery(topic_id="T10159"))
    assert len(page.records) == 2
    assert page.raw_count == 3


def test_malformed_discovery_payload_is_a_provider_error():
    from harvester.errors import ProviderError
    from mocks import Behavior

    providers = MockProviders(works=[])
    providers.script_route("openalex.works", [Behavior(status=200, json={"meta": {}})])
    adapter = OpenAlexAdapter(client_for(providers), OpenAlexConfig(base_url=OPENALEX_BASE))
    with pytest.raises(ProviderError, match="no 'results' list"):
        adapter.discover_page(OpenAlexQuery(topic_id="T10159"))


# ========================================================== Europe PMC: AC-025


def epmc_adapter(providers: MockProviders) -> EuropePmcAdapter:
    return EuropePmcAdapter(client_for(providers), EuropePmcConfig(base_url=EPMC_BASE))


def test_ac025_cross_check_records_identifier_matches_and_availability():
    """AC-025: identifier matches, PMCID/PMID mapping and full-text evidence."""
    doi = "10.1234/mock.0001"
    providers = MockProviders(
        epmc_by_doi={doi: epmc_result(doi=doi, pdf_url="https://files.invalid/epmc.pdf")}
    )
    adapter = epmc_adapter(providers)
    record = adapter.lookup_by_doi(doi)
    assert record is not None
    assert record.identifiers["pmcid"] == "PMC1000001"
    assert record.identifiers["pmid"] == "31000001"

    check = adapter.cross_check(doi, record, {"identifiers": {}, "title": record.title})
    assert check.matched_on == ["doi"]
    assert check.fulltext_availability["is_open_access"] is True
    assert check.fulltext_availability["candidate_count"] >= 1
    assert check.source is Source.EUROPE_PMC


def test_ac025_metadata_disagreement_is_preserved_not_normalized_away():
    doi = "10.1234/mock.0001"
    providers = MockProviders(
        epmc_by_doi={doi: epmc_result(doi=doi, title="Europe PMC title", year=1999)}
    )
    record = epmc_adapter(providers).lookup_by_doi(doi)
    check = epmc_adapter(providers).cross_check(
        doi,
        record,
        {"title": "OpenAlex title", "publication_year": 2021, "identifiers": {}},
    )
    assert check.metadata_differences["title"] == {
        "openalex": "OpenAlex title",
        "europe_pmc": "Europe PMC title",
    }
    assert check.metadata_differences["publication_year"]["europe_pmc"] == 1999


def test_ac025_cross_check_makes_no_semantic_certification_claim():
    """The evidence object records matches and differences only."""
    doi = "10.1234/mock.0001"
    providers = MockProviders(epmc_by_doi={doi: epmc_result(doi=doi)})
    record = epmc_adapter(providers).lookup_by_doi(doi)
    check = epmc_adapter(providers).cross_check(doi, record, {"identifiers": {}})
    payload = check.to_dict()
    assert set(payload) == {
        "source",
        "matched_on",
        "identifiers",
        "metadata_differences",
        "fulltext_availability",
        "checked_at",
    }
    assert "verified" not in str(payload).lower()


def test_europe_pmc_offers_fulltext_xml_only_with_provider_evidence():
    doi = "10.1234/mock.0001"
    open_providers = MockProviders(epmc_by_doi={doi: epmc_result(doi=doi, is_oa=True)})
    record = epmc_adapter(open_providers).lookup_by_doi(doi)
    xml = [c for c in record.candidates if c.kind is ArtifactKind.XML]
    assert xml and xml[0].url.endswith("/PMC1000001/fullTextXML")

    closed_providers = MockProviders(epmc_by_doi={doi: epmc_result(doi=doi, is_oa=False)})
    closed = epmc_adapter(closed_providers).lookup_by_doi(doi)
    assert [c for c in closed.candidates if c.kind is ArtifactKind.XML] == []


def test_europe_pmc_returns_none_for_an_unknown_doi():
    providers = MockProviders(epmc_by_doi={})
    assert epmc_adapter(providers).lookup_by_doi("10.1234/unknown") is None


def test_europe_pmc_ignores_results_whose_doi_does_not_match():
    doi = "10.1234/wanted"
    providers = MockProviders(epmc_by_doi={doi: epmc_result(doi="10.1234/different")})
    assert epmc_adapter(providers).lookup_by_doi(doi) is None


def test_pmcid_and_doi_are_not_treated_as_interchangeable():
    adapter = epmc_adapter(MockProviders())
    assert adapter.fulltext_xml_url("10.1234/x") is None
    assert adapter.fulltext_xml_url("PMC12345") == f"{EPMC_BASE}/PMC12345/fullTextXML"


def test_yes_no_fields_stay_unknown_when_absent():
    doi = "10.1234/mock.0001"
    result = epmc_result(doi=doi)
    result.pop("isOpenAccess")
    providers = MockProviders(epmc_by_doi={doi: result})
    record = epmc_adapter(providers).lookup_by_doi(doi)
    assert record.is_oa is None  # not fabricated as False


# ============================================================= Unpaywall


def unpaywall_adapter(providers: MockProviders, email: str | None = "operator@example.org"):
    return UnpaywallAdapter(
        client_for(providers), UnpaywallConfig(base_url=UNPAYWALL_BASE), contact_email=email
    )


def test_unpaywall_requires_the_configured_email():
    with pytest.raises(ConfigurationError, match="contact email"):
        unpaywall_adapter(MockProviders(), None).resolve("10.1234/x")


def test_unpaywall_sends_the_email_parameter():
    doi = "10.1234/mock.0001"
    providers = MockProviders(
        unpaywall_by_doi={doi: unpaywall_record(doi=doi, pdf_url="https://files.invalid/u.pdf")}
    )
    unpaywall_adapter(providers).resolve(doi)
    assert providers.requests[-1].url.params["email"] == "operator@example.org"


def test_unpaywall_unknown_doi_returns_none_not_an_error():
    assert unpaywall_adapter(MockProviders()).resolve("10.1234/unknown") is None


def test_unpaywall_ranks_locations_deterministically():
    doi = "10.1234/ranked"
    record = {
        "doi": doi,
        "is_oa": True,
        "oa_status": "green",
        "best_oa_location": {
            "url_for_pdf": "https://files.invalid/best.pdf",
            "host_type": "repository",
            "version": "acceptedVersion",
        },
        "oa_locations": [
            {
                "url_for_pdf": "https://files.invalid/z-repo.pdf",
                "host_type": "repository",
                "version": "submittedVersion",
            },
            {
                "url_for_pdf": "https://files.invalid/a-publisher.pdf",
                "host_type": "publisher",
                "version": "publishedVersion",
            },
        ],
    }
    providers = MockProviders(unpaywall_by_doi={doi: record})
    resolved = unpaywall_adapter(providers).resolve(doi)
    urls = [c.url for c in resolved.candidates]
    assert urls == [
        "https://files.invalid/best.pdf",       # provider's own choice first
        "https://files.invalid/a-publisher.pdf",  # then publisher/published
        "https://files.invalid/z-repo.pdf",
    ]


def test_unpaywall_never_invents_an_abstract():
    doi = "10.1234/mock.0001"
    providers = MockProviders(
        unpaywall_by_doi={doi: unpaywall_record(doi=doi, pdf_url="https://files.invalid/u.pdf")}
    )
    record = unpaywall_adapter(providers).resolve(doi)
    assert record.abstract is None


def test_unpaywall_record_without_locations_yields_no_candidates():
    doi = "10.1234/closed"
    providers = MockProviders(
        unpaywall_by_doi={doi: unpaywall_record(doi=doi, pdf_url=None, is_oa=False)}
    )
    record = unpaywall_adapter(providers).resolve(doi)
    assert record is not None
    assert record.candidates == []
    assert record.is_oa is False
