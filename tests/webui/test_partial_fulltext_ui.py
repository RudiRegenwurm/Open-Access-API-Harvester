"""A document with XML but no PDF must not be presented as having no full text.

Reported from the roll-out: a run detail page showed the badge "Full text unavailable"
and the counter "Unavailable: 1" beside an "XML" chip and the sentence "0 PDF and 1 XML
file(s) stored". Both halves were true — the document really is FAILED_PERMANENT because
COMPLETED requires a PDF, and the XML really is in the corpus — and together they read
as a defect.

Presentation only. No persisted status changes, no new document state; the counters are
derived from the ``has_pdf``/``has_xml`` information every row already carried.

The corpus these tests harvest holds all three cases at once, so mutual exclusivity and
the group total are asserted against a mixture rather than against a single shape:

===========  =======================  =========================
work         what the providers offer resulting group
===========  =======================  =========================
mock.0001    a valid PDF              full_text_available
mock.0002    a valid PDF              full_text_available
mock.0003    HTML, plus EPMC XML      partial_full_text
mock.0004    nothing (404)            unavailable
===========  =======================  =========================
"""

from __future__ import annotations

import json

import pytest

from conftest import wait_for_idle
from mocks import (
    MockProviders,
    epmc_result,
    make_html_bytes,
    make_pdf_bytes,
    openalex_work,
)
from test_ux_workflow import (  # the existing node harness, reused rather than rebuilt
    app_js,
    needs_node,
    run_node,
    slice_between,
)

QUERY = {"search": "moral psychology", "limit": 4}

PARTIAL_DOI = "10.1234/mock.0003"


@pytest.fixture
def ui_providers() -> MockProviders:
    """Overrides the shared fixture: a corpus with one of each acquisition outcome."""
    return MockProviders(
        works=[openalex_work(i) for i in range(1, 5)],
        files={
            "/W2000001.pdf": make_pdf_bytes(),
            "/W2000002.pdf": make_pdf_bytes(),
            # Not a PDF, so this document falls back to the Europe PMC XML...
            "/W2000003.pdf": make_html_bytes(),
            # ...and this one has nowhere left to go.
        },
        epmc_by_doi={PARTIAL_DOI: epmc_result(doi=PARTIAL_DOI)},
    )


@pytest.fixture
def harvested(ui):
    """One finished harvest over the mixed corpus."""
    ui.post("/api/harvest", {**QUERY, "dry_run": False})
    return wait_for_idle(ui)


def pill_harness(body: str) -> str:
    """Everything ``docPill`` needs, sliced out of the served asset."""
    return "".join((
        slice_between(body, "function esc(value) {", "function choiceOptions("),
        slice_between(body, "const DOC_STATE = {", "function pill("),
    ))


GROUPS = (
    "full_text_available",
    "partial_full_text",
    "unavailable",
    "retryable",
    "in_progress",
)


# ============================================================== document badge


@needs_node
def test_a_settled_document_with_xml_and_no_pdf_reads_as_partial(ui, tmp_path):
    result = run_node(
        tmp_path,
        pill_harness(app_js(ui)),
        """
console.log(JSON.stringify({
  permanent: docPill('FAILED_PERMANENT', { pdf: false, xml: true }),
  skipped:   docPill('SKIPPED', { pdf: false, xml: true }),
  retryable: docPill('FAILED_RETRYABLE', { pdf: false, xml: true }),
}));
""",
    )
    for key in ("permanent", "skipped"):
        assert "Partial full text" in result[key]
        assert "Full text unavailable" not in result[key]
    # The persisted state is unchanged and still one hover away.
    assert 'title="FAILED_PERMANENT"' in result["permanent"]

    # A document that will be tried again has no final outcome yet, so it keeps its
    # retry label — the same precedence the counters apply, so the badge and the
    # "Can be retried" figure above it describe the document identically.
    assert "can retry" in result["retryable"]
    assert "Partial full text" not in result["retryable"]


@needs_node
def test_a_document_with_neither_artifact_still_reads_as_unavailable(ui, tmp_path):
    result = run_node(
        tmp_path,
        pill_harness(app_js(ui)),
        """
console.log(JSON.stringify({
  none:   docPill('FAILED_PERMANENT', { pdf: false, xml: false }),
  noInfo: docPill('FAILED_PERMANENT'),
}));
""",
    )
    assert "Full text unavailable" in result["none"]
    # Called without artifact information — an older caller — nothing changes.
    assert "Full text unavailable" in result["noInfo"]


@needs_node
def test_a_document_with_a_pdf_reads_as_acquired(ui, tmp_path):
    result = run_node(
        tmp_path,
        pill_harness(app_js(ui)),
        """
console.log(JSON.stringify({
  pdfOnly: docPill('COMPLETED', { pdf: true, xml: false }),
  both:    docPill('COMPLETED', { pdf: true, xml: true }),
}));
""",
    )
    assert "Full text acquired" in result["pdfOnly"]
    assert "Full text acquired" in result["both"]
    assert "Partial" not in result["both"]


