"""UX and workflow hardening after the Julie usability test.

One test module per observed problem, in the order the problems were reported. Each
one pins the behaviour that was changed, not the wording that happened to be chosen
for it — except where the wording *was* the defect, in which case the ambiguous phrase
is asserted to be gone and the unambiguous one present.

The front end has no build step and no framework, so the parts of it that can be
executed are executed: `harvestKey`, `phaseLabel` and `docPill` are sliced out of
`app.js` and run under node. The rest is asserted against the served asset, which is
what the existing UI tests do.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import harvester.webui
from harvester.config import env_variable_names

from conftest import wait_for_idle

STATIC = Path(harvester.webui.__file__).parent / "static" / "app.js"

QUERY = {"search": "moral psychology", "limit": 3}

#: What `num()` renders for a value the backend never recorded.
EM_DASH = "\u2014"


def app_js(ui) -> str:
    return ui.get("/app.js", raw=True).decode("utf-8")


def slice_between(source: str, start: str, end: str) -> str:
    """The source text of one front-end function, for executing under node."""
    first, last = source.index(start), source.index(end)
    assert first < last, f"{start!r} no longer precedes {end!r}"
    return source[first:last]


def run_node(tmp_path: Path, source: str, script: str) -> dict:
    harness = tmp_path / "harness.mjs"
    harness.write_text(f"{source}\n{script}\n", encoding="utf-8")
    # Decoded as UTF-8 explicitly. Python would otherwise use the console code page,
    # which on Windows turns every em dash, curly quote and box character the interface
    # renders into mojibake — and an assertion about "—" would fail for the wrong reason.
    completed = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, timeout=60,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs node to run the UI code"
)


# ==================================================== 1. search mode clarity


def test_the_two_search_modes_explain_themselves_without_jargon(ui):
    """A research-oriented first-time user could not tell the modes apart.

    "I know the query I want to use" assumes the reader already knows what a query is
    and whether they have one. The replacement says what each mode expects from them.
    """
    body = app_js(ui)
    assert "I know the query I want to use." not in body
    assert "I already have the words to search for" in body
    assert "I have a topic or a research question" in body
    # And a way out for someone who still cannot choose.
    assert "Not sure?" in body


def test_switching_search_mode_is_still_lossless(ui):
    """Guard on the existing behaviour the clarification must not disturb."""
    body = app_js(ui)
    setter = slice_between(body, "  setMode(mode) {", "  field(key, value) {")
    assert "searchState.mode = mode" in setter
    # Nothing in the switch clears the other mode's state.
    for cleared in ("conventionalQuery = ''", "researchQuestion = ''", "preview = null"):
        assert cleared not in setter


# ================================ 2. running harvest: visible progress/liveness


def test_live_progress_reports_measured_counters_not_estimates(ui):
    ui.post("/api/harvest", {**QUERY, "dry_run": False})
    wait_for_idle(ui)

    progress = ui.get("/api/activity")["progress"]
    # Everything the operator is shown while a run is in flight.
    for key in (
        "discovered", "discovery_pages", "discovery_complete", "acquired",
        "unavailable", "retryable", "in_flight", "pending", "settled", "total",
        "started_at", "last_activity_at",
    ):
        assert key in progress, key
    assert progress["acquired"] == 3
    assert progress["in_flight"] == 0
    assert progress["discovery_complete"] is True
    # Liveness: the run did something, and state says when.
    assert progress["last_activity_at"]
    assert progress["last_activity_at"] >= progress["started_at"]


def test_last_activity_is_none_before_a_run_has_done_anything(ui):
    """A run with no attempts and no documents must not invent a timestamp."""
    store = ui.server.context.store
    store.create_run("run-empty", query={"search": "x"}, config={})
    assert store.last_activity_at("run-empty") is None


def test_the_progress_bar_waits_for_discovery_to_finish(ui):
    """A percentage of a total that is still growing is a guess, not a measurement.

    The front end is therefore told whether discovery has finished, and shows the bar
    only then; until it has, it reports the record and page counts it actually has.
    """
    body = app_js(ui)
    card = slice_between(body, "function activityCard(data) {", "async function viewDashboard")
    assert "p.discovery_complete" in card
    assert "Still searching" in card
    assert "record(s) found so far" in card
    # And the liveness lines that replace a bare "running", now shared with the
    # finished-run wording so the two can never drift apart.
    assert "operationTiming(op, p, running)" in card
    timing = slice_between(body, "function operationTiming(", "function statCells(")
    assert "Running for" in timing and "Last activity" in timing


@needs_node
def test_the_phase_label_names_what_is_actually_happening(ui, tmp_path):
    source = slice_between(app_js(ui), "function phaseLabel(", "function statCells(")
    result = run_node(
        tmp_path,
        source,
        """
