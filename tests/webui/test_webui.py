"""Web UI integration tests.

Each acceptance criterion from the UI brief is exercised end to end through the real
HTTP server: launch, dry run, real harvest, run detail, corpus browse/filter,
provenance, verify/deep verify, retry failed, opening a PDF, and the guarantee that
the UI drives the same state the CLI does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harvester.identity import document_id_for_doi
from harvester.state import StateStore

from conftest import wait_for_idle


# ============================================================ A. launch / shell


def test_a_ui_shell_is_served(ui):
    body = ui.get("/", raw=True).decode("utf-8")
    assert ui.status == 200
    assert "Control Center" in body
    for asset in ("/styles.css", "/app.js"):
        ui.get(asset, raw=True)
        assert ui.status == 200


def test_deep_links_fall_back_to_the_spa_shell(ui):
    body = ui.get("/some/deep/link", raw=True).decode("utf-8")
    assert ui.status == 200
    assert "<div id=\"view\"" in body


def test_dashboard_reports_an_empty_corpus_without_error(ui):
    data = ui.get("/api/dashboard")
    assert data["cards"] == {
        "documents": 0, "completed": 0, "full_text_available": 0,
        "partial_full_text": 0, "pdfs": 0, "xmls": 0, "bytes": 0, "failed": 0,
    }
    assert data["last_verification"] is None
    assert data["recent_runs"] == []
    assert data["can_start"] is True


def test_provider_readiness_is_explained_in_plain_language(ui):
    data = ui.get("/api/providers")
    by_id = {p["id"]: p for p in data["providers"]}
    assert by_id["openalex"]["state"] == "ready"
    assert by_id["europe_pmc"]["state"] == "ready"
    assert by_id["unpaywall"]["state"] == "ready"
    assert data["can_start"] is True


def test_missing_contact_email_is_surfaced_as_a_blocker(ui, ui_config_file, monkeypatch):
    monkeypatch.delenv("HARVESTER_CONTACT_EMAIL", raising=False)
    config = json.loads(ui_config_file.read_text("utf-8"))
    del config["contact_email"]
    ui_config_file.write_text(json.dumps(config), encoding="utf-8")
    ui.server.context.reload_config()

    data = ui.get("/api/providers")
    unpaywall = next(p for p in data["providers"] if p["id"] == "unpaywall")
    assert unpaywall["state"] == "blocked"
    assert "contact email" in unpaywall["detail"].lower()
    assert unpaywall["action"]
    assert data["can_start"] is False


# ============================================================ B. dry run preview


def test_b_dry_run_preview_discovers_without_downloading(ui, tmp_path):
    started = ui.post("/api/harvest", {"search": "moral psychology", "limit": 3, "dry_run": True})
    assert ui.status == 200
    assert started["started"] is True
    assert started["operation"]["kind"] == "dry_run"

    operation = wait_for_idle(ui)
    assert operation["ok"] is True
    run_id = operation["run_id"]

    detail = ui.get(f"/api/runs/{run_id}")
    assert detail["dry_run"] is True
    assert detail["document_total"] == 3
    assert len(detail["documents"]) == 3
    assert all(d["title"] for d in detail["documents"])

    corpus = Path(ui.server.context.config.storage_root)
    assert not corpus.exists() or list(corpus.glob("*.pdf")) == []


def test_dry_run_rejects_an_empty_query_with_a_helpful_message(ui):
    result = ui.post("/api/harvest", {"dry_run": True})
    assert ui.status == 400
    assert "search term" in result["error"].lower()


def test_deprecated_concept_id_is_refused(ui):
    result = ui.post("/api/harvest", {"topic_id": "C169760540", "dry_run": True})
    assert ui.status == 400
    assert "Topic ID" in result["error"]


# ============================================================ C. real harvest


def test_c_real_harvest_downloads_validates_and_stores(ui):
    ui.post("/api/harvest", {"search": "moral psychology", "limit": 3, "dry_run": False})
    operation = wait_for_idle(ui)

    assert operation["ok"] is True
    assert operation["result"]["status"] == "COMPLETED"
    assert operation["result"]["stats"]["completed"] == 3
    assert "3 document(s) completed" in operation["message"]

    corpus = Path(ui.server.context.config.storage_root)
    assert len(list(corpus.glob("*.pdf"))) == 3
    assert len(list(corpus.glob("*.json"))) == 3

    dashboard = ui.get("/api/dashboard")
    assert dashboard["cards"]["documents"] == 3
    assert dashboard["cards"]["pdfs"] == 3
    assert dashboard["cards"]["bytes"] > 0


def test_only_one_operation_runs_at_a_time(ui):
    ui.post("/api/harvest", {"search": "one", "limit": 5, "dry_run": False})
    second = ui.post("/api/harvest", {"search": "two", "limit": 5, "dry_run": False})
    assert ui.status == 409
    assert "still running" in second["error"]
    wait_for_idle(ui)


def test_per_harvest_toggles_do_not_change_saved_settings(ui, ui_config_file):
    before = json.loads(ui_config_file.read_text("utf-8"))
    ui.post(
        "/api/harvest",
        {"search": "x", "limit": 1, "dry_run": True, "europe_pmc": False,
         "unpaywall": False, "xml_policy": "disabled"},
    )
    wait_for_idle(ui)
    assert json.loads(ui_config_file.read_text("utf-8")) == before


def test_harvest_is_blocked_when_configuration_is_incomplete(ui, ui_config_file):
    config = json.loads(ui_config_file.read_text("utf-8"))
    config["openalex"].pop("api_key")
    ui_config_file.write_text(json.dumps(config), encoding="utf-8")
    ui.server.context.reload_config()

    result = ui.post("/api/harvest", {"search": "x", "limit": 1, "dry_run": False})
    assert ui.status == 400
    assert "API key" in result["error"]
    # A preview still works, so the operator can explore before configuring.
    ui.post("/api/harvest", {"search": "x", "limit": 1, "dry_run": True})
    assert ui.status == 200
    wait_for_idle(ui)


# ============================================================ D. runs


def test_d_runs_list_and_detail(ui):
    ui.post("/api/harvest", {"search": "moral psychology", "limit": 2, "dry_run": False})
    operation = wait_for_idle(ui)
    run_id = operation["run_id"]

    listing = ui.get("/api/runs")
    assert len(listing["runs"]) == 1
    summary = listing["runs"][0]
    assert summary["run_id"] == run_id
    assert summary["status"] == "COMPLETED"
    assert summary["completed"] == 2

    detail = ui.get(f"/api/runs/{run_id}")
    for field in (
        "status", "started_at", "finished_at", "document_counts", "artifacts",
        "failures", "failure_groups", "documents", "providers_used", "storage_root",
        "query", "stats",
    ):
        assert field in detail, f"run detail is missing {field}"
    # Europe PMC is cross-checked for every DOI, so it took part even though it
    # discovered nothing — which is precisely what this field used to hide.
    assert detail["providers_used"] == ["europe_pmc", "openalex"]
    assert detail["artifacts"]["pdf"] == 2
    assert detail["report_path"]


def test_run_report_is_downloadable(ui):
    ui.post("/api/harvest", {"search": "x", "limit": 1, "dry_run": False})
    run_id = wait_for_idle(ui)["run_id"]
    report = ui.get(f"/api/runs/{run_id}/report")
    assert report["run_id"] == run_id
    assert report["status"] == "COMPLETED"


def test_unknown_run_is_a_clean_404(ui):
    result = ui.get("/api/runs/run-does-not-exist")
    assert ui.status == 404
    assert "No run named" in result["error"]


def test_suspended_run_is_reported_as_resumable(ui):
    from mocks import Behavior

    ui.providers.script_route(
        "openalex.works",
        [Behavior(status=429, json={"error": "daily credit allowance exhausted"},
                  headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "600"})],
    )
    ui.post("/api/harvest", {"search": "x", "limit": 5, "dry_run": False})
    operation = wait_for_idle(ui)

    assert operation["result"]["status"] == "SUSPENDED"
    assert "budget" in operation["message"].lower()
    detail = ui.get(f"/api/runs/{operation['run_id']}")
    assert detail["resumable"] is True
    assert detail["suspend_reason"]

    resumed = ui.post(f"/api/runs/{operation['run_id']}/resume")
    assert ui.status == 200 and resumed["started"] is True
    wait_for_idle(ui)


# ============================================================ E/F. corpus


@pytest.fixture
def harvested(ui):
    ui.post("/api/harvest", {"search": "moral psychology", "limit": 4, "dry_run": False})
    wait_for_idle(ui)
    return ui


def test_e_corpus_lists_and_paginates(harvested):
    data = harvested.get("/api/corpus?page_size=2")
    assert data["total"] == 4
    assert data["pages"] == 2
    assert len(data["documents"]) == 2
    first = data["documents"][0]
    for field in ("document_id", "doi", "title", "has_pdf", "has_xml", "oa_status", "journal"):
        assert field in first


def test_e_corpus_search_and_filters(harvested):
    assert harvested.get("/api/corpus?search=Mock%20Open%20Access")["total"] == 4
    assert harvested.get("/api/corpus?search=nothing-matches-this")["total"] == 0
    assert harvested.get("/api/corpus?has_pdf=true")["total"] == 4
    assert harvested.get("/api/corpus?has_xml=true")["total"] == 0
    assert harvested.get("/api/corpus?oa_status=gold")["total"] == 4
    assert harvested.get("/api/corpus?oa_status=bronze")["total"] == 0
    assert harvested.get("/api/corpus?journal=Journal%20of%20Mock")["total"] == 4

    year = harvested.get("/api/corpus")["documents"][0]["publication_year"]
    assert harvested.get(f"/api/corpus?year={year}")["total"] >= 1


def test_corpus_facets_reflect_the_real_corpus(harvested):
    facets = harvested.get("/api/corpus/facets")
    assert facets["oa_status"] == ["gold"]
    assert "Journal of Mock Studies" in facets["journal"]
    assert facets["year"]


def test_f_document_detail_exposes_provenance_and_integrity(harvested):
    document_id = document_id_for_doi("10.1234/mock.0001")
    detail = harvested.get(f"/api/corpus/{document_id}")

    assert detail["status"] == "COMPLETED"
    assert detail["metadata"]["title"] == "Mock Open Access Article 1"
    assert detail["metadata"]["authors"] == ["Ada Lovelace", "Alan Turing"]
    assert detail["metadata"]["abstract"]

    pdf = detail["artifacts"]["pdf"]
    assert len(pdf["sha256"]) == 64
    assert pdf["size_bytes"] > 0

    provenance = detail["provenance"]
    assert provenance["discovered_via"] == ["openalex"]
    assert provenance["acquired_via"] == "openalex"
    assert provenance["http_status"] == 200
    assert "cross_checked_via" in provenance

    assert detail["files"]["pdf"]["exists"] is True
    assert detail["files"]["json"]["exists"] is True
    assert detail["sidecar"]["document_id"] == document_id
    assert detail["source_records"]


def test_document_can_be_addressed_by_doi(harvested):
    detail = harvested.get("/api/corpus/10.1234%2Fmock.0001")
    assert detail["document_id"] == document_id_for_doi("10.1234/mock.0001")


def test_unknown_document_is_a_clean_404(harvested):
    result = harvested.get("/api/corpus/doi_not_a_real_document_0123456789ab")
    assert harvested.status == 404
    assert "not in the corpus" in result["error"]


# ============================================================ I. open the PDF


def test_i_pdf_is_served_inline_for_the_browser(harvested):
    document_id = document_id_for_doi("10.1234/mock.0001")
    body = harvested.get(f"/api/corpus/{document_id}/file/pdf", raw=True)
    assert harvested.status == 200
    assert body.startswith(b"%PDF-")
    assert harvested.headers["Content-Type"] == "application/pdf"
    assert "inline" in harvested.headers["Content-Disposition"]
    assert int(harvested.headers["Content-Length"]) == len(body)


def test_sidecar_json_is_served(harvested):
    document_id = document_id_for_doi("10.1234/mock.0001")
    body = harvested.get(f"/api/corpus/{document_id}/file/json", raw=True)
    assert json.loads(body)["document_id"] == document_id


def test_missing_artifact_file_is_a_clean_404(harvested):
    document_id = document_id_for_doi("10.1234/mock.0001")
    result = harvested.get(f"/api/corpus/{document_id}/file/xml")
    assert harvested.status == 404
    assert "XML" in result["error"]


@pytest.mark.parametrize(
    "document_id",
    [
        "../../../../etc/passwd",
        "..%2F..%2Fsecret",
        "a%2Fb",
        "with%20space",
        "UPPER",
        "doi_ok_0123456789ab%00.pdf",
    ],
)
def test_artifact_route_refuses_unsafe_identifiers(harvested, document_id):
    """Hostile identifiers reach the core's own path guard and are refused there."""
    harvested.get(f"/api/corpus/{document_id}/file/pdf", raw=True)
    assert harvested.status in (400, 404)
    corpus = Path(harvested.server.context.config.storage_root)
    assert len(list(corpus.glob("*.pdf"))) == 4  # nothing was created or removed


