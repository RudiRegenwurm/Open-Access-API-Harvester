"""A saved setting must survive save, reload and restart unchanged.

Regression cover for a setting that changed itself. `advisor.effort = default` was
saved correctly, but the Settings page then rendered a select in which no option
matched — a select with nothing selected reports its *first* option instead, the page
displayed that invented value as though it were configured, and because saving submits
every field, the next save wrote it into the configuration file. The operator's chosen
value was replaced by `low` without anyone touching it, and Assisted Search started
sending an `effort` the model rejects.

The same class of bug had a second entrance: `describe()` decided what the environment
controls from a local list covering only secrets, while `Config.load()` honours a much
larger table. Any other environment-overridden setting was therefore shown as an
ordinary editable file value, and saving the form wrote the environment's value into
the file.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from harvester.config import Config, env_names_for, env_override_for
from harvester.webui import settings_store

import harvester.webui

STATIC = Path(harvester.webui.__file__).parent / "static" / "app.js"


def effort_entry(payload: dict) -> dict:
    return {s["key"]: s for s in payload["settings"]}["advisor.effort"]


def stored_effort(path: Path) -> object:
    return json.loads(path.read_text("utf-8")).get("advisor", {}).get("effort")


# =================================================== A. saving keeps what was chosen


def test_a_an_unset_effort_is_saved_verbatim(ui, ui_config_file):
    result = ui.put("/api/settings", {"updates": {"advisor.effort": "default"}})
    assert ui.status == 200, result

    assert stored_effort(ui_config_file) == "default"

    entry = effort_entry(ui.get("/api/settings"))
    assert entry["value"] == "default"
    assert entry["source"] == "file"
    assert entry["editable"] is True


# ============================================================ B. reload keeps it too


def test_b_a_config_reload_keeps_the_unset_effort(ui, ui_config_file):
    ui.put("/api/settings", {"updates": {"advisor.effort": "default"}})

    ui.server.context.reload_config()

    assert ui.server.context.config.advisor.effort == "default"
    assert effort_entry(ui.get("/api/settings"))["value"] == "default"
    assert stored_effort(ui_config_file) == "default"


# ===================================================== C. and so does a full restart


def test_c_the_unset_effort_survives_save_reload_and_restart(ui, ui_config_file):
    """Save, read back, reload, restart, read again. Never `low` at any point."""
    ui.put("/api/settings", {"updates": {"advisor.effort": "default"}})
    assert effort_entry(ui.get("/api/settings"))["value"] == "default"

    ui.server.context.reload_config()
    assert effort_entry(ui.get("/api/settings"))["value"] == "default"

    # A restart is a fresh load of the same file by a process that shares nothing
    # with the one that wrote it.
    restarted = Config.load(config_path=ui_config_file, env={})
    assert restarted.advisor.effort == "default"

    fresh = settings_store.describe(restarted, ui_config_file, env={})
    assert effort_entry(fresh)["value"] == "default"
    assert stored_effort(ui_config_file) == "default"

    # And the advisor still asks for no effort after all of that.
    from harvester.advisor import build_advisor

    advisor = build_advisor(restarted)
    try:
        body = advisor.build_request("a question", None)
    finally:
        advisor.close()
    assert "effort" not in body["output_config"]
    assert body["output_config"]["format"]["type"] == "json_schema"


def test_c_a_save_of_other_settings_does_not_disturb_the_effort(ui, ui_config_file):
    """The form submits every field, so an unrelated save must not rewrite this one."""
    ui.put("/api/settings", {"updates": {"advisor.effort": "default"}})

    ui.put("/api/settings", {"updates": {"xml_policy": "required", "log_level": "INFO"}})
    assert ui.status == 200

    assert stored_effort(ui_config_file) == "default"


# ========================================================== D. environment overrides


def test_d_an_environment_override_is_reported_as_such(ui_config_file, monkeypatch):
    """Not just for secrets: for every setting `Config.load` reads from the environment."""
    monkeypatch.setenv("HARVESTER_ADVISOR_EFFORT", "high")

    config = Config.load(config_path=ui_config_file)
    entry = effort_entry(settings_store.describe(config, ui_config_file))

    assert entry["source"] == "environment"
    assert entry["editable"] is False
    assert entry["env_var"] == "HARVESTER_ADVISOR_EFFORT"


def test_d_an_environment_controlled_setting_is_not_written_to_the_file(
    ui_config_file, monkeypatch
):
    monkeypatch.setenv("HARVESTER_ADVISOR_EFFORT", "high")
    settings_store.apply_updates(ui_config_file, {"advisor.effort": "default"}, env={})
    assert stored_effort(ui_config_file) == "default"

    from harvester.errors import ConfigurationError

    with pytest.raises(ConfigurationError) as excinfo:
        settings_store.apply_updates(ui_config_file, {"advisor.effort": "low"})

    assert "HARVESTER_ADVISOR_EFFORT" in str(excinfo.value)
    assert stored_effort(ui_config_file) == "default"   # untouched


def test_d_environment_detection_comes_from_the_loader_s_own_table():
    """No second list. The UI and the loader answer this question from one source."""
    assert env_names_for("advisor.effort") == ["HARVESTER_ADVISOR_EFFORT"]
    assert env_names_for("advisor.api_key") == [
        "ANTHROPIC_API_KEY",
        "HARVESTER_ADVISOR_API_KEY",
    ]
    assert env_names_for("openalex.per_page") == ["HARVESTER_OPENALEX_PER_PAGE"]

    # Every editable setting that the loader can read from the environment is
    # detectable here — the drift that started this could not survive this assertion.
    for dotted in settings_store.EDITABLE_SETTINGS:
        names = env_names_for(dotted)
        for name in names:
            assert env_override_for(dotted, {name: "x"}) == name

    # An empty variable is not an override, matching what the loader does with it.
    assert env_override_for("advisor.effort", {"HARVESTER_ADVISOR_EFFORT": ""}) is None
    assert env_override_for("advisor.effort", {}) is None


def test_d_every_environment_backed_setting_is_covered_not_just_secrets():
    covered = [d for d in settings_store.EDITABLE_SETTINGS if env_names_for(d)]
    secrets = [d for d in covered if settings_store.EDITABLE_SETTINGS[d] == "secret"]
    assert len(covered) > len(secrets), covered


# ============================================== E. a value that is not on offer


def test_e_a_stored_value_outside_the_offered_set_is_reported_as_stored(
    ui_config_file, monkeypatch
):
    """The API must not launder an unexpected value into an allowed one."""
    data = json.loads(ui_config_file.read_text("utf-8"))
    data["advisor"]["effort"] = "default"
    ui_config_file.write_text(json.dumps(data), encoding="utf-8")

    # Pretend this server build does not offer "default" any more.
    monkeypatch.setitem(
        settings_store.EDITABLE_SETTINGS, "advisor.effort", "choice:low,medium,high"
    )
    config = Config.load(config_path=ui_config_file, env={})
    entry = effort_entry(settings_store.describe(config, ui_config_file, env={}))

    assert entry["value"] == "default"     # not "low", not silently corrected
    assert stored_effort(ui_config_file) == "default"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the UI code")
def test_e_the_select_never_falls_back_to_the_first_option(tmp_path):
    """Run the real `choiceOptions` from app.js and check what it selects.

    Asserting on the source text would pass on a comment. This executes it.
    """
    source = STATIC.read_text("utf-8")
    harness = tmp_path / "check.mjs"
    harness.write_text(
        source[: source.index("function num(")]
        + """
