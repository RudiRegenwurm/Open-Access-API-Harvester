"""Language and affiliation-country constraints through the real pipeline.

These tests do not assert on a filter string. They run the actual discovery path
against the deterministic provider mock — which applies both filters the way the live
API does — so a constraint that is accepted but never sent produces a visible failure
rather than a green test.

Provider capability, stated once and relied on throughout: OpenAlex is the only
discovery provider. Europe PMC and Unpaywall are looked up per already-discovered DOI
and can never add a work to the result set, so a discovery constraint is not something
they can silently ignore — it is not theirs to apply. See ``docs/providers.md`` 1.9.
"""

from __future__ import annotations

import pytest

from harvester.config import Config
from harvester.http import ClientPool
from harvester.orchestrator import Harvester
from harvester.preview import preview_discovery
from harvester.providers.openalex import OpenAlexQuery
from harvester.state import StateStore
from mocks import EPMC_BASE, OPENALEX_BASE, UNPAYWALL_BASE, MockProviders, make_pdf_bytes, openalex_work

#: A small, deliberately mixed corpus: languages and institution countries vary
#: independently of one another, so neither constraint can be satisfied by accident.
WORKS = [
    openalex_work(1, language="en", institution_countries=("US",)),
    openalex_work(2, language="de", institution_countries=("DE",)),
    openalex_work(3, language="de", institution_countries=("US", "GB")),
    openalex_work(4, language="fr", institution_countries=("FR", "DE")),
    openalex_work(5, language="en", institution_countries=("AT",)),
    openalex_work(6, language=None, institution_countries=("DE",)),   # language unknown
    openalex_work(7, language="de", institution_countries=()),        # no affiliation
]


@pytest.fixture
def providers() -> MockProviders:
    return MockProviders(
        works=list(WORKS),
        page_size=25,
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, 8)},
    )


@pytest.fixture
def config(tmp_path) -> Config:
    return Config.load(
        env={},
        overrides={
            "storage_root": str(tmp_path / "corpus"),
            "state_db": str(tmp_path / "state.sqlite3"),
            "reports_dir": str(tmp_path / "reports"),
            "contact_email": "operator@example.org",
            "openalex.api_key": "k",
            "openalex.base_url": OPENALEX_BASE,
            "openalex.requests_per_second": 1000,
            "europe_pmc.base_url": EPMC_BASE,
            "unpaywall.base_url": UNPAYWALL_BASE,
            "downloads.concurrency": 1,
            "downloads.min_pdf_size_bytes": 512,
        },
    )


def discovered(config: Config, providers: MockProviders, query: OpenAlexQuery) -> list[str]:
    """Run the real discovery path and return the discovered work ids."""
    with ClientPool(config, transport=providers.transport, sleeper=lambda _s: None) as clients:
        result = preview_discovery(config, clients, query, limit=10)
    return [record.document_id for record in result.records]


def titles(config: Config, providers: MockProviders, query: OpenAlexQuery) -> set[str]:
    with ClientPool(config, transport=providers.transport, sleeper=lambda _s: None) as clients:
        result = preview_discovery(config, clients, query, limit=10)
    return {record.title for record in result.records}


# ============================================ 1./4. defaults change nothing at all


def test_the_default_query_still_discovers_everything(config, providers):
    assert len(discovered(config, providers, OpenAlexQuery(search="x"))) == len(WORKS)


def test_the_default_query_sends_no_language_or_country_filter(config, providers):
    discovered(config, providers, OpenAlexQuery(search="x"))
    sent = providers.requests[-1].url.params["filter"]
    assert "language:" not in sent
    assert "country_code" not in sent


# =================================================== 2./3. language, single and OR


def test_a_single_language_narrows_discovery_to_that_language(config, providers):
    found = titles(config, providers, OpenAlexQuery(search="x", languages=["de"]))
    assert found == {
        "Mock Open Access Article 2",
        "Mock Open Access Article 3",
        "Mock Open Access Article 7",
    }


