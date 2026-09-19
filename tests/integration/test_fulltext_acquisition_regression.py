"""Regression cover for three documents whose free full text was not acquired.

All three are demonstrably open or free to read, were discovered correctly, and still
ended ``FAILED_PERMANENT`` in a production run. The three failures had one shape
between them: the only full-text location OpenAlex offered was an NCBI PMC URL that
now answers automated clients with a proof-of-work challenge instead of the file,
and Europe PMC — which holds all three and offers a working mirror — was never
consulted successfully, because it is looked up by DOI and the DOI index answered
"nothing here" for articles it demonstrably holds.

The cases, with the identifiers and behaviour observed live on 2026-08-19:

===========================  =========  ======  ==============================
DOI                          PMCID      isOA    what is actually retrievable
===========================  =========  ======  ==============================
10.1371/journal.pone.0278830 PMC9876350 Y       publisher PDF, EPMC PDF, XML
10.1080/15299732.2023.2289195 PMC11299760 N     EPMC PDF only
10.1002/jts.22967            PMC11299761 N      nothing (EPMC render answers 500)
===========================  =========  ======  ==============================

The third case stays unacquirable, and that is the honest outcome: the only routes
are an access challenge we do not solve and a provider endpoint that is failing. What
must change is that it is reported as such rather than as a corrupt file.

No test here touches the network; the payloads mirror the recorded live responses.
"""

from __future__ import annotations

import pytest

from harvester.errors import BotChallengeError, ErrorCategory
from harvester.identity import document_id_for_doi, pmcid_from_urls
from harvester.models import ArtifactKind, DocumentStatus, FulltextCandidate, Source
from harvester.orchestrator import _order_candidates
from harvester.providers.europepmc import EuropePmcAdapter
from harvester.validation import looks_like_bot_challenge, validate_pdf
from mocks import (
    EPMC_BASE,
    FILES_BASE,
    Behavior,
    MockProviders,
    epmc_result,
    make_pdf_bytes,
    make_pow_challenge_bytes,
    openalex_work,
)

CASE_1_DOI = "10.1371/journal.pone.0278830"
CASE_2_DOI = "10.1080/15299732.2023.2289195"
CASE_3_DOI = "10.1002/jts.22967"

CASE_1 = {"doi": CASE_1_DOI, "pmcid": "PMC9876350", "pmid": "36696396", "is_oa": True}
CASE_2 = {"doi": CASE_2_DOI, "pmcid": "PMC11299760", "pmid": "38047579", "is_oa": False}
CASE_3 = {"doi": CASE_3_DOI, "pmcid": "PMC11299761", "pmid": "37671574", "is_oa": False}

#: The PMC location OpenAlex offers for each case — the one behind the challenge.
def pmc_pdf_url(case: dict) -> str:
    return f"https://pmc.ncbi.nlm.nih.gov/articles/{case['pmcid']}/pdf/main.pdf"


def pmc_route(case: dict) -> str:
    return f"other:pmc.ncbi.nlm.nih.gov/articles/{case['pmcid']}/pdf/main.pdf"


def epmc_mirror_url(case: dict) -> str:
    """Europe PMC's own PDF location, hosted by the mock file server."""
    return f"{FILES_BASE}/epmc-{case['pmcid']}.pdf"


def providers_for(case: dict, *, doi_search_blind: bool = True) -> MockProviders:
    """OpenAlex offering only the challenged PMC URL; Europe PMC holding a mirror."""
    providers = MockProviders(
        works=[
            openalex_work(1, doi=case["doi"], pdf_url=pmc_pdf_url(case), pmid=case["pmid"])
        ],
        epmc_by_doi={
            case["doi"]: epmc_result(
                doi=case["doi"],
                pmcid=case["pmcid"],
                pmid=case["pmid"],
                is_oa=case["is_oa"],
                pdf_url=epmc_mirror_url(case),
            )
        },
        files={f"/epmc-{case['pmcid']}.pdf": make_pdf_bytes()},
        epmc_doi_search_blind=doi_search_blind,
    )
    providers.script_route(
        pmc_route(case),
        [Behavior(status=200, content=make_pow_challenge_bytes())] * 8,
    )
    return providers


# ================================================ A. the challenge is named, not solved


