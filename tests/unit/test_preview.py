"""Discovery preview and preview-fingerprint tests (SPEC_ASSISTED_SEARCH_V1 §51, 54).

The fingerprint is what makes a preview approval expire, so it is tested one filter at
a time: each independent change must invalidate the previous approval.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from harvester.config import Config
from harvester.http import ClientPool
from harvester.preview import PREVIEW_LIMIT, preview_discovery, query_fingerprint
from harvester.providers.openalex import OpenAlexQuery
from mocks import OPENALEX_BASE, MockProviders, openalex_work

BASE = OpenAlexQuery(
    search="ADHD adult remission recurrence longitudinal trajectory",
    topic_id="T10159",
    from_publication_year=2015,
    to_publication_year=2025,
    oa_status="gold",
)


def make_config(**overrides: object) -> Config:
    settings = {
        "openalex.base_url": OPENALEX_BASE,
        "openalex.api_key": "preview-key",
        "openalex.requests_per_second": 1000,
    }
    settings.update(overrides)  # type: ignore[arg-type]
    return Config.load(env={}, overrides=settings)


@pytest.fixture
def providers() -> MockProviders:
    return MockProviders(works=[openalex_work(i) for i in range(1, 26)], page_size=25)


def run_preview(providers: MockProviders, query: OpenAlexQuery, limit: int = PREVIEW_LIMIT):
    config = make_config()
    with ClientPool(config, transport=providers.transport, sleeper=lambda _s: None) as clients:
        return preview_discovery(config, clients, query, limit=limit)


# ============================================================ fingerprint identity


def test_the_same_query_fingerprints_the_same_way():
    assert query_fingerprint(BASE) == query_fingerprint(replace(BASE))


@pytest.mark.parametrize(
    "change",
    [
        {"search": "ADHD adult remission recurrence"},
        {"from_publication_year": 2016},
        {"to_publication_year": 2024},
        {"is_oa": False},
        {"oa_status": "green"},
        {"topic_id": "T10160"},
        {"primary_topic_only": True},
        {"has_doi": False},
        {"publication_year": 2020},
        {"extra_filters": ["type:article"]},
        {"languages": ["de"]},
        {"affiliation_countries": ["DE"]},
    ],
)
def test_any_discovery_input_change_invalidates_the_previous_approval(change):
    """Section 51: query, from-year, to-year, OA setting and topic each invalidate."""
    assert query_fingerprint(replace(BASE, **change)) != query_fingerprint(BASE)


def test_an_edited_query_produces_a_different_fingerprint():
    """Query A is approved; query B must not inherit that approval (section 55)."""
    query_a = replace(BASE, search="ADHD adult remission")
    query_b = replace(BASE, search="ADHD adult recurrence")
    assert query_fingerprint(query_a) != query_fingerprint(query_b)


def test_every_query_field_is_covered_by_the_parametrised_cases():
    """Guards the list above: a new query field must get its own staleness case."""
    covered = {
        "search", "from_publication_year", "to_publication_year", "is_oa", "oa_status",
        "topic_id", "primary_topic_only", "has_doi", "publication_year", "extra_filters",
        "languages", "affiliation_countries",
    }
    fields = set(BASE.to_dict()) - {"filter"}  # "filter" is derived from the rest
    assert fields == covered


# ================================================================ preview results


def test_preview_returns_at_most_ten_results(providers):
    result = run_preview(providers, BASE)
    assert len(result.records) <= PREVIEW_LIMIT == 10
    assert result.limit == PREVIEW_LIMIT


def test_preview_never_asks_for_more_than_it_shows(providers):
    """No unnecessarily large result set is retrieved merely for preview (§24)."""
    run_preview(providers, BASE)
    request = providers.requests[-1]
    assert request.url.params["per-page"] == str(PREVIEW_LIMIT)


def test_a_larger_limit_is_clamped_to_the_ceiling(providers):
    result = run_preview(providers, BASE, limit=500)
    assert result.limit == PREVIEW_LIMIT
    assert len(result.records) <= PREVIEW_LIMIT


def test_preview_uses_one_discovery_call_only(providers):
    run_preview(providers, BASE)
    assert providers.count("openalex.works") == 1


def test_preview_records_carry_the_display_fields(providers):
    record = run_preview(providers, BASE).records[0].to_dict()
    assert record["title"]
    assert record["publication_year"]
    assert record["first_author"] == record["authors"][0]
    assert record["doi"]
    assert record["journal"]
    assert record["oa_status"]
    assert record["discovered_via"] == ["openalex"]


def test_preview_reports_the_provider_total(providers):
    result = run_preview(providers, BASE)
    assert result.total_count == 25


def test_preview_fingerprint_matches_the_query_it_ran(providers):
    result = run_preview(providers, BASE)
    assert result.fingerprint == query_fingerprint(BASE)


def test_an_empty_result_set_is_reported_as_empty_not_as_an_error():
    providers = MockProviders(works=[])
    result = run_preview(providers, BASE)
    assert result.records == []
    assert result.to_dict()["count"] == 0