const busy = { finished: false, kind: 'harvest', phase: 'running' };
console.log(JSON.stringify({
  starting:   phaseLabel(busy, null),
  searching:  phaseLabel(busy, { discovery_complete: false }),
  downloading:phaseLabel(busy, { discovery_complete: true, in_flight: 2, pending: 5 }),
  acquiring:  phaseLabel(busy, { discovery_complete: true, in_flight: 0, pending: 5 }),
  settled:    phaseLabel(busy, { discovery_complete: true, in_flight: 0, pending: 0 }),
  finished:   phaseLabel({ finished: true, ok: true }, null),
  stopped:    phaseLabel({ finished: true, ok: false }, null),
  verifying:  phaseLabel({ finished: false, kind: 'verify' }, null),
}));
""",
    )
    assert result == {
        "starting": "starting", "searching": "searching", "downloading": "downloading",
        "acquiring": "acquiring", "settled": "running", "finished": "finished",
        "stopped": "stopped", "verifying": "checking artifacts",
    }


# ======================================================= 3. post-harvest workflow


def test_a_finished_harvest_becomes_the_pages_primary_action(ui):
    """The operator was left on a page that still looked like the pre-harvest one.

    After a successful full harvest the page leads with the result, and the harvest
    button stops being the brightest thing on it.
    """
    body = app_js(ui)
    card = slice_between(body, "function harvestDoneCard(", "function duplicateWarningCard(")
    assert "View harvest results" in card
    assert 'class="btn primary" href="#/runs/' in card          # the primary action opens the run
    assert "actions.refineSearch()" in card
    assert "actions.newHarvest()" in card
    assert "Retry unavailable" in card

    # Start full harvest is demoted, not removed: a deliberate rerun stays possible.
    form = slice_between(body, "async function viewHarvest() {", "/* ------------------------------------------------------- assisted preview */")
    assert "done ? 'Run this harvest again' : 'Start harvest'" in form
    assert "btn ${done ? '' : 'primary'}" in form
    # And the search preview steps aside once the harvest it approved has run.
    assert '<div id="assisted-preview" ${done ? \'hidden\' : \'\'}>' in form


def test_the_finished_harvest_is_recognised_from_the_runs_own_record(ui):
    """So the page still knows what finished after a browser reload."""
    body = app_js(ui)
    remember = slice_between(body, "function rememberCompletedHarvest(", "function startPolling(")
    assert "op.detail && op.detail.query" in remember
    assert "harvestKey(query)" in remember
    # The run manager has to keep supplying that query for this to work.
    ui.post("/api/harvest", {**QUERY, "dry_run": False})
    operation = wait_for_idle(ui)
    assert operation["kind"] == "harvest"
    assert operation["ok"] is True
    assert operation["run_id"]
    assert operation["detail"]["query"]["search"] == "moral psychology"


def test_editing_the_search_returns_the_page_to_its_pre_harvest_state(ui):
    """Without stealing the focus out of the field being typed into."""
    body = app_js(ui)
    sync = slice_between(body, "function syncPostHarvest() {", "/* Cheap refresh of just")
    assert "harvestComplete()" in sync
    assert "card.hidden = !done" in sync
    assert "Run this harvest again" in sync
    assert "rerender" not in sync           # a re-render per keystroke would lose focus
    # And the toggle runs before the assisted-preview early return, which conventional
    # mode always takes.
    controls = slice_between(body, "function syncControls() {", "function harvestPayload(")
    assert controls.index("syncPostHarvest()") < controls.index("if (!button || !banner) return")


@needs_node
def test_a_harvest_is_recognised_from_either_shape_of_the_same_search(ui, tmp_path):
    """The request the browser sends and the query a run records name the year bounds
    differently. They describe the same search, so they must key the same."""
    source = slice_between(app_js(ui), "function harvestKey(", "function currentHarvestKey(")
    result = run_node(
        tmp_path,
        source,
        """