const out = [];
const selected = (html) => {
  const m = [...html.matchAll(/<option value="([^"]*)"([^>]*)>/g)]
    .filter(([, , attrs]) => attrs.includes('selected'));
  return m.map(([, value]) => value);
};
const unknownFlag = (html) => html.includes('data-unknown="true"');

// 1. an offered value selects exactly itself
const known = choiceOptions('choice:default,low,medium', 'medium');
out.push(['known.selected', selected(known)]);
out.push(['known.unknown', unknownFlag(known)]);
out.push(['known.explicitValues', /<option value="default"/.test(known)]);

// 2. a value that is not offered must still be the selected one
const alien = choiceOptions('choice:low,medium,high', 'default');
out.push(['alien.selected', selected(alien)]);
out.push(['alien.unknown', unknownFlag(alien)]);
out.push(['alien.optionCount', (alien.match(/<option /g) || []).length]);

// 3. the first option is never selected just because nothing matched
out.push(['alien.firstIsSelected', /<option value="low"[^>]*selected/.test(alien)]);

// 4. an empty value is not laundered into the first option either
const empty = choiceOptions('choice:low,medium', '');
out.push(['empty.selected', selected(empty)]);
out.push(['empty.unknown', unknownFlag(empty)]);

console.log(JSON.stringify(Object.fromEntries(out)));
""",
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)

    assert result["known.selected"] == ["medium"]
    assert result["known.unknown"] is False
    assert result["known.explicitValues"] is True

    # The configured value stays selected even though the server no longer offers it.
    assert result["alien.selected"] == ["default"]
    assert result["alien.unknown"] is True
    assert result["alien.optionCount"] == 4          # the three offered plus this one
    assert result["alien.firstIsSelected"] is False  # <- the bug

    assert result["empty.selected"] == [""]
    assert result["empty.unknown"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the UI code")
def test_e_the_save_leaves_an_unoffered_value_alone(tmp_path):
    """The save must skip a select still resting on a value it cannot submit."""
    source = STATIC.read_text("utf-8")
    body = source[source.index("  async saveSettings()"):]
    body = body[: body.index("\n  },")] + "\n  }"

    harness = tmp_path / "save.mjs"
    harness.write_text(
        """
const collected = {};
const elements = [
  { dataset: { key: 'advisor.model' }, value: 'claude-haiku-4-5', type: 'select-one',
    selectedOptions: [{ dataset: {} }] },
  { dataset: { key: 'advisor.effort' }, value: 'default', type: 'select-one',
    selectedOptions: [{ dataset: { unknown: 'true' } }] },
  { dataset: { key: 'xml_policy' }, value: 'preferred', type: 'select-one',
    selectedOptions: [{ dataset: {} }] },
  { dataset: { key: 'openalex.api_key' }, value: '', type: 'password',
    disabled: true, selectedOptions: null },
];
global.document = { querySelectorAll: () => elements };
const api = async (_path, options) => { Object.assign(collected, options.body.updates); };
const guard = async (fn) => fn();
const toast = () => {};
const rerender = () => {};
const actions = {
"""
        + body
        + """
};
await actions.saveSettings();
console.log(JSON.stringify(collected));
""",
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, completed.stderr
    sent = json.loads(completed.stdout)

    assert "advisor.effort" not in sent      # <- never written back
    assert sent["advisor.model"] == "claude-haiku-4-5"
    assert sent["xml_policy"] == "preferred"
    assert "openalex.api_key" not in sent    # disabled fields stay out, as before
