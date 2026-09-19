"""Controlled real-world smoke tests (MASTER_SPEC section 53, "Definition of Done").

Deselected by default (``-m 'not live'`` in ``pyproject.toml``). Run explicitly with::

    pytest -m live

These tests contact the real providers. They deliberately keep the request count in
the single digits and use ``--limit``-style bounds so that running them costs almost
nothing against any provider's daily budget.

Credential requirements:

* ``HARVESTER_OPENALEX_API_KEY`` — required for the production-auth test. Without it
  the keyless smoke path is exercised instead and the auth test is skipped.
* ``HARVESTER_CONTACT_EMAIL`` — required by Unpaywall's terms. The Unpaywall test is
  skipped when it is absent; the harvester must never invent a contact address.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harvester.config import Config
from harvester.http import ClientPool
from harvester.identity import normalize_doi
from harvester.models import RunStatus
from harvester.orchestrator import Harvester
from harvester.providers.europepmc import EuropePmcAdapter
from harvester.providers.openalex import OpenAlexAdapter, OpenAlexQuery
from harvester.providers.unpaywall import UnpaywallAdapter
from harvester.state import StateStore
from harvester.verify import verify_corpus

pytestmark = pytest.mark.live

API_KEY = os.environ.get("HARVESTER_OPENALEX_API_KEY")
CONTACT_EMAIL = os.environ.get("HARVESTER_CONTACT_EMAIL")

#: A well-known Open Access article with Europe PMC full text.
KNOWN_DOI = "10.1371/journal.pone.0123456"
#: "Moral Psychology" — the Topic used throughout the documentation examples.
TOPIC_ID = "T10159"


def live_config(tmp_path: Path) -> Config:
    return Config.load(
        env={},
        overrides={
            "storage_root": str(tmp_path / "corpus"),
            "state_db": str(tmp_path / "state.sqlite3"),
            "reports_dir": str(tmp_path / "reports"),
            "contact_email": CONTACT_EMAIL,
            "openalex.api_key": API_KEY,
            "openalex.allow_keyless": API_KEY is None,
            "openalex.per_page": 5,
            "unpaywall.enabled": CONTACT_EMAIL is not None,
            "log_level": "INFO",
        },
    )


# ------------------------------------------------------------------- OpenAlex


def test_live_openalex_topic_discovery(tmp_path: Path):
    """Topic filters, cursor paging and budget headers behave as documented."""
    config = live_config(tmp_path)
    with ClientPool(config) as clients:
        adapter = OpenAlexAdapter(clients.openalex, config.openalex)
        page = adapter.discover_page(OpenAlexQuery(topic_id=TOPIC_ID))

        assert page.records, "the Topic filter returned no Open Access works"
        assert page.total_count and page.total_count > 0
        assert page.next_cursor

        document, record = page.records[0]
        assert document.doi and normalize_doi(document.doi) == document.doi
        assert document.is_oa is True
        assert document.topics, "primary_topic/topics must be present"
        assert document.domain_tags, "domain -> field -> subfield -> topic tags"

        # The credit budget is observable, which is what suspension relies on.
        assert clients.openalex.budget.limit is not None
        assert clients.openalex.budget.remaining is not None


@pytest.mark.skipif(API_KEY is None, reason="HARVESTER_OPENALEX_API_KEY is not configured")
def test_live_openalex_production_authentication(tmp_path: Path):
    """SPEC_PATCH section 2: normal production operation uses the configured key."""
    config = live_config(tmp_path)
    assert config.openalex.api_key
    with ClientPool(config) as clients:
        adapter = OpenAlexAdapter(clients.openalex, config.openalex)
        page = adapter.discover_page(OpenAlexQuery(topic_id=TOPIC_ID))
        assert page.records
        # A keyed caller gets the full daily allowance, not the tiny anonymous one.
        assert (clients.openalex.budget.limit or 0) > 1000


def test_live_openalex_abstract_is_reconstructed_not_invented(tmp_path: Path):
    config = live_config(tmp_path)
    with ClientPool(config) as clients:
        adapter = OpenAlexAdapter(clients.openalex, config.openalex)
        page = adapter.discover_page(OpenAlexQuery(topic_id=TOPIC_ID))
        for document, record in page.records:
            supplied = record.extra.get("abstract_available")
            if supplied:
                assert document.abstract and len(document.abstract.split()) > 3
            else:
                assert document.abstract is None


# ----------------------------------------------------------------- Europe PMC


def test_live_europe_pmc_cross_check_and_fulltext(tmp_path: Path):
    """Identity cross-check, PMCID mapping and OA full-text evidence."""
    config = live_config(tmp_path)
    with ClientPool(config) as clients:
        adapter = EuropePmcAdapter(clients.europe_pmc, config.europe_pmc)
        record = adapter.lookup_by_doi(KNOWN_DOI)
        assert record is not None, f"Europe PMC does not know {KNOWN_DOI}"
        assert record.doi == KNOWN_DOI
        assert record.identifiers.get("pmcid", "").startswith("PMC")
        assert record.identifiers.get("pmid")
        assert record.is_oa is True

        check = adapter.cross_check(KNOWN_DOI, record, {"identifiers": {}})
        assert "doi" in check.matched_on
        assert check.fulltext_availability["is_open_access"] is True

        kinds = {candidate.kind.value for candidate in record.candidates}
        assert "xml" in kinds, "an OA article should expose fullTextXML"
        assert "pdf" in kinds, "an OA article should expose a PDF location"


def test_live_europe_pmc_fulltext_xml_is_well_formed(tmp_path: Path):
    from harvester.validation import validate_xml

    config = live_config(tmp_path)
    with ClientPool(config) as clients:
        adapter = EuropePmcAdapter(clients.europe_pmc, config.europe_pmc)
        record = adapter.lookup_by_doi(KNOWN_DOI)
        xml_candidates = [c for c in record.candidates if c.kind.value == "xml"]
        assert xml_candidates

        response = clients.europe_pmc.request("GET", xml_candidates[0].url)
        path = tmp_path / "live.xml"
        path.write_bytes(response.content)
        result = validate_xml(path, min_size_bytes=256)
        assert result.sha256 and result.size_bytes > 1000


# ------------------------------------------------------------------ Unpaywall


@pytest.mark.skipif(
    CONTACT_EMAIL is None, reason="HARVESTER_CONTACT_EMAIL is not configured"
)
def test_live_unpaywall_resolves_oa_locations(tmp_path: Path):
    config = live_config(tmp_path)
    with ClientPool(config) as clients:
        adapter = UnpaywallAdapter(
            clients.unpaywall, config.unpaywall, contact_email=CONTACT_EMAIL
        )
        record = adapter.resolve(KNOWN_DOI)
        assert record is not None
        assert record.doi == KNOWN_DOI
        assert record.is_oa is True
        assert record.oa_status  # preserved, with its source recorded downstream
        assert record.abstract is None  # never invented


# ------------------------------------------------------------ full pipeline


def test_live_bounded_end_to_end_harvest(tmp_path: Path):
    """A real, deliberately tiny harvest: discover, acquire, validate, verify."""
    config = live_config(tmp_path)
    config.validate(require_openalex=True)

    with StateStore(config.state_db) as store, ClientPool(config) as clients:
        harvester = Harvester(config, store, clients)
        result = harvester.harvest(OpenAlexQuery(topic_id=TOPIC_ID), limit=3)

    assert result.status in (RunStatus.COMPLETED, RunStatus.SUSPENDED)
    assert result.stats.records_discovered <= 3
    assert result.report_path and result.report_path.exists()

    root = Path(config.storage_root)
    for pdf in root.glob("*.pdf"):
        sidecar = root / f"{pdf.stem}.json"
        assert sidecar.exists(), "the JSON sidecar is mandatory"
        assert pdf.read_bytes().startswith(b"%PDF-")

    with StateStore(config.state_db) as store:
        report = verify_corpus(config, store, deep=True)
    assert report.problems == [], report.problems


def test_live_harvest_is_idempotent(tmp_path: Path):
    """Re-running the same live query downloads nothing a second time."""
    config = live_config(tmp_path)
    query = OpenAlexQuery(topic_id=TOPIC_ID)

    with StateStore(config.state_db) as store, ClientPool(config) as clients:
        first = Harvester(config, store, clients).harvest(query, limit=2)
    if first.stats.completed == 0:
        pytest.skip("no document completed; nothing to prove idempotent")

    with StateStore(config.state_db) as store, ClientPool(config) as clients:
        second = Harvester(config, store, clients).harvest(query, limit=2)

    assert second.stats.downloaded == 0
    assert second.stats.already_complete >= 1
    assert len(list(Path(config.storage_root).glob("*.pdf"))) == first.stats.completed
