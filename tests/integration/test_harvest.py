"""Integration: a complete harvest against the deterministic mock providers.

Covers AC-002 (dedup), AC-007 (atomicity), AC-011 (provenance), AC-012 (SHA-256),
AC-013 (XML), AC-016 (run report), AC-023 (flat storage) and AC-024 (OA-status).
"""

from __future__ import annotations

import json
from pathlib import Path

from harvester.identity import document_id_for_doi, sha256_file
from harvester.models import DocumentStatus, RunStatus
from harvester.providers.openalex import OpenAlexQuery
from mocks import (
    FILES_BASE,
    MockProviders,
    epmc_result,
    make_pdf_bytes,
    make_xml_bytes,
    openalex_work,
    unpaywall_record,
)


def test_complete_harvest_produces_the_flat_a1_corpus(config, store, harvester_factory, query):
    """AC-023: sibling <document_id>.pdf / .json artifacts in one flat directory."""
    providers = MockProviders(
        works=[openalex_work(i) for i in range(1, 4)],
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, 4)},
    )
    harvester = harvester_factory(providers)
    result = harvester.harvest(query)

    assert result.status is RunStatus.COMPLETED
    assert result.stats.records_discovered == 3
    assert result.stats.completed == 3
    assert result.stats.pdf_count == 3
    assert result.stats.failed_permanent == 0

    root = Path(config.storage_root)
    # Flat: every file is a direct child, no per-document directory anywhere.
    assert all(path.is_file() for path in root.iterdir())
    assert sorted(p.suffix for p in root.iterdir()) == [".json"] * 3 + [".pdf"] * 3

    for index in range(1, 4):
        document_id = document_id_for_doi(f"10.1234/mock.{index:04d}")
        assert (root / f"{document_id}.pdf").exists()
        assert (root / f"{document_id}.json").exists()


def test_sidecar_is_mandatory_and_separates_the_four_metadata_groups(
    config, store, harvester_factory, query
):
    """AC-011/AC-012/AC-024 and MASTER_SPEC section 20."""
    pdf = make_pdf_bytes()
    providers = MockProviders(
        works=[openalex_work(1, oa_status="gold")], files={"/W2000001.pdf": pdf}
    )
    harvester_factory(providers).harvest(query)

    document_id = document_id_for_doi("10.1234/mock.0001")
    sidecar = json.loads((Path(config.storage_root) / f"{document_id}.json").read_text("utf-8"))

    # bibliographic
    assert sidecar["doi"] == "10.1234/mock.0001"
    assert sidecar["title"] == "Mock Open Access Article 1"
    assert sidecar["authors"] == ["Ada Lovelace", "Alan Turing"]
    assert sidecar["abstract"] == "Moral psychology studies human judgement"
    assert sidecar["domain_tags"][0] == "Social Sciences"
    # AC-024: oa_status preserved with its source
    assert sidecar["oa_status"] == "gold"
    assert sidecar["oa_status_source"] == "openalex"

    # artifact metadata — AC-012: every successful artifact has a SHA-256
    artifact = sidecar["artifacts"]["pdf"]
    assert artifact["sha256"] == sha256_file(Path(config.storage_root) / f"{document_id}.pdf")
    assert artifact["size_bytes"] == len(pdf)
    assert artifact["filename"] == f"{document_id}.pdf"

    # AC-011: provenance records acquisition source and resolved URL
    provenance = sidecar["provenance"]
    assert provenance["discovered_via"] == ["openalex"]
    assert provenance["acquired_via"] == "openalex"
    assert provenance["resolved_url"].endswith("/W2000001.pdf")
    assert provenance["http_status"] == 200
    assert provenance["retrieved_at"]
    # SPEC_PATCH section 4: cross_checked_via, never an undefined verified_via
    assert "cross_checked_via" in provenance
    assert "verified_via" not in provenance

    # harvest state
    assert sidecar["harvest"]["status"] == "COMPLETED"
    assert sidecar["harvest"]["run_id"]