const payload = { search: 'julie', topic_id: '', from_year: 1990, to_year: null,
                  oa_status: '', languages: ['en', 'de'], affiliation_countries: [] };
const recorded = { search: 'julie', topic_id: null, primary_topic_only: false,
                   from_publication_year: 1990, to_publication_year: null,
                   oa_status: null, languages: ['de', 'en'], affiliation_countries: [],
                   filter: 'ignored', extra_filters: [] };
console.log(JSON.stringify({
  sameAcrossShapes: harvestKey(payload) === harvestKey(recorded),
  changedQuery:     harvestKey(payload) === harvestKey({ ...payload, search: 'other' }),
  changedYear:      harvestKey(payload) === harvestKey({ ...payload, from_year: 1991 }),
  changedLanguages: harvestKey(payload) === harvestKey({ ...payload, languages: ['en'] }),
  limitIgnored:     harvestKey(payload) === harvestKey({ ...payload, limit: 999 }),
}));
""",
    )
    assert result == {
        "sameAcrossShapes": True, "changedQuery": False, "changedYear": False,
        "changedLanguages": False, "limitIgnored": True,
    }


# ======================================================= 4. preview terminology


def test_preview_never_reads_as_show_me_my_harvest(ui):
    """"Preview results" after a harvest was read as "the results of my harvest"."""
    body = app_js(ui)
    assert "Preview results<" not in body
    assert "Preview (dry run)" not in body
    assert "Preview first<" not in body
    assert "Harvest these documents" not in body

    assert "Preview search results" in body
    assert "Search preview" in body
    assert "View harvest results" in body
    # The preview card says in so many words which results these are.
    assert "These are the search results, not harvest results" in body


def test_a_preview_run_is_labelled_as_a_search_preview(ui):
    started = ui.post("/api/harvest", {**QUERY, "dry_run": True})
    assert started["started"] is True
    wait_for_idle(ui)
    body = app_js(ui)
    assert "Search preview — ${num(detail.document_total)} record(s) found" in body
    assert "'Search preview — nothing was downloaded'" in body
    # The run itself is still recorded as a dry run: no backend semantics moved.
    assert ui.get("/api/runs")["runs"][0]["dry_run"] is True


# ================================================== 5. acquisition status semantics


def test_document_status_is_presented_as_an_acquisition_outcome(ui):
    body = app_js(ui)
    states = slice_between(body, "const DOC_STATE = {", "function pill(")
    assert "Full text acquired" in states
    assert "Full text unavailable" in states
    assert "can retry" in states
    # Presentation layer only: the raw state is still one hover away.
    assert 'title="${esc(status)}"' in states


def test_the_backend_status_values_are_untouched(ui):
    """The mapping is presentation. State, API and CLI still speak the same states."""
    ui.post("/api/harvest", {**QUERY, "dry_run": False})
    operation = wait_for_idle(ui)

    detail = ui.get(f"/api/runs/{operation['run_id']}")
    assert {d["status"] for d in detail["documents"]} == {"COMPLETED"}
    assert detail["document_counts"]["COMPLETED"] == 3
    corpus = ui.get("/api/corpus")
    assert {d["status"] for d in corpus["documents"]} == {"COMPLETED"}


@needs_node
def test_an_unknown_state_still_renders_rather_than_disappearing(ui, tmp_path):
    source = slice_between(app_js(ui), "function esc(value) {", "function choiceOptions(")
    source += slice_between(app_js(ui), "const DOC_STATE = {", "function pill(")
    result = run_node(
        tmp_path,
        source,
        """