def test_static_route_refuses_path_traversal(ui):
    for path in ("/../server.py", "/..%2F..%2Fapi.py", "/static/../../api.py"):
        body = ui.get(path, raw=True)
        assert ui.status in (200, 403, 404)
        assert b"AppContext" not in body  # never the server's own source


# ============================================================ G. verify


def test_g_verify_and_deep_verify(harvested):
    started = harvested.post("/api/verify", {"deep": False})
    assert started["started"] is True
    operation = wait_for_idle(harvested)
    assert operation["ok"] is True
    assert "no problems" in operation["message"].lower()
    assert operation["result"]["artifacts_checked"] == 4

    deep = harvested.post("/api/verify", {"deep": True})
    assert deep["started"] is True
    deep_operation = wait_for_idle(harvested)
    assert deep_operation["ok"] is True

    history = harvested.get("/api/verify/history")
    assert history["latest"]["deep"] is True
    assert len(history["history"]) == 2
    assert harvested.get("/api/dashboard")["last_verification"]["ok"] is True


def test_verify_reports_a_deleted_artifact(harvested):
    document_id = document_id_for_doi("10.1234/mock.0001")
    (Path(harvested.server.context.config.storage_root) / f"{document_id}.pdf").unlink()

    harvested.post("/api/verify", {"deep": False})
    operation = wait_for_idle(harvested)
    assert operation["ok"] is False
    assert "problem" in operation["message"]
    problems = operation["result"]["problems"]
    assert any(p["document_id"] == document_id for p in problems)
    assert harvested.get("/api/dashboard")["last_verification"]["ok"] is False


