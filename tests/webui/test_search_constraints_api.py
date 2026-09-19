"""Search constraints from the browser through to the provider request.

The whole path is exercised over the real HTTP server: the payload the front end
sends, the query model, the preview gate, the run record and the provider request the
deterministic mock actually receives.
"""

from __future__ import annotations

import json

import pytest

from harvester.state import StateStore
from mocks import make_pdf_bytes, openalex_work

from conftest import wait_for_idle

MIXED = [
    openalex_work(1, language="en", institution_countries=("US",)),
    openalex_work(2, language="de", institution_countries=("DE",)),
    openalex_work(3, language="de", institution_countries=("US",)),
    openalex_work(4, language="fr", institution_countries=("FR", "DE")),
    openalex_work(5, language="en", institution_countries=("AT",)),
]


@pytest.fixture
def mixed(ui):
    """A corpus whose languages and affiliation countries vary independently."""
    ui.providers.works = list(MIXED)
    ui.providers.files = {f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, 6)}
    return ui


def openalex_filter(ui) -> str:
    request = [r for r in ui.providers.requests if "openalex" in r.url.host][-1]
    return request.url.params["filter"]


# ============================================================ the value lists


def test_the_vocabulary_endpoint_serves_both_lists_with_display_names(ui):
    vocab = ui.get("/api/vocabulary")
    assert ui.status == 200
    codes = {entry["code"]: entry["name"] for entry in vocab["languages"]}
    assert codes["en"] == "English" and codes["de"] == "German"
    countries = {entry["code"]: entry["name"] for entry in vocab["countries"]}
    assert countries["DE"] == "Germany" and countries["AT"] == "Austria"
    assert len(vocab["languages"]) == 181 and len(vocab["countries"]) == 247
    # Sorted by the name the operator reads, not by code.
    names = [entry["name"] for entry in vocab["countries"]]
    assert names == sorted(names)


# =================================================== defaults behave as before


def test_a_preview_without_the_constraints_sends_no_such_filter(mixed):
    result = mixed.post("/api/search/preview", {"search": "x", "limit": 10})
    assert mixed.status == 200
    assert len(result["results"]) == len(MIXED)
    assert "language:" not in openalex_filter(mixed)
    assert "country_code" not in openalex_filter(mixed)


def test_empty_constraint_lists_are_the_same_as_omitting_them(mixed):
    with_empty = mixed.post(
        "/api/search/preview",
        {"search": "x", "limit": 10, "languages": [], "affiliation_countries": []},
    )
    assert with_empty["fingerprint"] == mixed.post(
        "/api/search/preview", {"search": "x", "limit": 10}
    )["fingerprint"]


def test_a_harvest_without_the_constraints_records_them_as_any(mixed):
    mixed.post("/api/harvest", {"search": "x", "limit": 2, "dry_run": False})
    operation = wait_for_idle(mixed)
    detail = mixed.get(f"/api/runs/{operation['run_id']}")
    assert detail["query"]["languages"] == []
    assert detail["query"]["affiliation_countries"] == []


# ======================================================= language, single / OR


def test_a_language_constraint_reaches_the_provider_and_narrows_the_preview(mixed):
    result = mixed.post(
        "/api/search/preview", {"search": "x", "limit": 10, "languages": ["de"]}
    )
    assert "language:de" in openalex_filter(mixed)
    assert {r["title"] for r in result["results"]} == {
        "Mock Open Access Article 2",
        "Mock Open Access Article 3",
    }


def test_several_languages_are_or_ed_end_to_end(mixed):
    result = mixed.post(
        "/api/search/preview", {"search": "x", "limit": 10, "languages": ["de", "fr"]}
    )
    assert "language:de|fr" in openalex_filter(mixed)
    assert len(result["results"]) == 3


# ======================================================== country, single / OR


def test_an_affiliation_country_reaches_the_provider_and_narrows_the_preview(mixed):
    result = mixed.post(
        "/api/search/preview", {"search": "x", "limit": 10, "affiliation_countries": ["DE"]}
    )
    assert "authorships.institutions.country_code:DE" in openalex_filter(mixed)
    assert {r["title"] for r in result["results"]} == {
        "Mock Open Access Article 2",
        "Mock Open Access Article 4",
    }


def test_several_countries_are_or_ed_end_to_end(mixed):
    result = mixed.post(
        "/api/search/preview",
        {"search": "x", "limit": 10, "affiliation_countries": ["DE", "AT"]},
    )
    assert "authorships.institutions.country_code:AT|DE" in openalex_filter(mixed)
    assert len(result["results"]) == 3


def test_language_and_country_select_different_sets(mixed):
    by_language = mixed.post(
        "/api/search/preview", {"search": "x", "limit": 10, "languages": ["de"]}
    )
    by_country = mixed.post(
        "/api/search/preview", {"search": "x", "limit": 10, "affiliation_countries": ["DE"]}
    )
    assert {r["title"] for r in by_language["results"]} != {
        r["title"] for r in by_country["results"]
    }


# ============================================== nothing is ignored in silence


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"languages": ["xx"]}, "unknown language code"),
        ({"languages": ["de", "nope"]}, "unknown language code"),
        ({"affiliation_countries": ["ZZ"]}, "unknown affiliation country code"),
        ({"affiliation_countries": ["DE", "Germany"]}, "unknown affiliation country code"),
    ],
)
def test_an_unusable_code_is_a_clean_error_not_a_quietly_dropped_filter(mixed, payload, message):
    result = mixed.post("/api/search/preview", {"search": "x", "limit": 10, **payload})
    assert mixed.status == 400
    assert message in result["error"]

    result = mixed.post("/api/harvest", {"search": "x", "limit": 1, "dry_run": False, **payload})
    assert mixed.status == 400
    assert message in result["error"]
    assert mixed.get("/api/runs")["runs"] == [], "a rejected constraint still started a run"