console.log(JSON.stringify({
  completed:  docPill('COMPLETED'),
  permanent:  docPill('FAILED_PERMANENT'),
  unknown:    docPill('SOMETHING_NEW'),
}));
""",
    )
    assert "Full text acquired" in result["completed"]
    assert 'title="COMPLETED"' in result["completed"]
    assert "Full text unavailable" in result["permanent"]
    # An unrecognised state degrades to the raw value instead of rendering blank.
    assert "something_new" in result["unknown"]


# ======================================================== 6. run summary semantics


def test_a_repeat_run_separates_corpus_state_from_work_performed(ui):
    """The reported confusion, reproduced and then answered.

    The second run over the same query acquires nothing — everything is already on
    disk — while the documents it covers are all in hand. "0 acquired" and "3 with full
    text" are both true, and printed as one number they looked like a bug.
    """
    ui.post("/api/harvest", {**QUERY, "dry_run": False})
    wait_for_idle(ui)
    ui.post("/api/harvest", {**QUERY, "dry_run": False})
    second = wait_for_idle(ui)

    outcome = ui.get(f"/api/runs/{second['run_id']}")["outcome"]

    state = outcome["corpus_state"]
    assert state["full_text_available"] == 3
    assert state["unavailable"] == 0
    assert state["retryable"] == 0
    assert state["total"] == 3

    work = outcome["this_run"]
    assert work["newly_acquired"] == 0            # nothing was downloaded again
    assert work["already_available"] == 3         # ... because it was all already there
    assert work["bytes_downloaded"] == 0
    assert "retries_attempted" in work

    # And the two blocks agree with the raw technical report they are derived from.
    report = ui.get(f"/api/runs/{second['run_id']}/report")
    assert report["completed"] == work["newly_acquired"]
    assert report["already_complete"] == work["already_available"]


def test_a_run_still_in_flight_reports_no_work_block(ui):
    """Rather than a row of zeros it cannot vouch for."""
    store = ui.server.context.store
    store.create_run("run-live", query={"search": "x"}, config={})
    summary = next(r for r in ui.get("/api/runs")["runs"] if r["run_id"] == "run-live")
    assert summary["outcome"]["this_run"] is None
    assert summary["outcome"]["corpus_state"]["total"] == 0


def test_the_runs_list_counts_the_same_things_the_run_page_does(ui):
    """One definition of "unavailable", not one per page.

    The list used to add the two failed states together while the run page counted
    permanently-failed and skipped documents as unavailable and retryable ones
    separately. Both now read the same block.
    """
    body = app_js(ui)
    table = slice_between(body, "function runsTable(runs, compact = false) {", "async function viewRuns(")
    assert "corpusStateOf(r)" in table
    assert "state.full_text_available" in table
    assert "(state.unavailable || 0) + (state.retryable || 0)" in table
    assert "num(r.completed)" not in table and "num(r.failed)" not in table
    # The API still reports the older fields: no client is broken by the change.
    ui.post("/api/harvest", {**QUERY, "dry_run": False})
    wait_for_idle(ui)
    summary = ui.get("/api/runs")["runs"][0]
    assert summary["completed"] == 3
    assert summary["failed"] == 0
    assert summary["outcome"]["corpus_state"]["full_text_available"] == 3


def test_the_run_page_asks_the_two_questions_separately(ui):
    body = app_js(ui)
    cards = slice_between(body, "function runOutcomeCards(r) {", "async function viewRunDetail(")
    assert "Full text in the corpus" in cards
    assert "What this run did" in cards
    assert "Newly acquired" in cards and "Already available" in cards
    assert "Retries attempted" in cards


# ============================================================= 7. report naming


def test_the_raw_run_json_is_named_for_what_it_is(ui):
    ui.post("/api/harvest", {**QUERY, "dry_run": False})
    operation = wait_for_idle(ui)

    body = app_js(ui)
    assert ">Open report</a>" not in body
    assert "Technical report (JSON)" in body

    # And it is still served, unchanged and complete: this is audit data.
    report = ui.get(f"/api/runs/{operation['run_id']}/report")
    for key in (
        "run_id", "status", "records_discovered", "completed", "already_complete",
        "downloaded", "bytes_downloaded", "failed_retryable", "failed_permanent",
        "retry_count", "search_provenance",
    ):
        assert key in report, key


# =================================================== 8. duplicate / repeat runs


def test_an_identical_search_is_reported_before_it_is_run_again(ui):
    ui.post("/api/harvest", {**QUERY, "dry_run": False})
    first = wait_for_idle(ui)

    answer = ui.post("/api/search/similar-runs", QUERY)
    assert ui.status == 200
    assert [r["run_id"] for r in answer["runs"]] == [first["run_id"]]
    assert answer["window_days"] >= 1
    match = answer["runs"][0]
    assert match["outcome"]["corpus_state"]["full_text_available"] == 3
    assert match["differences"] == []


def test_a_different_search_is_not_reported_as_a_repeat(ui):
    ui.post("/api/harvest", {**QUERY, "dry_run": False})
    wait_for_idle(ui)

    for changed in (
        {"search": "something else", "limit": 3},
        {**QUERY, "from_year": 2000},
        {**QUERY, "languages": ["en"]},
        {**QUERY, "topic_id": "T10159"},
    ):
        assert ui.post("/api/search/similar-runs", changed)["runs"] == [], changed


def test_a_preview_is_never_reported_as_a_previous_harvest(ui):
    """It downloaded nothing, so repeating it costs nothing worth warning about."""
    ui.post("/api/harvest", {**QUERY, "dry_run": True})
    wait_for_idle(ui)
    assert ui.post("/api/search/similar-runs", QUERY)["runs"] == []


def test_a_repeat_with_different_acquisition_settings_is_still_reported(ui):
    """The search is what the operator recognises; the differences are named, not hidden."""
    ui.post("/api/harvest", {**QUERY, "dry_run": False, "xml_policy": "preferred"})
    wait_for_idle(ui)

    answer = ui.post(
        "/api/search/similar-runs",
        {**QUERY, "limit": 25, "xml_policy": "disabled", "unpaywall": False},
    )
    assert len(answer["runs"]) == 1
    differences = answer["runs"][0]["differences"]
    assert any("full-text XML" in d for d in differences), differences
    assert any("Unpaywall" in d for d in differences), differences
    assert any("document limit" in d for d in differences), differences


def test_the_repeat_check_starts_nothing_and_writes_nothing(ui):
    ui.post("/api/harvest", {**QUERY, "dry_run": False})
    wait_for_idle(ui)
    before = ui.get("/api/runs")["runs"]

    ui.post("/api/search/similar-runs", QUERY)
    assert ui.get("/api/runs")["runs"] == before
    assert ui.get("/api/activity")["busy"] is False


def test_an_empty_query_is_refused_by_the_repeat_check_like_any_other(ui):
    ui.post("/api/search/similar-runs", {})
    assert ui.status == 400


def test_the_warning_offers_both_ways_out_and_blocks_neither(ui):
    body = app_js(ui)
    card = slice_between(body, "function duplicateWarningCard() {", "async function viewHarvest() {")
    assert "This search was already harvested recently" in card
    assert "Open existing run" in card
    assert "actions.runAnyway()" in card

    check = slice_between(body, "  async checkForRepeat(payload) {", "  async runAnyway() {")
    # The acknowledgement is spent when it is used. Remembering that a search had once
    # been cleared would suppress the warning in exactly the case it exists for: the
    # first start finds no previous run, and the second — the actual repeat — would
    # then pass in silence.
    assert "searchState.duplicateAck = null;" in check
    assert "searchState.duplicateAck = key" not in check
    granted = slice_between(body, "  async runAnyway() {", "  dismissDuplicate() {")
    assert "searchState.duplicateAck = pending.key" in granted
    # A failed or unavailable check must let the harvest through: an advisory that can
    # block is no longer an advisory.
    after_catch = check.split("catch (_) {")[1]
    assert "return true;" in after_catch.split("}")[0]
    # And the question is asked once per search, not once per click.
    assert "searchState.duplicateAck === key" in check


# ============================ 10. legacy runs (manual acceptance regressions)
#
# The Julie benchmark run predates this work, and the Control Center that showed it had
# been running since before the API gained its `outcome` block: static assets are read
# from disk on every request while the API lives in the server process's memory, so new
# markup was talking to an older API. The run itself is intact — 25 documents, 10 with
# full text, 10 PDFs and 4 XMLs, 90 seconds of work — and the interface must report it
# from whatever that older response carries rather than printing dashes over it.

#: The run as the older API describes it: per-document counts and stored counters, and
#: no `outcome` block. The numbers are the real ones from the benchmark run.
LEGACY_JULIE = {
    "run_id": "run-20260827T190057Z-8346b475",
    "dry_run": False,
    "started_at": "2026-08-27T19:00:57Z",
    "finished_at": "2026-08-27T19:02:27Z",
    "discovery_seen": 25,
    "discovery_pages": 1,
    "document_counts": {"COMPLETED": 10, "FAILED_PERMANENT": 13, "FAILED_RETRYABLE": 2},
    "artifacts": {"pdf": 10, "xml": 4, "bytes": 13705895},
    "stats": {
        "duration_seconds": 90.297, "completed": 0, "already_complete": 10,
        "downloaded": 0, "bytes_downloaded": 0, "failed_retryable": 2,
        "failed_permanent": 0, "retry_count": 6, "records_discovered": 25,
        "duplicates": 25,
    },
}


def outcome_harness(body: str) -> str:
    """Everything needed to render `runOutcomeCards` under node."""
    return "".join((
        slice_between(body, "function esc(value) {", "function choiceOptions("),
        slice_between(body, "function num(value) {", "function when(iso) {"),
        slice_between(body, "const STATE_GROUPS = {", "function docPill("),
        slice_between(body, "function statCells(cells) {", "function activityCard(data) {"),
        slice_between(body, "function corpusStateOf(r) {", "async function viewRunDetail("),
    ))


STAT_READER = r"""
const readStats = (html) => Object.fromEntries(
  [...html.matchAll(/stat-label">([^<]*)<\/div>\s*<div class="stat-value[^>]*>([^<]*)</g)]
    .map(([, label, value]) => [label, value]));