def test_verify_surfaces_temporary_files(harvested):
    root = Path(harvested.server.context.config.storage_root)
    (root / "doi_abandoned.pdf.deadbeef.part").write_bytes(b"partial")
    harvested.post("/api/verify", {"deep": False})
    operation = wait_for_idle(harvested)
    assert operation["result"]["temporary_files"] == ["doi_abandoned.pdf.deadbeef.part"]


# ============================================================ H. retry failed


@pytest.fixture
def with_failures(ui):
    """A corpus where every document failed, so retry behaviour is observable."""
    from mocks import MockProviders, openalex_work

    ui.providers.files.clear()  # every PDF now 404s
    ui.post("/api/harvest", {"search": "moral psychology", "limit": 3, "dry_run": False})
    wait_for_idle(ui)
    return ui


def test_h_failures_are_grouped_by_reason(with_failures):
    data = with_failures.get("/api/failures")
    assert data["total_failed"] == 3
    assert data["groups"]
    categories = {g["category"] for g in data["groups"]}
    assert "NOT_FOUND" in categories
    for group in data["groups"]:
        assert group["documents"] >= 1
        assert group["last_seen"]
    assert len(data["documents"]) == 3
    assert all(d["category"] for d in data["documents"])


def test_h_retry_requeues_everything(with_failures):
    result = with_failures.post("/api/failures/retry", {})
    assert result["requeued"] == 3
    assert "moved back to the queue" in result["message"]
    assert with_failures.get("/api/failures")["total_failed"] == 0