def test_two_languages_discover_the_union_not_the_intersection(config, providers):
    only_de = titles(config, providers, OpenAlexQuery(search="x", languages=["de"]))
    only_fr = titles(config, providers, OpenAlexQuery(search="x", languages=["fr"]))
    both = titles(config, providers, OpenAlexQuery(search="x", languages=["de", "fr"]))
    assert both == only_de | only_fr
    assert len(both) > len(only_de)


def test_an_unknown_publication_language_is_never_counted_as_a_selected_one(config, providers):
    """Work 6 has ``language: null``. Unknown must stay unknown."""
    for codes in (["en"], ["de"], ["en", "de", "fr"]):
        found = titles(config, providers, OpenAlexQuery(search="x", languages=codes))
        assert "Mock Open Access Article 6" not in found
    # It is still discoverable when no language constraint is active.
    assert "Mock Open Access Article 6" in titles(config, providers, OpenAlexQuery(search="x"))


# ==================================================== 5./6. country, single and OR


def test_a_single_affiliation_country_narrows_discovery(config, providers):
    found = titles(config, providers, OpenAlexQuery(search="x", affiliation_countries=["DE"]))
    assert found == {
        "Mock Open Access Article 2",
        "Mock Open Access Article 4",
        "Mock Open Access Article 6",
    }


def test_several_countries_discover_the_union(config, providers):
    de = titles(config, providers, OpenAlexQuery(search="x", affiliation_countries=["DE"]))
    at = titles(config, providers, OpenAlexQuery(search="x", affiliation_countries=["AT"]))
    both = titles(config, providers, OpenAlexQuery(search="x", affiliation_countries=["DE", "AT"]))
    assert both == de | at


def test_one_matching_affiliation_among_several_is_enough(config, providers):
    """Work 4 is FR+DE: selecting Germany must find it (at least one affiliation)."""
    found = titles(config, providers, OpenAlexQuery(search="x", affiliation_countries=["DE"]))
    assert "Mock Open Access Article 4" in found


def test_a_work_without_any_institutional_affiliation_is_never_attributed(config, providers):
    """Work 7 has no institutions at all — it belongs to no country."""
    for codes in (["DE"], ["US"], ["DE", "US", "FR"]):
        found = titles(config, providers, OpenAlexQuery(search="x", affiliation_countries=codes))
        assert "Mock Open Access Article 7" not in found
    assert "Mock Open Access Article 7" in titles(config, providers, OpenAlexQuery(search="x"))


# ============================== the two constraints are independent and compose


def test_language_and_country_are_not_confused_with_one_another(config, providers):
    """German-language work is not the same set as German-institution work."""
    german_language = titles(config, providers, OpenAlexQuery(search="x", languages=["de"]))
    german_institutions = titles(
        config, providers, OpenAlexQuery(search="x", affiliation_countries=["DE"])
    )
    assert german_language != german_institutions
    # Work 3 is written in German by US/GB institutions; work 6 is a German institution
    # publishing in an unknown language. Each belongs to exactly one of the two sets.
    assert "Mock Open Access Article 3" in german_language
    assert "Mock Open Access Article 3" not in german_institutions
    assert "Mock Open Access Article 6" in german_institutions
    assert "Mock Open Access Article 6" not in german_language


def test_both_constraints_together_are_an_intersection(config, providers):
    found = titles(
        config, providers, OpenAlexQuery(search="x", languages=["de"], affiliation_countries=["DE"])
    )
    assert found == {"Mock Open Access Article 2"}


# ============================================= 8./9. providers without capability


def test_only_the_discovery_provider_receives_the_constraints(config, providers):
    """Europe PMC and Unpaywall must never be sent a constraint they cannot honour."""
    with StateStore(config.state_db) as store, ClientPool(
        config, transport=providers.transport, sleeper=lambda _s: None
    ) as clients:
        Harvester(config, store, clients).harvest(
            OpenAlexQuery(search="x", languages=["de"], affiliation_countries=["DE"]), limit=5
        )

    for request in providers.requests:
        host = request.url.host
        query = str(request.url.query)
        if "openalex" in host:
            continue
        assert "language" not in query, f"{host} was sent a language constraint"
        assert "country_code" not in query, f"{host} was sent a country constraint"