@needs_node
def test_an_unknown_state_stays_visible_with_artifact_information(ui, tmp_path):
    """The pre-existing guarantee must survive the new argument."""
    result = run_node(
        tmp_path,
        pill_harness(app_js(ui)),
        """
console.log(JSON.stringify({
  unknown:    docPill('SOMETHING_NEW'),
  unknownXml: docPill('SOMETHING_NEW', { pdf: false, xml: true }),
}));
""",
    )
    # An unrecognised state is not settled, so its artifacts do not reinterpret it —
    # and it still renders rather than disappearing.
    assert "something_new" in result["unknown"]
    assert "something_new" in result["unknownXml"]


@needs_node
def test_in_flight_documents_are_not_relabelled_by_a_partial_artifact(ui, tmp_path):
    """A document still being worked on reports progress, not a result."""
    result = run_node(
        tmp_path,
        pill_harness(app_js(ui)),
        """
console.log(JSON.stringify({
  acquiring: docPill('ACQUIRING', { pdf: false, xml: true }),
  queued:    docPill('QUEUED', { pdf: false, xml: true }),
}));
""",
    )
    assert "Downloading" in result["acquiring"]
    assert "Waiting to acquire" in result["queued"]


# =============================================================== run counters


def test_run_counters_separate_available_partial_and_unavailable(ui, harvested):
    detail = ui.get(f"/api/runs/{harvested['run_id']}")
    state = detail["outcome"]["corpus_state"]

    assert state["full_text_available"] == 2
    assert state["partial_full_text"] == 1
    assert state["unavailable"] == 1
    assert state["retryable"] == 0
    assert state["in_progress"] == 0
    assert detail["artifacts"]["pdf"] == 2
    assert detail["artifacts"]["xml"] == 1
    # The run's own frozen counter says the same about the work it did.
    assert detail["outcome"]["this_run"]["partial_full_text"] == 1


def test_the_counter_groups_are_exclusive_and_sum_to_the_document_total(ui, harvested):
    detail = ui.get(f"/api/runs/{harvested['run_id']}")
    state = detail["outcome"]["corpus_state"]

    assert sum(state[key] for key in GROUPS) == state["total"]
    assert state["total"] == sum(detail["document_counts"].values())
    assert state["total"] == detail["document_total"] == 4
    # The same grouping, computed independently for the whole corpus.
    assert sum(detail["acquisition_counts"][key] for key in GROUPS) == 4


def test_the_persisted_document_states_are_untouched(ui, harvested):
    """Presentation only: state, API and CLI still speak the same document states."""
    detail = ui.get(f"/api/runs/{harvested['run_id']}")
    assert detail["document_counts"]["COMPLETED"] == 2
    assert detail["document_counts"]["FAILED_PERMANENT"] == 2
    assert "PARTIAL" not in detail["document_counts"]
    assert "PARTIAL_FULL_TEXT" not in detail["document_counts"]

    by_doi = {d["doi"]: d for d in detail["documents"]}
    partial = by_doi[PARTIAL_DOI]
    assert partial["status"] == "FAILED_PERMANENT"
    assert partial["has_pdf"] is False
    assert partial["has_xml"] is True


def test_the_corpus_page_carries_the_same_information(ui, harvested):
    corpus = ui.get("/api/corpus")
    assert corpus["total"] == 4
    row = next(d for d in corpus["documents"] if d["doi"] == PARTIAL_DOI)
    assert row["status"] == "FAILED_PERMANENT"
    assert row["has_pdf"] is False
    assert row["has_xml"] is True
    # Reachable through the artifact filters the corpus page already offers.
    assert ui.get("/api/corpus?has_xml=true")["total"] == 1
    assert ui.get("/api/corpus?has_pdf=true")["total"] == 2


def test_the_dashboard_uses_the_same_grouping(ui, harvested):
    dashboard = ui.get("/api/dashboard")
    assert dashboard["cards"]["full_text_available"] == 2
    assert dashboard["cards"]["partial_full_text"] == 1
    assert dashboard["cards"]["pdfs"] == 2
    assert dashboard["cards"]["xmls"] == 1
    assert sum(dashboard["acquisition_counts"][key] for key in GROUPS) == 4
    # The state-machine counts are still reported unchanged beside them.
    assert dashboard["document_counts"]["FAILED_PERMANENT"] == 2