"""


@needs_node
def test_a_legacy_run_still_reports_its_corpus_state(ui, tmp_path):
    """The reported defect: four dashes over a run with ten full texts in hand."""
    result = run_node(
        tmp_path,
        outcome_harness(app_js(ui)) + STAT_READER,
        rf"""
const legacy = {json.dumps(LEGACY_JULIE)};
const html = runOutcomeCards(legacy);
const stats = readStats(html);
console.log(JSON.stringify({{
  fullTextAvailable: stats['Full text available'],
  unavailable: stats['Unavailable'],
  canBeRetried: stats['Can be retried'],
  stillToAcquire: stats['Still to acquire'],
  anyDashes: Object.values(stats).includes('\u2014'),
  artifactLine: /10 PDF and\s+4 XML file\(s\)/.test(html.replace(/\s+/g, ' ')),
}}));
""",
    )
    assert result == {
        "fullTextAvailable": "10",
        "unavailable": "13",
        "canBeRetried": "2",
        "stillToAcquire": "0",
        "anyDashes": False,
        "artifactLine": True,
    }


@needs_node
def test_a_legacy_run_reports_the_work_it_recorded(ui, tmp_path):
    """"No counters were recorded" was wrong: the run recorded them under older names."""
    result = run_node(
        tmp_path,
        outcome_harness(app_js(ui)) + STAT_READER,
        rf"""