def test_ac016_every_run_ends_with_a_machine_readable_summary(
    config, store, harvester_factory, query
):
    """AC-016."""
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    result = harvester_factory(providers).harvest(query)

    assert result.report_path is not None and result.report_path.exists()
    report = json.loads(result.report_path.read_text("utf-8"))
    for field in (
        "run_id",
        "started_at",
        "finished_at",
        "duration_seconds",
        "records_discovered",
        "records_normalized",
        "duplicates",
        "queued",
        "attempted",
        "downloaded",
        "validated",
        "completed",
        "skipped",
        "failed_retryable",
        "failed_permanent",
        "pdf_count",
        "xml_count",
        "bytes_downloaded",
        "retry_count",
    ):
        assert field in report, f"run summary is missing {field}"
    assert report["status"] == "COMPLETED"
    assert report["bytes_downloaded"] > 0


def test_ac002_duplicate_dois_across_pages_collapse_into_one_document(
    config, store, harvester_factory, query
):
    """AC-002: a repeated DOI is one logical document and one download."""
    duplicate = openalex_work(1)
    works = [duplicate, openalex_work(2), dict(duplicate, id="https://openalex.org/W9999999")]
    providers = MockProviders(
        works=works,
        files={"/W2000001.pdf": make_pdf_bytes(), "/W2000002.pdf": make_pdf_bytes()},
    )
    result = harvester_factory(providers).harvest(query)

    assert result.stats.records_discovered == 3
    assert result.stats.duplicates == 1
    assert len(store.all_document_ids()) == 2
    assert result.stats.completed == 2
    assert providers.count("file:/W2000001.pdf") == 1  # downloaded once, not twice


def test_ac013_europe_pmc_xml_is_acquired_and_validated(
    config, store, harvester_factory, query
):
    """AC-013: available Europe PMC XML is stored and validated alongside the PDF."""
    doi = "10.1234/mock.0001"
    providers = MockProviders(
        works=[openalex_work(1)],
        files={"/W2000001.pdf": make_pdf_bytes()},
        epmc_by_doi={doi: epmc_result(doi=doi)},
    )
    result = harvester_factory(providers).harvest(query)

    document_id = document_id_for_doi(doi)
    xml_path = Path(config.storage_root) / f"{document_id}.xml"
    assert xml_path.exists()
    assert xml_path.read_bytes() == make_xml_bytes()
    assert result.stats.xml_count == 1

    sidecar = json.loads((Path(config.storage_root) / f"{document_id}.json").read_text("utf-8"))
    assert sidecar["artifacts"]["xml"]["sha256"] == sha256_file(xml_path)
    assert sidecar["artifacts"]["xml"]["source"] == "europe_pmc"


def test_missing_xml_does_not_fail_a_valid_pdf_harvest(config, store, harvester_factory, query):
    """MASTER_SPEC section 16: default policy is PDF required, XML optional."""
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    result = harvester_factory(providers).harvest(query)
    assert result.stats.completed == 1
    assert result.stats.xml_count == 0


def test_xml_required_policy_fails_a_document_without_xml(
    config, store, harvester_factory, query
):
    config.xml_policy = "required"
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    result = harvester_factory(providers).harvest(query)
    assert result.stats.completed == 0
    assert result.stats.failed_permanent + result.stats.failed_retryable == 1


def test_ac025_cross_check_evidence_reaches_the_sidecar(config, store, harvester_factory, query):
    doi = "10.1234/mock.0001"
    providers = MockProviders(
        works=[openalex_work(1)],
        files={"/W2000001.pdf": make_pdf_bytes()},
        epmc_by_doi={doi: epmc_result(doi=doi, title="A slightly different title")},
    )
    harvester_factory(providers).harvest(query)

    document_id = document_id_for_doi(doi)
    sidecar = json.loads((Path(config.storage_root) / f"{document_id}.json").read_text("utf-8"))
    assert sidecar["provenance"]["cross_checked_via"] == ["europe_pmc"]
    check = sidecar["provenance"]["cross_checks"][0]
    assert check["matched_on"] == ["doi"]
    assert check["identifiers"]["pmcid"] == "PMC1000001"
    assert check["metadata_differences"]["title"]["europe_pmc"] == "A slightly different title"


