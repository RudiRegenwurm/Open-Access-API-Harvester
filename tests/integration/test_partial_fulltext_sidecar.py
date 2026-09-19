"""A document with validated XML but no PDF stays failed — and stays findable.

Observed in the roll-out smoke runs: both PDF locations were rejected while Europe PMC
served a valid full-text XML. The document correctly ended FAILED_PERMANENT (a PDF is
required for COMPLETED, MASTER_SPEC section 16), but the sidecar is only written on the
success path, so the validated XML sat in the corpus with no ``<document_id>.json``.
``list_corpus_documents`` reads the corpus *through* the sidecars, which made the file
unreachable for every downstream consumer while ``harvester verify`` still reported OK.

Completion semantics are unchanged here. What changes is that artifacts which were
downloaded, validated and hashed are described honestly instead of being lost quietly.
"""

from __future__ import annotations

import json
from pathlib import Path

from harvester.identity import document_id_for_doi
from harvester.models import ArtifactKind, DocumentStatus
from harvester.reporting import render_summary
from harvester.storage import list_corpus_documents, orphan_artifacts
from harvester.verify import verify_corpus
from mocks import (
    Behavior,
    MockProviders,
    epmc_result,
    make_html_bytes,
    make_pdf_bytes,
    openalex_work,
)

DOI = "10.1234/mock.0001"
DOCUMENT_ID = document_id_for_doi(DOI)


def xml_only_providers(**overrides) -> MockProviders:
    """OpenAlex offers a location that is not a PDF; Europe PMC serves the XML."""
    files = {"/W2000001.pdf": make_html_bytes()}
    files.update(overrides.pop("files", {}))
    return MockProviders(
        works=[openalex_work(1)],
        files=files,
        epmc_by_doi={DOI: epmc_result(doi=DOI)},
        **overrides,
    )


def read_sidecar(config) -> dict:
    path = Path(config.storage_root) / f"{DOCUMENT_ID}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_xml_only_document_keeps_its_failed_status(config, store, harvester_factory, query):
    """Completion semantics are untouched: no PDF, no COMPLETED."""
    result = harvester_factory(xml_only_providers()).harvest(query)

    assert store.get_document_status(DOCUMENT_ID) is DocumentStatus.FAILED_PERMANENT
    assert result.stats.completed == 0
    assert result.stats.failed_permanent == 1
    artifacts = store.get_artifacts(DOCUMENT_ID)
    assert ArtifactKind.PDF.value not in artifacts
    assert ArtifactKind.XML.value in artifacts


def test_xml_only_document_gets_an_honest_sidecar(config, store, harvester_factory, query):
    harvester_factory(xml_only_providers()).harvest(query)

    root = Path(config.storage_root)
    assert (root / f"{DOCUMENT_ID}.xml").is_file()
    assert (root / f"{DOCUMENT_ID}.json").is_file()
    assert not (root / f"{DOCUMENT_ID}.pdf").exists()

    sidecar = read_sidecar(config)
    # The real status, never a status the document did not reach.
    assert sidecar["document_status"] == DocumentStatus.FAILED_PERMANENT.value
    assert sidecar["harvest"]["status"] == DocumentStatus.FAILED_PERMANENT.value
    # Only what was actually validated.
    assert set(sidecar["artifacts"]) == {"xml"}
    xml = sidecar["artifacts"]["xml"]
    assert xml["filename"] == f"{DOCUMENT_ID}.xml"
    assert xml["kind"] == "xml"
    assert len(xml["sha256"]) == 64
    assert xml["size_bytes"] > 0
    assert xml["source"] == "europe_pmc"
    # The XML is the document's primary artifact when there is no PDF.
    assert sidecar["file_path"] == f"{DOCUMENT_ID}.xml"
    assert sidecar["provenance"]["acquired_via"] == "europe_pmc"
    assert sidecar["provenance"]["retrieved_at"]
    assert sidecar["doi"] == DOI

    # The sidecar describes the same bytes the state store recorded.
    recorded = store.get_artifacts(DOCUMENT_ID)[ArtifactKind.XML.value]
    assert xml["sha256"] == recorded.sha256
    assert xml["size_bytes"] == recorded.size_bytes