const legacy = {json.dumps(LEGACY_JULIE)};
const html = runOutcomeCards(legacy);
const stats = readStats(html);
console.log(JSON.stringify({{
  newlyAcquired: stats['Newly acquired'],
  alreadyAvailable: stats['Already available'],
  retriesAttempted: stats['Retries attempted'],
  downloaded: stats['Downloaded'],
  saysNoCounters: html.includes('stored no counters'),
  labelledAsStored: html.includes("stored counters"),
  note: /25 record\(s\) found by\s+the search, 25 of them already known/.test(html.replace(/\s+/g, ' ')),
}}));
""",
    )
    assert result["newlyAcquired"] == "0"          # nothing was downloaded again
    assert result["alreadyAvailable"] == "10"      # ... because it was already there
    assert result["retriesAttempted"] == "6"
    assert result["downloaded"] == "0 B"
    assert result["saysNoCounters"] is False
    assert result["labelledAsStored"] is True      # the section says where it came from
    assert result["note"] is True


@needs_node
def test_a_counter_a_legacy_run_never_recorded_is_not_invented(ui, tmp_path):
    """An absent counter renders as an em dash, never as a zero.

    Zero is a claim — "this run did none of that" — and it is not one that can be made
    on behalf of a run that simply never wrote the field.
    """
    thin = dict(LEGACY_JULIE)
    thin["stats"] = {"already_complete": 10}       # everything else absent
    result = run_node(
        tmp_path,
        outcome_harness(app_js(ui)) + STAT_READER,
        rf"""