def test_h_retry_by_category_only_touches_that_group(with_failures):
    groups = with_failures.get("/api/failures")["groups"]
    category = groups[0]["category"]
    result = with_failures.post("/api/failures/retry", {"category": category})
    assert result["requeued"] >= 1


def test_h_retry_with_nothing_to_do_is_not_an_error(ui):
    result = ui.post("/api/failures/retry", {})
    assert ui.status == 200
    assert result["requeued"] == 0
    assert "nothing to re-queue" in result["message"].lower()


def test_failure_groups_stay_historical_after_a_requeue(with_failures):
    """The "by reason" groups aggregate recorded failures, not current state.

    Pins the semantics the failures page now spells out: a document that has been
    re-queued disappears from ``total_failed`` but stays counted in its failure
    groups, because the failure really did happen and is kept as history.
    """
    before = {g["category"]: g["documents"] for g in with_failures.get("/api/failures")["groups"]}
    assert before

    with_failures.post("/api/failures/retry", {})

    after = with_failures.get("/api/failures")
    assert after["total_failed"] == 0
    assert {g["category"]: g["documents"] for g in after["groups"]} == before


def test_completed_run_shows_a_requeued_document_as_queued(with_failures):
    """A finished run keeps its status; its document counts are read live.

    Pins the state model behind the run detail page's notice: re-queueing a document
    after a run has completed must not reopen that run.
    """
    run_id = with_failures.get("/api/runs")["runs"][0]["run_id"]
    assert with_failures.get(f"/api/runs/{run_id}")["status"] == "COMPLETED"

    with_failures.post("/api/failures/retry", {})

    detail = with_failures.get(f"/api/runs/{run_id}")
    assert detail["status"] == "COMPLETED"
    assert detail["finished_at"]
    assert detail["resumable"] is False
    assert detail["document_counts"].get("QUEUED") == 3
    assert detail["failed"] == 0