def test_a_the_pmc_challenge_page_is_reported_as_a_challenge(tmp_path):
    """It is not a broken PDF: the location is fine and closed to us."""
    path = tmp_path / "challenge.pdf"
    path.write_bytes(make_pow_challenge_bytes())

    with pytest.raises(BotChallengeError) as excinfo:
        validate_pdf(path, min_size_bytes=512)

    error = excinfo.value
    assert error.category is ErrorCategory.BOT_CHALLENGE
    assert not error.retryable          # the same challenge comes back every time
    assert "challenge" in error.message.lower()


def test_a_an_ordinary_html_page_is_still_an_invalid_pdf(tmp_path):
    """The new class must not swallow the plain "landing page instead of PDF" case."""
    path = tmp_path / "landing.pdf"
    path.write_bytes(b"<!doctype html><html><body><h1>Article landing page</h1></body></html>")

    with pytest.raises(Exception) as excinfo:
        validate_pdf(path, min_size_bytes=512)
    assert excinfo.value.category is ErrorCategory.INVALID_PDF


def test_a_challenge_detection_does_not_misfire_on_real_content():
    assert not looks_like_bot_challenge(make_pdf_bytes())
    assert not looks_like_bot_challenge(b"<html><body>a page that mentions robots</body></html>")
    assert looks_like_bot_challenge(make_pow_challenge_bytes())


# ============================================== B. deriving a PMCID from what we have


def test_b_a_pmcid_is_derived_from_pmc_urls_only_when_unambiguous():
    assert pmcid_from_urls([pmc_pdf_url(CASE_2)]) == "PMC11299760"
    assert pmcid_from_urls(["https://europepmc.org/articles/PMC9876350?pdf=render"]) == "PMC9876350"
    # Same identifier from two locations is still one identifier.
    assert pmcid_from_urls(
        [pmc_pdf_url(CASE_1), "https://europepmc.org/articles/PMC9876350?pdf=render"]
    ) == "PMC9876350"


def test_b_an_ambiguous_or_untrusted_url_yields_no_pmcid():
    # Two different PMCIDs: the document's identity is unclear, which is worse than
    # unknown — guessing one of them would attach a real article to the wrong record.
    assert pmcid_from_urls([pmc_pdf_url(CASE_1), pmc_pdf_url(CASE_2)]) is None
    # "PMC123" on an unrelated host is a path segment, not an identifier.
    assert pmcid_from_urls(["https://example.org/files/PMC9876350/paper.pdf"]) is None
    assert pmcid_from_urls([]) is None
    assert pmcid_from_urls(["not a url", ""]) is None


def test_b_a_derived_pmcid_never_becomes_stated_metadata(config, store, harvester_factory, query):
    """It resolves a lookup; it is not a fact the harvester asserts about the work."""
    providers = providers_for(CASE_2)
    harvester_factory(providers).harvest(query)

    metadata = store.get_metadata(document_id_for_doi(CASE_2_DOI))
    check = next(c for c in metadata["cross_checks"] if c["source"] == Source.EUROPE_PMC.value)
    assert check["fulltext_availability"]["resolved_by"] == "derived_pmcid"
    # Europe PMC's own answer supplies the PMCID; the derivation only opened the door.
    assert metadata["identifiers"]["pmcid"] == CASE_2["pmcid"]


# ================================================== C. the Europe PMC lookup fallback


def test_c_the_adapter_resolves_by_pmcid_when_the_doi_index_is_blind():
    providers = providers_for(CASE_3)
    adapter = EuropePmcAdapter(_client(providers), _config().europe_pmc)

    assert adapter.lookup_by_doi(CASE_3_DOI) is None      # the observed failure mode
    record = adapter.lookup_by_pmcid(CASE_3["pmcid"])
    assert record is not None and record.doi == CASE_3_DOI


def test_c_the_pmid_lookup_scopes_the_query_to_pubmed():
    """The bare EXT_ID form returned nothing live; the scoped form resolved at once."""
    providers = providers_for(CASE_1)
    adapter = EuropePmcAdapter(_client(providers), _config().europe_pmc)

    record = adapter.lookup_by_pmid(CASE_1["pmid"])
    assert record is not None and record.doi == CASE_1_DOI

    query = providers.requests[-1].url.params.get("query", "")
    assert query == f"EXT_ID:{CASE_1['pmid']} AND SRC:MED"
    assert adapter.lookup_by_pmid("not-a-pmid") is None