def test_the_constraint_holds_for_every_document_that_reaches_the_corpus(config, providers):
    """End to end: what is acquired obeys the constraint, not just what is requested."""
    with StateStore(config.state_db) as store, ClientPool(
        config, transport=providers.transport, sleeper=lambda _s: None
    ) as clients:
        result = Harvester(config, store, clients).harvest(
            OpenAlexQuery(search="x", languages=["de"]), limit=10
        )

    with StateStore(config.state_db) as store:
        document_ids = store.documents_for_run(result.run_id)
        assert document_ids, "the harvest discovered nothing to check"
        for document_id in document_ids:
            languages = {
                entry["record"].get("extra", {}).get("language")
                for entry in store.source_records_for_document(document_id)
                if entry["source"] == "openalex"
            }
            assert languages == {"de"}, f"{document_id} carries {languages}"


def test_the_run_record_keeps_the_constraints_for_provenance(config, providers):
    with StateStore(config.state_db) as store, ClientPool(
        config, transport=providers.transport, sleeper=lambda _s: None
    ) as clients:
        result = Harvester(config, store, clients).harvest(
            OpenAlexQuery(search="x", languages=["de", "en"], affiliation_countries=["DE"]),
            limit=3,
            dry_run=True,
        )

    with StateStore(config.state_db) as store:
        run = store.get_run(result.run_id)
    assert run.query["languages"] == ["de", "en"]
    assert run.query["affiliation_countries"] == ["DE"]
    assert "language:de|en" in run.query["filter"]
    assert "authorships.institutions.country_code:DE" in run.query["filter"]


def test_a_resumed_run_reuses_exactly_the_same_constraints(config, providers):
    """Resume rebuilds the query from state; the scope must not drift."""
    with StateStore(config.state_db) as store, ClientPool(
        config, transport=providers.transport, sleeper=lambda _s: None
    ) as clients:
        harvester = Harvester(config, store, clients)
        result = harvester.harvest(
            OpenAlexQuery(search="x", languages=["de"], affiliation_countries=["DE"]),
            limit=1,
            dry_run=True,
        )
        original = providers.requests[-1].url.params["filter"]
        # Put the run back into the state an interrupted run has, so resuming really
        # re-enters discovery instead of finding it already complete.
        store.update_run_cursor(result.run_id, None, complete=False, pages=0, seen=0)
        before = len(providers.requests)
        harvester.resume(result.run_id)

    resumed = [r for r in providers.requests[before:] if "openalex" in r.url.host]
    assert resumed, "the resumed run performed no discovery"
    assert resumed[0].url.params["filter"] == original


# ================================================ 10. backward-compatible resume


def test_a_run_stored_without_the_constraints_resumes_with_its_original_scope(
    config, providers
):
    """Simulates a run recorded before the constraints existed."""
    with StateStore(config.state_db) as store, ClientPool(
        config, transport=providers.transport, sleeper=lambda _s: None
    ) as clients:
        harvester = Harvester(config, store, clients)
        result = harvester.harvest(OpenAlexQuery(search="x"), limit=10, dry_run=True)

        # Strip the keys from the stored query, as an older build would have left it,
        # and put the run back into the state an interrupted run has.
        legacy = {k: v for k, v in store.get_run(result.run_id).query.items()
                  if k not in ("languages", "affiliation_countries")}
        assert "languages" not in legacy and "affiliation_countries" not in legacy
        with store.transaction() as conn:
            from harvester.util import to_json

            conn.execute(
                "UPDATE runs SET query_json = ? WHERE run_id = ?",
                (to_json(legacy), result.run_id),
            )
        store.update_run_cursor(result.run_id, None, complete=False, pages=0, seen=0)

        before = len(providers.requests)
        harvester.resume(result.run_id)

    sent = [r for r in providers.requests[before:] if "openalex" in r.url.host]
    assert sent, "the resumed run performed no discovery"
    for request in sent:
        assert "language:" not in request.url.params["filter"]
        assert "country_code" not in request.url.params["filter"]