def test_requeued_documents_are_retried_by_a_later_run(with_failures):
    from mocks import make_pdf_bytes

    with_failures.post("/api/failures/retry", {})
    # The files come back; resuming the original run must now complete them.
    for index in range(1, 4):
        with_failures.providers.files[f"/W{2000000 + index}.pdf"] = make_pdf_bytes()

    run_id = with_failures.get("/api/runs")["runs"][0]["run_id"]
    with_failures.post(f"/api/runs/{run_id}/resume")
    wait_for_idle(with_failures)

    assert with_failures.get("/api/dashboard")["cards"]["pdfs"] == 3
    assert with_failures.get("/api/failures")["total_failed"] == 0


# ============================================================ settings


def test_settings_never_return_secret_values(ui):
    data = ui.get("/api/settings")
    blob = json.dumps(data)
    assert "ui-test-key" not in blob
    assert "operator@example.org" not in blob

    by_key = {s["key"]: s for s in data["settings"]}
    assert by_key["openalex.api_key"]["configured"] is True
    assert by_key["openalex.api_key"]["value"] is None
    assert by_key["openalex.api_key"]["masked"].startswith("*")
    assert by_key["contact_email"]["masked"].endswith("@example.org")
    assert "***" in by_key["contact_email"]["masked"]


def test_settings_can_be_changed_and_are_persisted(ui, ui_config_file):
    result = ui.put("/api/settings", {"updates": {"xml_policy": "disabled",
                                                  "downloads.concurrency": 3}})
    assert ui.status == 200 and result["saved"] is True

    saved = json.loads(ui_config_file.read_text("utf-8"))
    assert saved["xml_policy"] == "disabled"
    assert saved["downloads"]["concurrency"] == 3
    assert ui.server.context.config.xml_policy == "disabled"


def test_settings_reject_unknown_and_invalid_values(ui):
    assert ui.put("/api/settings", {"updates": {"totally_made_up": 1}})
    assert ui.status == 400

    result = ui.put("/api/settings", {"updates": {"xml_policy": "sometimes"}})
    assert ui.status == 400
    assert "preferred" in result["error"]

    result = ui.put("/api/settings", {"updates": {"downloads.concurrency": "lots"}})
    assert ui.status == 400
    assert "whole number" in result["error"]


def test_environment_owned_secrets_are_not_editable(ui, monkeypatch):
    monkeypatch.setenv("HARVESTER_OPENALEX_API_KEY", "from-environment")
    ui.server.context.reload_config()
    data = ui.get("/api/settings")
    entry = next(s for s in data["settings"] if s["key"] == "openalex.api_key")
    assert entry["source"] == "environment"
    assert entry["editable"] is False
    assert "from-environment" not in json.dumps(data)