const thin = {json.dumps(thin)};
const html = runOutcomeCards(thin);
const stats = readStats(html);
console.log(JSON.stringify({{
  alreadyAvailable: stats['Already available'],
  newlyAcquired: stats['Newly acquired'],
  retriesAttempted: stats['Retries attempted'],
  downloaded: stats['Downloaded'],
  note: html.includes('No further counters were recorded'),
}}));
""",
    )
    assert result["alreadyAvailable"] == "10"
    for absent in ("newlyAcquired", "retriesAttempted", "downloaded"):
        # An em dash, never a zero: a zero would be a claim about what the run did.
        assert result[absent] == EM_DASH, (absent, result[absent])
    assert result["note"] is True


@needs_node
def test_a_run_with_no_counters_at_all_says_so_without_zeros(ui, tmp_path):
    result = run_node(
        tmp_path,
        outcome_harness(app_js(ui)) + STAT_READER,
        rf"""
const finished = {json.dumps({**LEGACY_JULIE, "stats": None})};
const running = {json.dumps({**LEGACY_JULIE, "stats": None, "finished_at": None})};
console.log(JSON.stringify({{
  finished: runOutcomeCards(finished).includes('stored no counters'),
  finishedKeepsCorpusState: readStats(runOutcomeCards(finished))['Full text available'],
  running: runOutcomeCards(running).includes('has not finished'),
}}));
""",
    )
    assert result == {
        "finished": True, "finishedKeepsCorpusState": "10", "running": True,
    }


@needs_node
def test_a_current_run_still_uses_the_servers_own_outcome_block(ui, tmp_path):
    """The reconstruction is a fallback, never a second opinion."""
    result = run_node(
        tmp_path,
        outcome_harness(app_js(ui)),
        """
// Deliberately disagreeing numbers: whichever appears is the one that was trusted.
const current = {
  dry_run: false, discovery_seen: 25, artifacts: { pdf: 1, xml: 0 },
  document_counts: { COMPLETED: 999 },
  outcome: {
    corpus_state: { full_text_available: 7, unavailable: 1, retryable: 0, in_progress: 0, total: 8 },
    this_run: { newly_acquired: 7, already_available: 0, retries_attempted: 0,
                bytes_downloaded: 42, discovered: 8, duplicates: 0,
                failed_permanent: 1, failed_retryable: 0 },
  },
};
console.log(JSON.stringify({
  state: corpusStateOf(current),
  legacyFlag: runWorkOf(current).legacy,
  work: runWorkOf(current).work.newly_acquired,
}));
""",
    )
    assert result["state"]["full_text_available"] == 7      # not 999
    assert result["legacyFlag"] is False
    assert result["work"] == 7


@needs_node
def test_a_finished_run_stops_counting_elapsed_time(ui, tmp_path):
    """The reported defect: "Ran for 1242m 4s" for a harvest that took ninety seconds.

    Time since it started is the right number only while something is still running.
    """
    source = "".join((
        slice_between(app_js(ui), "function esc(value) {", "function choiceOptions("),
        slice_between(app_js(ui), "function when(iso) {", "function elapsed(iso) {"),
        slice_between(app_js(ui), "function elapsed(iso) {", "const RUN_TONE"),
        slice_between(app_js(ui), "function operationTiming(", "function statCells("),
    ))
    result = run_node(
        tmp_path,
        source,
        """
// An operation that started twenty hours ago and finished ninety seconds later.
const longAgo = new Date(Date.now() - 20 * 3600 * 1000).toISOString();
const op = { started_at: longAgo, finished: true, ok: true,
             result: { stats: { duration_seconds: 90.297 } } };

const withProgress = operationTiming(op, {
  duration_seconds: 90.297, finished_at: longAgo }, false);
const withoutProgress = operationTiming(op, null, false);
const noDurationAnywhere = operationTiming(
  { started_at: longAgo, finished: true, ok: true }, {}, false);