def test_document_status_is_stated_at_the_top_level_and_is_not_oa_status(
    config, store, harvester_factory, query
):
    """The roll-out finding: ``oa_status`` was the only top-level status in the file.

    ``oa_status: "gold"`` describes the article's Open-Access standing and is true here.
    It says nothing about whether this harvest worked, and a reader scanning the top
    level had nothing else to go on. Both are now present, named apart, and neither
    borrows the other's meaning.
    """
    harvester_factory(xml_only_providers()).harvest(query)
    sidecar = read_sidecar(config)

    # The three together are the contract an external consumer reads: the format
    # announces the field, the field states the harvest outcome, and the Open-Access
    # status of the article sits beside it without being mistaken for it.
    assert sidecar["schema_version"] == "1.1"
    assert sidecar["document_status"] == "FAILED_PERMANENT"
    # Unchanged, and still the bibliographic value it always was.
    assert sidecar["oa_status"] == "gold"
    assert sidecar["oa_status_source"] == "openalex"

    # The sidecar states the status the state store actually persisted.
    assert sidecar["document_status"] == store.get_document_status(DOCUMENT_ID).value


def test_a_retryable_failure_states_failed_retryable_in_its_sidecar(
    config, store, harvester_factory, query
):
    """The same document one attempt earlier: still retryable, and it says so."""
    providers = xml_only_providers(files={})
    # The only PDF location is down. 503 is retryable, so the transport exhausts its
    # attempts on it and the document keeps a retryable failure.
    providers.script_route("file:/W2000001.pdf", [Behavior(status=503) for _ in range(3)])
    result = harvester_factory(providers).harvest(query)

    assert store.get_document_status(DOCUMENT_ID) is DocumentStatus.FAILED_RETRYABLE
    assert result.stats.failed_retryable == 1
    assert result.stats.partial_fulltext == 1

    sidecar = read_sidecar(config)
    assert sidecar["document_status"] == DocumentStatus.FAILED_RETRYABLE.value
    assert sidecar["document_status"] == store.get_document_status(DOCUMENT_ID).value
    assert set(sidecar["artifacts"]) == {"xml"}


def test_a_completed_document_still_states_completed(
    config, store, harvester_factory, query
):
    """No regression on the success path: the field is not failure-only."""
    providers = MockProviders(
        works=[openalex_work(1)],
        files={"/W2000001.pdf": make_pdf_bytes()},
        epmc_by_doi={DOI: epmc_result(doi=DOI)},
    )
    harvester_factory(providers).harvest(query)

    assert store.get_document_status(DOCUMENT_ID) is DocumentStatus.COMPLETED
    sidecar = read_sidecar(config)
    assert sidecar["document_status"] == DocumentStatus.COMPLETED.value
    assert sidecar["harvest"]["status"] == DocumentStatus.COMPLETED.value
    # Everything the sidecar said before this field existed still holds.
    assert set(sidecar["artifacts"]) == {"pdf", "xml"}
    assert sidecar["file_path"] == f"{DOCUMENT_ID}.pdf"
    assert sidecar["provenance"]["acquired_via"] == "openalex"
    assert sidecar["oa_status"] == "gold"


def test_list_corpus_documents_finds_the_xml_only_document(
    config, store, harvester_factory, query
):
    harvester_factory(xml_only_providers()).harvest(query)
    assert list_corpus_documents(Path(config.storage_root)) == [DOCUMENT_ID]


def test_verify_accepts_the_xml_only_document(config, store, harvester_factory, query):
    harvester_factory(xml_only_providers()).harvest(query)

    report = verify_corpus(config, store, deep=True)
    assert report.problems == []
    assert report.orphans == []
    assert report.ok is True
    assert report.documents_in_corpus == 1
    # It is not a completed document, so it is not counted as one.
    assert report.completed_documents == 0
    # And the XML is no longer a file nothing accounts for.
    assert orphan_artifacts(Path(config.storage_root), {DOCUMENT_ID}) == []