def test_c_a_blind_doi_lookup_no_longer_ends_the_search(config, store, harvester_factory, query):
    providers = providers_for(CASE_2)
    harvester_factory(providers).harvest(query)

    records = store.source_records_for_document(document_id_for_doi(CASE_2_DOI))
    assert Source.EUROPE_PMC.value in {r["source"] for r in records}


# ============================================ D. what happens to the three documents


def test_d_case_1_is_acquired_from_the_publisher(config, store, harvester_factory, query):
    """PLOS offers a working PDF; the challenged PMC URL must not get in the way."""
    providers = MockProviders(
        works=[
            openalex_work(
                1, doi=CASE_1_DOI, pdf_url=f"{FILES_BASE}/plos.pdf", pmid=CASE_1["pmid"]
            )
        ],
        epmc_by_doi={
            CASE_1_DOI: epmc_result(
                doi=CASE_1_DOI, pmcid=CASE_1["pmcid"], pmid=CASE_1["pmid"],
                is_oa=True, pdf_url=epmc_mirror_url(CASE_1),
            )
        },
        files={"/plos.pdf": make_pdf_bytes(), f"/epmc-{CASE_1['pmcid']}.pdf": make_pdf_bytes()},
        epmc_doi_search_blind=True,
    )
    harvester_factory(providers).harvest(query)

    document_id = document_id_for_doi(CASE_1_DOI)
    assert store.get_document_row(document_id)["status"] == DocumentStatus.COMPLETED.value
    artifacts = store.get_artifacts(document_id)
    assert ArtifactKind.PDF.value in artifacts
    # is_oa: Y, so the OA full-text subset applies and the XML is offered too.
    assert ArtifactKind.XML.value in artifacts


def test_d_case_2_is_acquired_from_the_europe_pmc_mirror(
    config, store, harvester_factory, query
):
    """The whole point: the only OpenAlex location is challenged, EPMC has a mirror."""
    providers = providers_for(CASE_2)
    harvester_factory(providers).harvest(query)

    document_id = document_id_for_doi(CASE_2_DOI)
    assert store.get_document_row(document_id)["status"] == DocumentStatus.COMPLETED.value
    artifacts = store.get_artifacts(document_id)
    assert ArtifactKind.PDF.value in artifacts
    assert artifacts[ArtifactKind.PDF.value].source is Source.EUROPE_PMC
    # is_oa: N — free to read but outside the OA subset, so no XML is even attempted.
    assert ArtifactKind.XML.value not in artifacts
    assert not any(a["operation"] == "acquire.xml" for a in _attempts(store, document_id))


def test_d_case_3_fails_honestly_when_every_route_is_closed(
    config, store, harvester_factory, query
):
    """No mirror works. The document still fails — but says why, truthfully."""
    providers = providers_for(CASE_3)
    # Europe PMC's render endpoint answers 500 for this article (observed live).
    providers.script_route(
        f"file:/epmc-{CASE_3['pmcid']}.pdf",
        [Behavior(status=500, content=b'{"error":"unavailable"}')] * 8,
    )
    harvester_factory(providers).harvest(query)

    document_id = document_id_for_doi(CASE_3_DOI)
    assert ArtifactKind.PDF.value not in store.get_artifacts(document_id)

    categories = {a["error_category"] for a in _attempts(store, document_id) if not a["ok"]}
    # The challenge is named as a challenge, not misreported as a corrupt file.
    assert ErrorCategory.BOT_CHALLENGE.value in categories
    assert ErrorCategory.INVALID_PDF.value not in categories


def test_d_being_in_europe_pmc_is_not_the_same_as_being_open_access():
    """``inEPMC=Y`` with ``isOpenAccess=N`` is real, and ``fullTextXML`` 404s for it.

    Both remaining cases look exactly like this, and asking anyway produced one
    predictable NOT_FOUND per document.
    """
    free_but_not_oa = epmc_result(
        doi=CASE_2_DOI, pmcid=CASE_2["pmcid"], pmid=CASE_2["pmid"],
        is_oa=False, in_epmc=True, pdf_url=epmc_mirror_url(CASE_2),
    )
    providers = MockProviders(epmc_by_doi={CASE_2_DOI: free_but_not_oa})
    record = EuropePmcAdapter(_client(providers), _config().europe_pmc).lookup_by_doi(CASE_2_DOI)

    assert record is not None
    assert record.extra["in_epmc"] is True
    assert [c for c in record.candidates if c.kind is ArtifactKind.XML] == []
    # The PDF mirror is still offered: free to read is enough for a PDF.
    assert [c for c in record.candidates if c.kind is ArtifactKind.PDF]