def test_a_retryable_document_is_counted_as_retryable_not_as_partial(ui, harvested):
    """Precedence, pinned: an unfinished document is not reported as a result.

    The same rule the badge applies (``SETTLED_STATES`` in ``app.js``), so the figure
    and the badge above and below each other cannot describe one document differently.
    """
    from harvester.identity import document_id_for_doi
    from harvester.models import DocumentStatus

    store = ui.server.context.store
    document_id = document_id_for_doi(PARTIAL_DOI)
    assert store.get_artifacts(document_id).keys() == {"xml"}
    store.set_status(document_id, DocumentStatus.QUEUED)
    store.set_status(document_id, DocumentStatus.ACQUIRING)
    store.set_status(document_id, DocumentStatus.FAILED_RETRYABLE)

    state = ui.get(f"/api/runs/{harvested['run_id']}")["outcome"]["corpus_state"]
    assert state["retryable"] == 1
    assert state["partial_full_text"] == 0
    assert sum(state[key] for key in GROUPS) == state["total"] == 4


def test_the_run_list_reports_the_same_state_as_the_run_page(ui, harvested):
    listed = next(
        r for r in ui.get("/api/runs")["runs"] if r["run_id"] == harvested["run_id"]
    )
    detail = ui.get(f"/api/runs/{harvested['run_id']}")
    assert listed["outcome"]["corpus_state"] == detail["outcome"]["corpus_state"]


# ============================================================= rendered views


@needs_node
def test_the_run_page_states_the_partial_counter_and_explains_it(ui, tmp_path):
    from test_ux_workflow import STAT_READER, outcome_harness

    result = run_node(
        tmp_path,
        outcome_harness(app_js(ui)) + STAT_READER,
        r"""
const run = {
  dry_run: false, discovery_seen: 1, artifacts: { pdf: 0, xml: 1 },
  document_counts: { FAILED_PERMANENT: 1 },
  outcome: {
    corpus_state: { full_text_available: 0, partial_full_text: 1, unavailable: 0,
                    retryable: 0, in_progress: 0, total: 1 },
    this_run: { newly_acquired: 0, already_available: 0, retries_attempted: 0,
                bytes_downloaded: 0, discovered: 1, duplicates: 0,
                failed_permanent: 1, failed_retryable: 0, partial_full_text: 1 },
  },
};
const html = runOutcomeCards(run);
const flat = html.replace(/\s+/g, ' ');
const stats = readStats(html);
console.log(JSON.stringify({
  available: stats['Full text available'],
  partial: stats['Partial full text'],
  unavailable: stats['Unavailable'],
  explains: flat.includes('XML available, PDF unavailable'),
  artifactLine: /0 PDF and 1 XML file\(s\)/.test(flat),
}));
""",
    )
    assert result == {
        "available": "0",
        "partial": "1",
        "unavailable": "0",
        "explains": True,
        "artifactLine": True,
    }


@needs_node
def test_an_older_api_response_shows_no_invented_partial_number(ui, tmp_path):
    """A response without the field asks the question rather than answering it wrongly."""
    from test_ux_workflow import LEGACY_JULIE, STAT_READER, outcome_harness

    result = run_node(
        tmp_path,
        outcome_harness(app_js(ui)) + STAT_READER,
        f"""
const legacy = {json.dumps(LEGACY_JULIE)};
const stats = readStats(runOutcomeCards(legacy));
console.log(JSON.stringify({{
  keys: Object.keys(stats),
  available: stats['Full text available'],
  anyDashes: Object.values(stats).includes('\\u2014'),
}}));
""",
    )
    assert "Partial full text" not in result["keys"]
    assert result["available"] == "10"
    assert result["anyDashes"] is False


def test_every_view_shares_one_definition_of_partial(ui):
    """Dashboard, run list, run detail and corpus must not drift apart."""
    body = app_js(ui)
    # One explanation, one settled-state table, one badge state — not one per page.
    assert body.count("const PARTIAL_EXPLANATION") == 1
    assert body.count("const SETTLED_STATES") == 1
    assert body.count("PARTIAL:") == 1
    # And no second document state was invented to carry it.
    assert "PARTIAL_FULL_TEXT:" not in body

    # Every place that renders a document badge passes the artifacts it already has:
    # the run detail table, the corpus table, the document panel.
    assert body.count("docPill(d.status, { pdf: d.has_pdf, xml: d.has_xml })") == 2
    assert "docPill(d.status, { pdf: !!pdf, xml: !!xml })" in body

    # Every counter surface reads the same field and prints the same label.
    for start, end, field in (
        ("function activityCard(data) {", "async function viewDashboard()", "p.partial_full_text"),
        ("function harvestDoneCard(done, live) {", "function duplicateWarningCard()", "p.partial_full_text"),
        ("function runOutcomeCards(r) {", "function runWorkNote(work)", "state.partial_full_text"),
    ):
        section = slice_between(body, start, end)
        assert "Partial full text" in section, start
        assert field in section, start

    dashboard = slice_between(body, "async function viewDashboard() {", "function quickStartCard(data)")
    assert "cards.partial_full_text" in dashboard

    # The run list gained the column and kept the shared "unavailable" expression.
    table = slice_between(
        body, "function runsTable(runs, compact = false) {", "async function viewRuns()"
    )
    assert "state.partial_full_text" in table
    assert "(state.unavailable || 0) + (state.retryable || 0)" in table