def test_verify_still_reports_a_completed_document_without_a_pdf(
    config, store, harvester_factory, query
):
    """The pre-existing rule is not weakened: COMPLETED without a PDF stays a problem."""
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    harvester_factory(providers).harvest(query)
    assert store.get_document_status(DOCUMENT_ID) is DocumentStatus.COMPLETED

    # State now claims a completed document whose PDF it no longer records.
    store.delete_artifact(DOCUMENT_ID, ArtifactKind.PDF)

    report = verify_corpus(config, store)
    assert report.ok is False
    assert any(
        problem["problem"] == "COMPLETED document has no PDF artifact recorded"
        for problem in report.problems
    )


def test_a_failure_with_no_artifacts_writes_no_sidecar(
    config, store, harvester_factory, query
):
    """Only documents that actually hold artifacts are published to the corpus."""
    providers = MockProviders(works=[openalex_work(1)], files={})  # every location 404s
    harvester_factory(providers).harvest(query)

    assert store.get_document_status(DOCUMENT_ID) is DocumentStatus.FAILED_PERMANENT
    assert store.get_artifacts(DOCUMENT_ID) == {}
    assert not (Path(config.storage_root) / f"{DOCUMENT_ID}.json").exists()
    assert list_corpus_documents(Path(config.storage_root)) == []


def test_retry_failed_reuses_the_xml_and_only_retries_the_pdf(
    config, store, harvester_factory, query
):
    """``harvester retry-failed`` re-queues the document; the XML is not fetched again."""
    harvester_factory(xml_only_providers()).harvest(query)
    xml_before = store.get_artifacts(DOCUMENT_ID)[ArtifactKind.XML.value].sha256

    # What the CLI's retry-failed command does to a FAILED_PERMANENT document.
    assert store.reset_documents_for_retry([DOCUMENT_ID]) == 1
    assert store.get_document_status(DOCUMENT_ID) is DocumentStatus.QUEUED

    # Second run: the PDF location now serves a real PDF.
    second = xml_only_providers(files={"/W2000001.pdf": make_pdf_bytes()})
    result = harvester_factory(second).harvest(query)

    assert store.get_document_status(DOCUMENT_ID) is DocumentStatus.COMPLETED
    assert result.stats.completed == 1
    assert result.stats.partial_fulltext == 0
    # The PDF was retried...
    assert second.count("file:/W2000001.pdf") == 1
    # ...and the XML already in hand was not downloaded a second time.
    assert second.count("epmc.fulltextxml") == 0
    assert store.get_artifacts(DOCUMENT_ID)[ArtifactKind.XML.value].sha256 == xml_before

    sidecar = read_sidecar(config)
    assert sidecar["harvest"]["status"] == DocumentStatus.COMPLETED.value
    assert set(sidecar["artifacts"]) == {"pdf", "xml"}
    assert sidecar["file_path"] == f"{DOCUMENT_ID}.pdf"


def test_report_explains_the_partial_full_text(config, store, harvester_factory, query):
    """``downloaded=1`` next to ``completed=0`` must be explained, not just observed."""
    result = harvester_factory(xml_only_providers()).harvest(query)

    assert result.stats.downloaded == 1
    assert result.stats.validated == 1
    assert result.stats.xml_count == 1
    assert result.stats.completed == 0
    assert result.stats.partial_fulltext == 1

    note = next(
        (n for n in result.stats.notes if "validated XML full text" in n), None
    )
    assert note is not None
    assert "no PDF" in note

    summary = render_summary(result.stats)
    assert "partial full text (XML, no PDF): 1" in summary

    # The persisted report carries the same counter, not only the in-memory stats.
    report = json.loads(Path(result.report_path).read_text(encoding="utf-8"))
    assert report["partial_fulltext"] == 1
    assert any("validated XML full text" in n for n in report["notes"])