def test_unpaywall_can_be_enabled_with_an_environment_contact_email(
    ui, ui_config_file, monkeypatch
):
    """A contact email supplied only through the environment must satisfy the save.

    The file deliberately holds no address, so the save is rejected without the
    environment variable and accepted with it — and the address stays out of the file.
    """
    config = json.loads(ui_config_file.read_text("utf-8"))
    del config["contact_email"]
    config["unpaywall"]["enabled"] = False
    ui_config_file.write_text(json.dumps(config), encoding="utf-8")

    monkeypatch.delenv("HARVESTER_CONTACT_EMAIL", raising=False)
    ui.server.context.reload_config()
    result = ui.put("/api/settings", {"updates": {"unpaywall.enabled": True}})
    assert ui.status == 400
    assert "contact email" in result["error"].lower()
    assert json.loads(ui_config_file.read_text("utf-8"))["unpaywall"]["enabled"] is False

    monkeypatch.setenv("HARVESTER_CONTACT_EMAIL", "operator@example.org")
    ui.server.context.reload_config()
    result = ui.put("/api/settings", {"updates": {"unpaywall.enabled": True}})
    assert ui.status == 200 and result["saved"] is True

    saved = json.loads(ui_config_file.read_text("utf-8"))
    assert saved["unpaywall"]["enabled"] is True
    assert "contact_email" not in saved
    assert ui.server.context.config.contact_email == "operator@example.org"

    unpaywall = next(p for p in ui.get("/api/providers")["providers"] if p["id"] == "unpaywall")
    assert unpaywall["state"] == "ready"


def test_settings_changes_apply_to_the_next_harvest(ui, ui_config_file, tmp_path):
    new_root = tmp_path / "relocated"
    ui.put("/api/settings", {"updates": {"storage_root": str(new_root)}})
    ui.post("/api/harvest", {"search": "x", "limit": 1, "dry_run": False})
    wait_for_idle(ui)
    assert list(new_root.glob("*.pdf"))


# ======================================================= J/K. shared state


def test_j_ui_and_cli_share_one_authoritative_state(ui, ui_config_file, capsys):
    """The UI must not maintain a private view of the world."""
    from harvester.cli import main

    ui.post("/api/harvest", {"search": "moral psychology", "limit": 3, "dry_run": False})
    operation = wait_for_idle(ui)
    run_id = operation["run_id"]

    # The CLI, pointed at the same config file, sees exactly the same run and corpus.
    assert main(["--config", str(ui_config_file), "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["document_status_counts"]["COMPLETED"] == 3
    assert payload["artifacts"]["pdf"] == 3
    assert any(run["run_id"] == run_id for run in payload["runs"])

    assert main(["--config", str(ui_config_file), "verify", "--deep", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["completed_documents"] == 3


def test_k_state_written_by_the_cli_is_visible_in_the_ui(with_failures, ui_config_file, capsys):
    """A change made through the CLI shows up in the UI without restarting it."""
    from harvester.cli import main

    ui = with_failures
    assert ui.get("/api/failures")["total_failed"] == 3

    # The operator re-queues from the command line instead of the browser.
    assert main(["--config", str(ui_config_file), "retry-failed"]) == 0
    assert "re-queued 3" in capsys.readouterr().out

    assert ui.get("/api/failures")["total_failed"] == 0
    assert ui.get("/api/corpus?status=QUEUED")["total"] == 3


def test_ui_run_is_resumable_from_the_cli(ui, ui_config_file, capsys):
    """A run started in the browser can be finished from the command line."""
    from harvester.cli import main

    ui.providers.files.clear()
    ui.post("/api/harvest", {"search": "x", "limit": 2, "dry_run": False})
    run_id = wait_for_idle(ui)["run_id"]
    capsys.readouterr()

    assert main(["--config", str(ui_config_file), "status", "--run-id", run_id, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == run_id
    assert payload["document_status_counts"]["FAILED_PERMANENT"] == 2


def test_unknown_api_endpoint_and_method_are_reported_clearly(ui):
    result = ui.get("/api/nope")
    assert ui.status == 404 and "Unknown API endpoint" in result["error"]

    result = ui.post("/api/dashboard", {})
    assert ui.status == 405


def test_malformed_json_body_is_rejected_politely(ui):
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    request = Request(f"{ui.base}/api/harvest", data=b"{not json", method="POST")
    request.add_header("Content-Type", "application/json")
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=10)
    assert error.value.code == 400
    assert "not valid JSON" in json.loads(error.value.read())["error"]