def test_ac010_unpaywall_fallback_is_used_when_the_primary_pdf_is_unusable(
    config, store, harvester_factory, query
):
    """AC-010: an unusable primary location falls back to the configured sources."""
    doi = "10.1234/mock.0001"
    providers = MockProviders(
        works=[openalex_work(1)],
        files={
            "/W2000001.pdf": b"<html>not a pdf</html>",
            "/unpaywall.pdf": make_pdf_bytes(),
        },
        unpaywall_by_doi={
            doi: unpaywall_record(doi=doi, pdf_url=f"{FILES_BASE}/unpaywall.pdf")
        },
    )
    result = harvester_factory(providers).harvest(query)

    assert result.stats.completed == 1
    document_id = document_id_for_doi(doi)
    sidecar = json.loads((Path(config.storage_root) / f"{document_id}.json").read_text("utf-8"))
    assert sidecar["provenance"]["acquired_via"] == "unpaywall"
    assert sidecar["provenance"]["resolved_url"].endswith("/unpaywall.pdf")


def test_unpaywall_is_not_consulted_when_the_primary_location_works(
    config, store, harvester_factory, query
):
    """MASTER_SPEC section 43: a fallback resolver, not part of the happy path."""
    doi = "10.1234/mock.0001"
    providers = MockProviders(
        works=[openalex_work(1)],
        files={"/W2000001.pdf": make_pdf_bytes()},
        unpaywall_by_doi={doi: unpaywall_record(doi=doi, pdf_url=f"{FILES_BASE}/other.pdf")},
    )
    harvester_factory(providers).harvest(query)
    assert providers.count("unpaywall.doi") == 0


def test_document_with_no_oa_location_fails_with_a_structured_reason(
    config, store, harvester_factory, query
):
    work = openalex_work(1)
    work["best_oa_location"] = None
    work["primary_location"] = {"is_oa": False}
    work["open_access"] = {"is_oa": False, "oa_status": "closed", "oa_url": None}
    providers = MockProviders(works=[work])
    result = harvester_factory(providers).harvest(query)

    assert result.stats.completed == 0
    assert result.stats.failed_permanent == 1
    failures = store.failures_for_run(result.run_id)
    assert any(f["category"] == "NO_OA_LOCATION" for f in failures)
    # No silent loss: no artifact and no sidecar claiming success.
    assert list(Path(config.storage_root).glob("*.pdf")) == []


def test_dry_run_discovers_but_writes_no_artifacts(config, store, harvester_factory, query):
    """MASTER_SPEC section 35."""
    providers = MockProviders(
        works=[openalex_work(i) for i in range(1, 4)],
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, 4)},
    )
    result = harvester_factory(providers).harvest(query, dry_run=True)

    assert result.stats.records_discovered == 3
    assert result.stats.completed == 0
    assert result.stats.downloaded == 0
    root = Path(config.storage_root)
    assert not root.exists() or list(root.iterdir()) == []
    # State is still recorded, so the run can be continued later.
    assert len(store.all_document_ids()) == 3


def test_limit_bounds_discovery(config, store, harvester_factory, query):
    providers = MockProviders(
        works=[openalex_work(i) for i in range(1, 21)],
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, 21)},
        page_size=5,
    )
    result = harvester_factory(providers).harvest(query, limit=7)
    assert result.stats.records_discovered == 7
    assert len(store.all_document_ids()) == 7


def test_ac007_no_partial_artifact_is_left_under_the_final_filename(
    config, store, harvester_factory, query
):
    """AC-007: a rejected download leaves neither a final file nor a stray .part."""
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": b"%PDF-1.4 broken, no trailer"}
    )
    result = harvester_factory(providers).harvest(query)

    root = Path(config.storage_root)
    assert result.stats.completed == 0
    assert list(root.glob("*.pdf")) == []
    assert list(root.glob("*.part")) == []
    assert list(root.glob("*.json")) == []


def test_documents_reach_completed_state_in_the_store(config, store, harvester_factory, query):
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    harvester_factory(providers).harvest(query)
    document_id = document_id_for_doi("10.1234/mock.0001")
    assert store.get_document_status(document_id) is DocumentStatus.COMPLETED
    assert store.get_artifacts(document_id)["pdf"].http_status == 200


def test_secrets_never_reach_state_reports_or_sidecars(
    config, store, harvester_factory, query
):
    """AC-017 end to end."""
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    result = harvester_factory(providers).harvest(query)

    haystack = json.dumps(json.loads(result.report_path.read_text("utf-8")))
    document_id = document_id_for_doi("10.1234/mock.0001")
    haystack += (Path(config.storage_root) / f"{document_id}.json").read_text("utf-8")
    run = store.get_run(result.run_id)
    haystack += json.dumps(run.config)

    assert config.openalex.api_key not in haystack
    assert config.contact_email not in haystack