# ================================================== the preview gate covers them


@pytest.mark.parametrize(
    "change",
    [
        {"languages": ["de"]},
        {"languages": ["de", "en"]},
        {"affiliation_countries": ["DE"]},
        {"affiliation_countries": ["DE", "AT"]},
    ],
)
def test_adding_a_constraint_after_a_preview_invalidates_the_approval(mixed, change):
    preview = mixed.post("/api/search/preview", {"search": "x", "limit": 10})
    body = {
        "search": "x", "limit": 1, "search_mode": "assisted",
        "research_question": "q", "generated_query": "x",
        "preview_fingerprint": preview["fingerprint"], **change,
    }
    result = mixed.post("/api/harvest", body)
    assert mixed.status == 409
    assert result["detail"]["reason"] == "stale_preview"
    assert mixed.get("/api/runs")["runs"] == []


def test_removing_a_constraint_after_a_preview_also_invalidates_it(mixed):
    preview = mixed.post(
        "/api/search/preview", {"search": "x", "limit": 10, "languages": ["de"]}
    )
    result = mixed.post(
        "/api/harvest",
        {
            "search": "x", "limit": 1, "search_mode": "assisted",
            "research_question": "q", "generated_query": "x",
            "preview_fingerprint": preview["fingerprint"],
        },
    )
    assert mixed.status == 409
    assert result["detail"]["reason"] == "stale_preview"


def test_the_constrained_query_can_be_previewed_and_then_harvested(mixed):
    body = {"search": "x", "limit": 10, "languages": ["de"], "affiliation_countries": ["DE"]}
    preview = mixed.post("/api/search/preview", body)
    assert [r["title"] for r in preview["results"]] == ["Mock Open Access Article 2"]

    mixed.post(
        "/api/harvest",
        {
            **body, "limit": 2, "search_mode": "assisted",
            "research_question": "q", "generated_query": "x",
            "preview_fingerprint": preview["fingerprint"],
        },
    )
    assert mixed.status == 200
    operation = wait_for_idle(mixed)
    detail = mixed.get(f"/api/runs/{operation['run_id']}")
    assert detail["query"]["languages"] == ["de"]
    assert detail["query"]["affiliation_countries"] == ["DE"]
    assert detail["discovery_seen"] == 1


# ================================================ run record and reproducibility


def test_the_run_record_carries_the_constraints_for_a_later_reproduction(mixed):
    mixed.post(
        "/api/harvest",
        {"search": "x", "limit": 2, "dry_run": False,
         "languages": ["en", "de"], "affiliation_countries": ["AT", "DE"]},
    )
    operation = wait_for_idle(mixed)

    with StateStore(mixed.server.context.config.state_db) as store:
        stored = store.get_run(operation["run_id"]).query
    assert stored["languages"] == ["de", "en"]
    assert stored["affiliation_countries"] == ["AT", "DE"]
    assert "language:de|en" in stored["filter"]
    assert "authorships.institutions.country_code:AT|DE" in stored["filter"]


def test_selection_order_does_not_change_the_recorded_query(mixed):
    first = mixed.post(
        "/api/search/preview",
        {"search": "x", "limit": 10, "languages": ["de", "en"], "affiliation_countries": ["DE", "AT"]},
    )
    second = mixed.post(
        "/api/search/preview",
        {"search": "x", "limit": 10, "languages": ["en", "de"], "affiliation_countries": ["AT", "DE"]},
    )
    assert first["fingerprint"] == second["fingerprint"]
    assert first["query"] == second["query"]


# ================================================= the advisor knows about them


def test_the_advisor_is_told_the_constraints_are_already_applied(mixed):
    mixed.post(
        "/api/search/advice",
        {"research_question": "Wie verändert sich ADHS im Erwachsenenalter?",
         "languages": ["de"], "affiliation_countries": ["DE", "AT"]},
    )
    assert mixed.status == 200
    content = mixed.providers.advisor_requests[0]["messages"][0]["content"]
    supplied = json.loads(content.split("<active_filters_and_question>")[1].split("</")[0])
    assert supplied["active_filters"]["languages"] == ["de"]
    assert supplied["active_filters"]["affiliation_countries"] == ["AT", "DE"]

    system = mixed.providers.advisor_requests[0]["system"]
    assert "no language names" in system
    assert "affiliation filter" in system


def test_an_unusable_code_is_rejected_before_the_advisor_is_called(mixed):
    result = mixed.post(
        "/api/search/advice", {"research_question": "q", "languages": ["klingon"]}
    )
    assert mixed.status == 400
    assert "unknown language code" in result["error"]
    assert mixed.providers.advisor_requests == []


# ====================================================== the front end wires them


def test_the_harvest_form_offers_both_constraints(ui):
    body = ui.get("/app.js", raw=True).decode("utf-8")
    for marker in (
        "Language(s)",
        "Research institutions from",
        "Filters by countries associated with author institutional affiliations",
        "not the country a study was carried out in",
        "Any — no filter applied",
        "/api/vocabulary",
    ):
        assert marker in body, marker


def test_the_front_end_sends_and_fingerprints_both_constraints(ui):
    body = ui.get("/app.js", raw=True).decode("utf-8")
    payload = body.split("function harvestPayload")[1].split("function previewPayload")[0]
    assert "languages: [...f.languages]" in payload
    assert "affiliation_countries: [...f.affiliation_countries]" in payload
    # And the client-side staleness key must move with them, or the browser would
    # offer a harvest button that the server then refuses.
    key = body.split("function discoveryKey")[1].split("}")[0]
    assert "f.languages" in key and "f.affiliation_countries" in key