# ===================================================== E. ordering and de-duplication


def test_e_a_challenged_location_is_tried_last_but_still_tried():
    challenged = FulltextCandidate(
        url=pmc_pdf_url(CASE_1), kind=ArtifactKind.PDF, source=Source.OPENALEX
    )
    mirror = FulltextCandidate(
        url=epmc_mirror_url(CASE_1), kind=ArtifactKind.PDF, source=Source.EUROPE_PMC
    )
    ordered = _order_candidates([challenged, mirror], ArtifactKind.PDF)

    # OpenAlex normally outranks Europe PMC; being challenged outweighs that.
    assert [c.url for c in ordered] == [mirror.url, challenged.url]
    # Deprioritised, never dropped — the guard may be lifted by the provider.
    assert challenged.url in {c.url for c in ordered}


def test_e_the_only_candidate_is_still_tried_when_it_is_challenged():
    challenged = FulltextCandidate(
        url=pmc_pdf_url(CASE_1), kind=ArtifactKind.PDF, source=Source.OPENALEX
    )
    assert _order_candidates([challenged], ArtifactKind.PDF) == [challenged]


def test_e_the_fallback_pass_does_not_repeat_a_location(
    config, store, harvester_factory, query
):
    """Unpaywall usually re-offers what OpenAlex already gave; downloading twice
    cannot succeed and spends requests on a host that may be throttling us."""
    from mocks import unpaywall_record

    providers = providers_for(CASE_3, doi_search_blind=True)
    # Every route closed, so the fallback pass actually runs.
    providers.script_route(
        f"file:/epmc-{CASE_3['pmcid']}.pdf",
        [Behavior(status=500, content=b'{"error":"unavailable"}')] * 8,
    )
    providers.unpaywall_by_doi = {
        CASE_3_DOI: unpaywall_record(doi=CASE_3_DOI, pdf_url=pmc_pdf_url(CASE_3))
    }
    harvester_factory(providers).harvest(query)

    document_id = document_id_for_doi(CASE_3_DOI)
    tried = [a["url"] for a in _attempts(store, document_id) if a["operation"] == "acquire.pdf"]
    assert tried.count(pmc_pdf_url(CASE_3)) == 1


# ================================================= F. the run summary tells the truth


def test_f_europe_pmc_appears_among_the_providers_used(config, store, harvester_factory, query):
    providers = providers_for(CASE_2)
    result = harvester_factory(providers).harvest(query)

    used = store.providers_for_run(result.run_id)
    assert Source.EUROPE_PMC.value in used
    assert Source.OPENALEX.value in used


def test_f_europe_pmc_appears_even_when_it_knows_nothing(
    config, store, harvester_factory, query
):
    """Consulted and empty-handed is participation, and the operator should see it."""
    providers = MockProviders(
        works=[openalex_work(1, pdf_url=f"{FILES_BASE}/W2000001.pdf")],
        epmc_by_doi={},
        files={"/W2000001.pdf": make_pdf_bytes()},
    )
    result = harvester_factory(providers).harvest(query)

    assert Source.EUROPE_PMC.value in store.providers_for_run(result.run_id)


# ----------------------------------------------------------------------- helpers


def _config():
    from harvester.config import Config

    return Config.load(
        overrides={"europe_pmc.base_url": EPMC_BASE, "europe_pmc.requests_per_second": 1000}
    )


def _client(providers: MockProviders):
    from harvester.http import ProviderClient

    cfg = _config()
    return ProviderClient(
        "europe_pmc",
        cfg.europe_pmc,
        cfg.retry,
        user_agent="OpenAccessAPIHarvester/1.0 (test)",
        transport=providers.transport,
        sleeper=lambda _s: None,
    )


def _attempts(store, document_id: str) -> list[dict]:
    rows = store.connection.execute(
        "SELECT operation, source, ok, error_category, url FROM attempts "
        "WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()
    return [dict(row) for row in rows]