const stillRunning = operationTiming(
  { started_at: new Date(Date.now() - 65000).toISOString(), finished: false },
  { last_activity_at: new Date(Date.now() - 5000).toISOString() }, true);

console.log(JSON.stringify({
  withProgress, withoutProgress, noDurationAnywhere, stillRunning,
}));
""",
    )
    # The run's own duration, never the wall clock since it started.
    assert result["withProgress"].startswith("Ran for 1m 30s.")
    assert "1200m" not in result["withProgress"] and "1201m" not in result["withProgress"]
    # Falls back to the counters the operation record carries.
    assert result["withoutProgress"] == "Ran for 1m 30s."
    # With no duration recorded anywhere it says nothing about the length.
    assert result["noDurationAnywhere"] == "This operation has finished."
    assert "No activity recorded yet" not in result["noDurationAnywhere"]
    # And a genuinely running operation still gets its live clock and liveness line.
    assert result["stillRunning"].startswith("Running for 1m 5s.")
    assert "Last activity" in result["stillRunning"]


def test_the_activity_progress_carries_the_runs_real_duration(ui):
    """So the front end never has to compute one from the wall clock."""
    ui.post("/api/harvest", {**QUERY, "dry_run": False})
    wait_for_idle(ui)

    progress = ui.get("/api/activity")["progress"]
    assert progress["finished_at"]
    assert progress["duration_seconds"] is not None
    assert progress["duration_seconds"] >= 0
    # It is the run's own duration, not the age of the run record.
    assert progress["finished_at"] >= progress["started_at"]


def test_the_api_still_derives_corpus_state_when_a_run_stored_no_counters(ui):
    """The backend half of the same guarantee.

    `corpus_state` is read from the documents, so it survives a run whose counters were
    never written. `this_run` is honestly absent rather than a row of zeros.
    """
    store = ui.server.context.store
    ui.post("/api/harvest", {**QUERY, "dry_run": False})
    operation = wait_for_idle(ui)

    # Strip the stored counters, exactly as a run interrupted before writing them.
    with store.transaction() as conn:
        conn.execute("UPDATE runs SET stats_json = NULL WHERE run_id = ?", (operation["run_id"],))

    detail = ui.get(f"/api/runs/{operation['run_id']}")
    assert detail["outcome"]["this_run"] is None
    assert detail["outcome"]["corpus_state"]["full_text_available"] == 3
    assert detail["outcome"]["corpus_state"]["total"] == 3


@needs_node
def test_the_activity_counters_survive_an_older_progress_payload(ui, tmp_path):
    """The same page-newer-than-server case, on the dashboard's live panel."""
    source = "".join((
        slice_between(app_js(ui), "const STATE_GROUPS = {", "function docPill("),
    ))
    result = run_node(
        tmp_path,
        source,
        """
// An older /api/activity payload: per-document counts, no grouped totals.
const legacy = { COMPLETED: 10, FAILED_PERMANENT: 13, FAILED_RETRYABLE: 2 };
console.log(JSON.stringify(groupCounts(legacy)));
""",
    )
    assert result == {
        "full_text_available": 10, "unavailable": 13, "retryable": 2,
        "in_flight": 0, "pending": 0, "in_progress": 0, "total": 25,
    }


# ================================================== 9. test environment isolation


def test_no_harvester_environment_variable_reaches_a_test(ui):
    """The regression this fixture exists for.

    Two web-UI tests used to pass on a bare machine and fail on the machine of the
    person who actually uses the harvester, because their exported credentials became
    part of the configuration under test.
    """
    leaked = [name for name in env_variable_names() if os.environ.get(name)]
    assert leaked == []


def test_an_unconfigured_harvest_is_refused_whatever_the_host_exports(ui, ui_config_file):
    config = json.loads(ui_config_file.read_text("utf-8"))
    config["openalex"].pop("api_key")
    ui_config_file.write_text(json.dumps(config), encoding="utf-8")
    ui.server.context.reload_config()

    result = ui.post("/api/harvest", {"search": "x", "limit": 1, "dry_run": False})
    assert ui.status == 400
    assert "API key" in result["error"]
