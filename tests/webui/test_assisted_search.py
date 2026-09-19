"""Assisted Search V1 end to end through the real HTTP server.

Covers the specification's integration and adversarial requirements: the assisted
flow (§53), preview side effects (§54), query modification after preview (§55),
advisor failure (§56), conventional regression (§52) and run provenance (§31-33).

Every provider — OpenAlex, Europe PMC, Unpaywall and the query advisor — is the same
deterministic in-process mock. Nothing here reaches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harvester.preview import query_fingerprint
from harvester.providers.openalex import OpenAlexQuery
from harvester.state import StateStore
from mocks import Behavior

from conftest import wait_for_idle

QUESTION = (
    "Ich möchte herausfinden, ob ADHS-Symptome im Erwachsenenalter zeitweise "
    "remittieren und später erneut auftreten können und welche Rolle veränderte "
    "Lebensanforderungen dabei spielen."
)
FILTERS = {"from_year": 2015, "to_year": 2025, "oa_status": "gold"}


def preview_body(search: str, **overrides: object) -> dict:
    body = {"search": search, "limit": 10, **FILTERS}
    body.update(overrides)
    return body


def state_snapshot(ui) -> dict:
    """Everything a preview must leave untouched."""
    config = ui.server.context.config
    with StateStore(config.state_db) as store:
        documents, total = store.query_documents(limit=500)
        return {
            "runs": [run.run_id for run in store.list_runs(100)],
            "documents": total,
            "document_ids": sorted(d["document_id"] for d in documents),
            "counts": store.status_counts(),
            "artifacts": store.artifact_counts(),
            "failures": len(store.failed_document_ids()),
            "files": sorted(p.name for p in Path(config.storage_root).rglob("*") if p.is_file()),
        }


# ================================================== A. advisor endpoint contract


def test_a_advice_returns_one_recommendation_with_a_rationale(ui):
    result = ui.post("/api/search/advice", {"research_question": QUESTION, **FILTERS})
    assert ui.status == 200
    assert result["recommended_query"]
    assert result["rationale"]
    assert result["deferred_terms"] == ["environmental demands"]
    assert result["provider"] == "anthropic"
    assert result["model"]
    assert result["prompt_version"]
    assert result["research_question"] == QUESTION


def test_a_the_recommended_query_is_materially_more_compact_than_the_question(ui):
    """The motivating product case (§58): a retrieval query, not a restatement."""
    result = ui.post("/api/search/advice", {"research_question": QUESTION})
    query = result["recommended_query"]
    assert len(query) < len(QUESTION) / 2
    assert "\n" not in query


def test_a_active_filters_reach_the_advisor(ui):
    ui.post(
        "/api/search/advice",
        {
            "research_question": QUESTION,
            "topic_id": "T10159",
            "primary_topic_only": True,
            "from_year": 2015,
            "oa_status": "gold",
        },
    )
    content = ui.providers.advisor_requests[0]["messages"][0]["content"]
    supplied = json.loads(content.split("<active_filters_and_question>")[1].split("</")[0])
    assert supplied["active_filters"]["topic_id"] == "T10159"
    assert supplied["active_filters"]["primary_topic_only"] is True
    assert supplied["active_filters"]["from_publication_year"] == 2015


def test_a_an_empty_question_is_refused_without_calling_the_advisor(ui):
    result = ui.post("/api/search/advice", {"research_question": "   "})
    assert ui.status == 400
    assert result["detail"]["reason"] == "invalid_question"
    assert ui.providers.advisor_requests == []


def test_a_malformed_advice_never_reaches_the_client(ui):
    ui.providers.advisor_advice = {"recommended_query": ""}
    result = ui.post("/api/search/advice", {"research_question": QUESTION})
    assert ui.status == 502
    assert result["detail"]["reason"] == "invalid_response"
    assert "recommended_query" not in result


# ========================================================= B. discovery preview


def test_b_preview_returns_at_most_ten_real_discovery_results(ui):
    result = ui.post("/api/search/preview", preview_body("moral psychology"))
    assert ui.status == 200
    assert 0 < len(result["results"]) <= 10
    assert result["count"] == len(result["results"])
    first = result["results"][0]
    assert first["title"] and first["publication_year"] and first["doi"]
    assert first["discovered_via"] == ["openalex"]
    assert result["fingerprint"]


def test_b_preview_creates_no_persistent_state_whatsoever(ui):
    """Section 54: snapshot, preview, prove nothing moved."""
    before = state_snapshot(ui)
    for _ in range(3):
        ui.post("/api/search/preview", preview_body("moral psychology"))
        assert ui.status == 200
    after = state_snapshot(ui)

    assert after == before
    assert after["runs"] == []
    assert after["documents"] == 0
    assert after["artifacts"] == {} or after["artifacts"].get("pdf", 0) == 0
    assert after["files"] == []
    # No acquisition was attempted: no file host and no fallback provider was touched.
    assert ui.providers.count("unpaywall.doi") == 0
    assert not any(route.startswith("file:") for route in ui.providers.request_counts)
    # And no harvest run appears in the normal run history.
    assert ui.get("/api/runs")["runs"] == []
    assert ui.get("/api/activity")["busy"] is False


def test_b_the_side_effect_snapshot_is_sensitive_enough_to_be_worth_taking(ui):
    """Guards the test above: prove the snapshot notices state when state is made.

    A dry-run harvest — the pre-existing "preview" — *does* create a run and corpus
    documents. That is precisely why Assisted Search needed a different mechanism,
    and it makes a good positive control for the comparison.
    """
    before = state_snapshot(ui)
    ui.post("/api/harvest", {"search": "moral psychology", "limit": 2, "dry_run": True})
    wait_for_idle(ui)
    after = state_snapshot(ui)
    assert after != before
    assert after["runs"] and after["documents"] > 0


def test_b_preview_does_not_start_an_operation(ui):
    ui.post("/api/search/preview", preview_body("moral psychology"))
    activity = ui.get("/api/activity")
    assert activity["busy"] is False
    assert activity["operation"] is None


def test_b_zero_results_is_reported_without_changing_the_query(ui):
    ui.providers.works = []
    result = ui.post("/api/search/preview", preview_body("nothing at all matches this"))
    assert ui.status == 200
    assert result["results"] == []
    assert result["count"] == 0
    assert result["query"]["search"] == "nothing at all matches this"


def test_b_preview_failure_leaves_no_partial_state(ui):
    before = state_snapshot(ui)
    ui.providers.script_route(
        "openalex.works", [Behavior(status=500, json={"error": "boom"}) for _ in range(4)]
    )
    result = ui.post("/api/search/preview", preview_body("moral psychology"))
    assert ui.status == 502
    assert "could not be completed" in result["error"]
    assert state_snapshot(ui) == before


def test_b_preview_requires_something_to_search_for(ui):
    result = ui.post("/api/search/preview", {"limit": 10})
    assert ui.status == 400
    assert "search term" in result["error"]


# =========================================== C. the preview gate and staleness


def assisted_harvest_body(query: str, fingerprint: str | None, **overrides: object) -> dict:
    body = {
        "search": query,
        "limit": 2,
        "search_mode": "assisted",
        "research_question": QUESTION,
        "generated_query": query,
        "preview_fingerprint": fingerprint,
        "advisor": {
            "provider": "anthropic",
            "model": "claude-opus-5",
            "prompt_version": "assisted-search-v1",
        },
        **FILTERS,
    }
    body.update(overrides)
    return body


def test_c_an_assisted_harvest_without_a_preview_is_refused(ui):
    result = ui.post("/api/harvest", assisted_harvest_body("moral psychology", None))
    assert ui.status == 409
    assert result["detail"]["reason"] == "preview_required"
    assert ui.get("/api/runs")["runs"] == []


def test_c_a_stale_fingerprint_is_refused(ui):
    preview = ui.post("/api/search/preview", preview_body("moral psychology"))
    result = ui.post(
        "/api/harvest", assisted_harvest_body("something else entirely", preview["fingerprint"])
    )
    assert ui.status == 409
    assert result["detail"]["reason"] == "stale_preview"
    assert ui.get("/api/runs")["runs"] == []


@pytest.mark.parametrize(
    "change",
    [
        {"from_year": 2016},
        {"to_year": 2024},
        {"oa_status": "green"},
        {"topic_id": "T10159"},
        {"primary_topic_only": True},
    ],
)
def test_c_changing_any_filter_after_the_preview_invalidates_it(ui, change):
    preview = ui.post("/api/search/preview", preview_body("moral psychology"))
    body = assisted_harvest_body("moral psychology", preview["fingerprint"], **change)
    result = ui.post("/api/harvest", body)
    assert ui.status == 409
    assert result["detail"]["reason"] == "stale_preview"
    assert ui.get("/api/runs")["runs"] == []


def test_c_a_forged_fingerprint_is_refused(ui):
    ui.post("/api/search/preview", preview_body("moral psychology"))
    result = ui.post("/api/harvest", assisted_harvest_body("moral psychology", "0" * 64))
    assert ui.status == 409
    assert result["detail"]["reason"] == "stale_preview"
    assert ui.get("/api/runs")["runs"] == []


def test_c_query_modification_after_preview_round_trip(ui):
    """Section 55, in full: A previewed, B blocked, B previewed, B executed."""
    preview_a = ui.post("/api/search/preview", preview_body("moral psychology"))
    assert ui.status == 200

    # 4./5. The user edits to query B: the previous approval no longer authorises it.
    ui.post("/api/harvest", assisted_harvest_body("open science", preview_a["fingerprint"]))
    assert ui.status == 409

    # 6./7. Preview B: available again.
    preview_b = ui.post("/api/search/preview", preview_body("open science"))
    assert preview_b["fingerprint"] != preview_a["fingerprint"]

    # 8. The run uses exactly query B.
    ui.post("/api/harvest", assisted_harvest_body("open science", preview_b["fingerprint"]))
    assert ui.status == 200
    operation = wait_for_idle(ui)
    detail = ui.get(f"/api/runs/{operation['run_id']}")
    assert detail["query"]["search"] == "open science"
    assert detail["search_provenance"]["effective_search_query"] == "open science"


def test_c_the_server_recomputes_the_fingerprint_from_the_query_it_will_run(ui):
    preview = ui.post("/api/search/preview", preview_body("moral psychology"))
    expected = query_fingerprint(
        OpenAlexQuery(
            search="moral psychology",
            from_publication_year=2015,
            to_publication_year=2025,
            oa_status="gold",
        )
    )
    assert preview["fingerprint"] == expected


# ==================================================== D. the full assisted flow


def test_d_assisted_flow_runs_the_existing_pipeline_and_records_provenance(ui):
    advice = ui.post("/api/search/advice", {"research_question": QUESTION, **FILTERS})
    generated = advice["recommended_query"]

    preview = ui.post("/api/search/preview", preview_body(generated))
    assert 0 < len(preview["results"]) <= 10

    started = ui.post("/api/harvest", assisted_harvest_body(generated, preview["fingerprint"]))
    assert ui.status == 200
    assert started["started"] is True
    operation = wait_for_idle(ui)
    assert operation["ok"] is True

    detail = ui.get(f"/api/runs/{operation['run_id']}")
    provenance = detail["search_provenance"]
    assert provenance["search_mode"] == "assisted"
    assert provenance["research_question"] == QUESTION
    assert provenance["generated_query"] == generated
    assert provenance["effective_search_query"] == generated
    assert provenance["query_edited"] is False
    assert provenance["query_advisor"]["model"] == "claude-opus-5"
    assert detail["search_mode"] == "assisted"

    # The existing pipeline really ran: documents, artifacts and a report.
    assert detail["completed"] >= 1
    assert detail["artifacts"]["pdf"] >= 1
    report = ui.get(f"/api/runs/{operation['run_id']}/report")
    assert report["search_provenance"]["search_mode"] == "assisted"
    assert report["search_provenance"]["effective_search_query"] == generated


def test_d_an_edited_query_stays_distinct_from_the_generated_one(ui):
    advice = ui.post("/api/search/advice", {"research_question": QUESTION})
    generated = advice["recommended_query"]
    edited = "moral psychology"

    preview = ui.post("/api/search/preview", preview_body(edited))
    ui.post(
        "/api/harvest",
        assisted_harvest_body(edited, preview["fingerprint"], generated_query=generated),
    )
    assert ui.status == 200
    operation = wait_for_idle(ui)

    detail = ui.get(f"/api/runs/{operation['run_id']}")
    provenance = detail["search_provenance"]
    assert provenance["generated_query"] == generated
    assert provenance["effective_search_query"] == edited
    assert provenance["query_edited"] is True
    # What the harvest executed is the edited value, not the generated one.
    assert detail["query"]["search"] == edited


def test_d_the_visible_query_is_the_executed_query(ui):
    preview = ui.post("/api/search/preview", preview_body("moral psychology"))
    assert preview["query"]["search"] == "moral psychology"
    ui.post("/api/harvest", assisted_harvest_body("moral psychology", preview["fingerprint"]))
    operation = wait_for_idle(ui)
    detail = ui.get(f"/api/runs/{operation['run_id']}")
    assert detail["query"]["search"] == preview["query"]["search"]
    assert detail["query"]["filter"] == preview["query"]["filter"]


def test_d_a_double_submit_cannot_create_two_runs(ui):
    preview = ui.post("/api/search/preview", preview_body("moral psychology"))
    body = assisted_harvest_body("moral psychology", preview["fingerprint"])
    ui.post("/api/harvest", body)
    assert ui.status == 200
    second = ui.post("/api/harvest", body)
    assert ui.status == 409
    assert "still running" in second["error"]
    wait_for_idle(ui)
    assert len(ui.get("/api/runs")["runs"]) == 1


# ================================================ E. advisor failure containment


def test_e_advisor_failure_starts_no_harvest_and_keeps_the_ui_usable(ui):
    ui.providers.script_route(
        "advisor.messages", [Behavior(status=500, json={"error": "boom"}) for _ in range(4)]
    )
    result = ui.post("/api/search/advice", {"research_question": QUESTION, **FILTERS})
    assert ui.status == 502
    assert "Assisted Search is currently unavailable" in result["error"]
    assert "preserved" in result["error"]

    # Nothing started, nothing persisted.
    assert ui.get("/api/runs")["runs"] == []
    assert ui.get("/api/activity")["busy"] is False
    # And the rest of the application still answers.
    assert ui.get("/api/dashboard")["can_start"] is True
    ui.post("/api/search/preview", preview_body("moral psychology"))
    assert ui.status == 200


def test_e_an_unconfigured_advisor_reports_itself_and_blocks_nothing(ui, ui_config_file, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("HARVESTER_ADVISOR_API_KEY", raising=False)
    config = json.loads(ui_config_file.read_text("utf-8"))
    config["advisor"].pop("api_key")
    ui_config_file.write_text(json.dumps(config), encoding="utf-8")
    ui.server.context.reload_config()

    result = ui.post("/api/search/advice", {"research_question": QUESTION})
    assert ui.status == 503
    assert result["detail"]["reason"] == "not_configured"

    providers = ui.get("/api/providers")
    assert providers["advisor"]["state"] == "blocked"
    assert providers["advisor"]["required"] is False
    # Conventional Search is entirely unaffected (§52).
    assert providers["can_start"] is True
    assert providers["blockers"] == []
    ui.post("/api/harvest", {"search": "moral psychology", "limit": 1, "dry_run": False})
    assert ui.status == 200
    wait_for_idle(ui)


def test_e_a_disabled_advisor_leaves_conventional_search_working(ui, ui_config_file, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("HARVESTER_ADVISOR_API_KEY", raising=False)
    config = json.loads(ui_config_file.read_text("utf-8"))
    config["advisor"]["enabled"] = False
    ui_config_file.write_text(json.dumps(config), encoding="utf-8")
    ui.server.context.reload_config()

    assert ui.get("/api/providers")["advisor"]["state"] == "disabled"
    ui.post("/api/harvest", {"search": "moral psychology", "limit": 2, "dry_run": False})
    assert ui.status == 200
    operation = wait_for_idle(ui)
    detail = ui.get(f"/api/runs/{operation['run_id']}")
    assert detail["completed"] >= 1
    assert detail["search_provenance"]["search_mode"] == "conventional"


# ============================================== F. conventional regression path


def test_f_a_conventional_harvest_is_unchanged_and_needs_no_advisor(ui):
    ui.post("/api/harvest", {"search": "moral psychology", "limit": 2, "dry_run": False})
    assert ui.status == 200
    operation = wait_for_idle(ui)
    detail = ui.get(f"/api/runs/{operation['run_id']}")

    assert detail["query"]["search"] == "moral psychology"
    assert detail["completed"] >= 1
    assert detail["artifacts"]["pdf"] >= 1
    assert detail["search_provenance"] == {
        "search_mode": "conventional",
        "effective_search_query": "moral psychology",
    }
    # No advisor metadata is fabricated for a conventional run (§32).
    assert "query_advisor" not in detail["search_provenance"]
    assert "research_question" not in detail["search_provenance"]
    # The advisor was never consulted.
    assert ui.providers.advisor_requests == []


def test_f_a_request_without_a_search_mode_is_conventional(ui):
    """Backward compatibility: existing clients keep working untouched."""
    ui.post("/api/harvest", {"search": "moral psychology", "limit": 1, "dry_run": False})
    assert ui.status == 200
    operation = wait_for_idle(ui)
    detail = ui.get(f"/api/runs/{operation['run_id']}")
    assert detail["search_mode"] == "conventional"


def test_f_the_conventional_dry_run_preview_still_works(ui):
    ui.post("/api/harvest", {"search": "moral psychology", "limit": 2, "dry_run": True})
    assert ui.status == 200
    operation = wait_for_idle(ui)
    detail = ui.get(f"/api/runs/{operation['run_id']}")
    assert detail["dry_run"] is True
    assert detail["search_mode"] == "conventional"


def test_f_an_unknown_search_mode_is_rejected(ui):
    result = ui.post(
        "/api/harvest", {"search": "x", "limit": 1, "dry_run": False, "search_mode": "magic"}
    )
    assert ui.status == 400
    assert "Search mode must be one of" in result["error"]
    assert ui.get("/api/runs")["runs"] == []


def test_f_a_broken_advisor_setting_does_not_block_harvesting(ui, ui_config_file):
    """The advisor takes no part in discovery, so it must not gate it (§8, §52)."""
    config = json.loads(ui_config_file.read_text("utf-8"))
    config["advisor"]["effort"] = "turbo"
    ui_config_file.write_text(json.dumps(config), encoding="utf-8")
    ui.server.context.reload_config()

    providers = ui.get("/api/providers")
    assert providers["can_start"] is True
    assert providers["blockers"] == []
    assert providers["advisor"]["state"] == "blocked"
    assert "misconfigured" in providers["advisor"]["detail"]

    ui.post("/api/harvest", {"search": "moral psychology", "limit": 2, "dry_run": False})
    assert ui.status == 200
    operation = wait_for_idle(ui)
    assert ui.get(f"/api/runs/{operation['run_id']}")["completed"] >= 1


def test_f_a_resumed_assisted_run_keeps_its_provenance(ui):
    preview = ui.post("/api/search/preview", preview_body("moral psychology"))
    ui.post("/api/harvest", assisted_harvest_body("moral psychology", preview["fingerprint"]))
    operation = wait_for_idle(ui)
    run_id = operation["run_id"]

    ui.post(f"/api/runs/{run_id}/resume", {})
    assert ui.status == 200
    wait_for_idle(ui)

    detail = ui.get(f"/api/runs/{run_id}")
    assert detail["search_provenance"]["search_mode"] == "assisted"
    assert detail["search_provenance"]["research_question"] == QUESTION
    report = ui.get(f"/api/runs/{run_id}/report")
    assert report["search_provenance"]["research_question"] == QUESTION


# ============================================================= G. no secrets


def test_g_no_advisor_credential_reaches_the_browser_or_the_run_record(ui):
    advice = ui.post("/api/search/advice", {"research_question": QUESTION})
    preview = ui.post("/api/search/preview", preview_body(advice["recommended_query"]))
    ui.post(
        "/api/harvest",
        assisted_harvest_body(advice["recommended_query"], preview["fingerprint"]),
    )
    operation = wait_for_idle(ui)

    for path in (
        "/api/search/advice",
        f"/api/runs/{operation['run_id']}",
        f"/api/runs/{operation['run_id']}/report",
        "/api/settings",
        "/api/providers",
        "/api/dashboard",
    ):
        blob = json.dumps(
            ui.post(path, {"research_question": QUESTION})
            if path == "/api/search/advice"
            else ui.get(path)
        )
        assert "ui-advisor-key" not in blob, path

    with StateStore(ui.server.context.config.state_db) as store:
        run = store.get_run(operation["run_id"])
        assert "ui-advisor-key" not in json.dumps(run.search_provenance)
        assert "ui-advisor-key" not in json.dumps(run.config)


def test_g_advisor_metadata_in_a_request_cannot_smuggle_extra_fields_into_a_run(ui):
    preview = ui.post("/api/search/preview", preview_body("moral psychology"))
    body = assisted_harvest_body("moral psychology", preview["fingerprint"])
    body["advisor"] = {
        "provider": "anthropic",
        "model": "claude-opus-5",
        "prompt_version": "assisted-search-v1",
        "api_key": "sk-should-never-be-stored",
        "authorization": "Bearer nope",
    }
    ui.post("/api/harvest", body)
    operation = wait_for_idle(ui)

    detail = ui.get(f"/api/runs/{operation['run_id']}")
    advisor = detail["search_provenance"]["query_advisor"]
    assert set(advisor) == {"provider", "model", "prompt_version"}
    assert "sk-should-never-be-stored" not in json.dumps(detail)


def test_g_advisor_settings_are_editable_without_exposing_the_value(ui):
    data = ui.get("/api/settings")
    by_key = {s["key"]: s for s in data["settings"]}
    assert by_key["advisor.api_key"]["configured"] is True
    assert by_key["advisor.api_key"]["value"] is None
    assert by_key["advisor.api_key"]["masked"].startswith("*")
    assert by_key["advisor.model"]["value"] == "claude-opus-5"
    assert "ui-advisor-key" not in json.dumps(data)


def test_g_every_editable_setting_is_actually_rendered_by_the_settings_page(ui):
    """Regression: the Settings page renders an explicit allow-list of keys.

    `renderField` returns an empty string for any key missing from SETTING_GROUPS, so
    a setting can be fully wired through the API, be saveable, and still be invisible
    to the operator — which is exactly what happened to the advisor settings. Asserting
    on the API response alone did not catch it.
    """
    keys = [s["key"] for s in ui.get("/api/settings")["settings"]]
    body = ui.get("/app.js", raw=True).decode("utf-8")
    groups = body.split("const SETTING_GROUPS")[1].split("const SETTING_LABELS")[0]

    missing = [key for key in keys if f"'{key}'" not in groups]
    assert missing == [], f"editable but never rendered: {missing}"


def test_g_an_unset_advisor_effort_survives_a_settings_roundtrip(ui, ui_config_file):
    """The operator must be able to choose "no particular effort" from the UI.

    A model that accepts no effort parameter rejects a request that carries one, so
    this is the setting that makes such a model usable at all. It has to be offered,
    saved, written to the config file and read back as a *valid* advisor.
    """
    offered = {s["key"]: s for s in ui.get("/api/settings")["settings"]}
    assert "default" in offered["advisor.effort"]["spec"]

    result = ui.put(
        "/api/settings",
        {"updates": {"advisor.effort": "default", "advisor.model": "claude-haiku-4-5"}},
    )
    assert ui.status == 200, result

    stored = json.loads(ui_config_file.read_text("utf-8"))
    assert stored["advisor"]["effort"] == "default"
    assert stored["advisor"]["model"] == "claude-haiku-4-5"

    ui.server.context.reload_config()
    by_key = {s["key"]: s for s in ui.get("/api/settings")["settings"]}
    assert by_key["advisor.effort"]["value"] == "default"

    advisor = ui.get("/api/providers")["advisor"]
    assert advisor["state"] == "ready"
    assert advisor["model"] == "claude-haiku-4-5"

    advice = ui.post("/api/search/advice", {"research_question": QUESTION})
    assert ui.status == 200
    assert advice["recommended_query"]


def test_g_an_unknown_advisor_effort_is_still_refused(ui):
    result = ui.put("/api/settings", {"updates": {"advisor.effort": "turbo"}})
    assert ui.status == 400
    assert "advisor.effort" in result["error"]


def test_g_the_advisor_settings_are_grouped_and_labelled(ui):
    body = ui.get("/app.js", raw=True).decode("utf-8")
    assert "title: 'Assisted Search'" in body
    for key, label in (
        ("advisor.enabled", "Assisted Search enabled"),
        ("advisor.api_key", "Query advisor API key"),
        ("advisor.model", "Query advisor model"),
        ("advisor.effort", "Query advisor effort"),
    ):
        assert f"'{key}': ['{label}'" in body, key


# =========================================================== H. the UI shell


def test_h_the_hidden_attribute_actually_hides(ui):
    """Regression: the stale-preview warning is toggled with the `hidden` attribute.

    `.notice { display: flex }` beats the browser's own `[hidden] { display: none }`,
    so without an explicit rule the warning stayed visible on a *current* preview —
    a false stale warning next to an enabled harvest button. Found in the browser.
    """
    css = ui.get("/styles.css", raw=True).decode("utf-8")
    assert ".notice {" in css and "display: flex" in css      # the conflicting rule
    assert "[hidden] { display: none !important; }" in css    # the rule that wins


def test_h_the_shell_offers_both_search_modes(ui):
    body = ui.get("/app.js", raw=True).decode("utf-8")
    for marker in (
        "Conventional Search",
        "Assisted Search",
        "What are you researching?",
        "Recommended search query",
        "Why this query?",
        "Deferred for now",
        "Search preview",
        "Start full harvest",
        "No results found",
        "Your research question is sent to the configured query-advisor service",
    ):
        assert marker in body, marker
