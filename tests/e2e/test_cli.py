"""End-to-end CLI tests (MASTER_SPEC sections 33, 34, 35, 36).

Covers AC-018 (usable help and meaningful exit codes) and AC-016 (run summary),
plus the verification command and the full documented exit-code contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harvester.cli import build_parser, main
from harvester.errors import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    EXIT_SUSPENDED,
)
from harvester.identity import document_id_for_doi
from mocks import Behavior, MockProviders, make_html_bytes, make_pdf_bytes, openalex_work

COMMANDS = [
    "harvest",
    "discover",
    "resume",
    "status",
    "inspect",
    "verify",
    "retry-failed",
    "evidence-export",
    "evidence-restore",
]


# ===================================================================== AC-018


def test_ac018_top_level_help_lists_every_command(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == EXIT_OK
    out = capsys.readouterr().out
    for command in COMMANDS:
        assert command in out


@pytest.mark.parametrize("command", COMMANDS)
def test_ac018_every_command_has_usable_help(command, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main([command, "--help"])
    assert exit_info.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "usage:" in out
    assert len(out) > 100


def test_ac018_version_is_reported(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == EXIT_OK
    assert "harvester" in capsys.readouterr().out


def test_ac018_no_command_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code != EXIT_OK


def test_ac018_unknown_command_is_a_usage_error():
    with pytest.raises(SystemExit) as exit_info:
        main(["nonsense"])
    assert exit_info.value.code != EXIT_OK


def test_canonical_topic_flag_exists_and_concept_flag_does_not(capsys):
    """SPEC_PATCH section 1: --topic-id is canonical; --concept-id does not exist."""
    with pytest.raises(SystemExit):
        main(["harvest", "--help"])
    out = capsys.readouterr().out
    assert "--topic-id" in out
    assert "--concept-id" not in out


def test_parser_rejects_an_invalid_xml_policy():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["harvest", "--xml-policy", "sometimes"])


# ============================================================ exit-code contract


def cli_args(config, *extra: str) -> list[str]:
    return [
        "--storage-root",
        str(config.storage_root),
        "--state-db",
        str(config.state_db),
        "--reports-dir",
        str(config.reports_dir),
        "--contact-email",
        "operator@example.org",
        "--openalex-api-key",
        "cli-test-key",
        "--log-level",
        "ERROR",
        *extra,
    ]


@pytest.fixture
def patched_clients(monkeypatch):
    """Route the CLI's ClientPool at a chosen mock provider set."""
    holder: dict[str, Any] = {}
    import harvester.cli as cli_module
    from harvester.http import ClientPool

    def factory(config, **kwargs):
        return ClientPool(
            config, transport=holder["providers"].transport, sleeper=lambda _s: None
        )

    monkeypatch.setattr(cli_module, "ClientPool", factory)

    def install(providers: MockProviders, config) -> None:
        holder["providers"] = providers
        config.openalex.base_url = "https://api.openalex.invalid"
        config.europe_pmc.base_url = "https://epmc.invalid/rest"
        config.unpaywall.base_url = "https://api.unpaywall.invalid/v2"

    return install


def base_env(config, monkeypatch, providers) -> None:
    monkeypatch.setenv("HARVESTER_OPENALEX_BASE_URL", "https://api.openalex.invalid")
    monkeypatch.setenv("HARVESTER_EUROPE_PMC_BASE_URL", "https://epmc.invalid/rest")
    monkeypatch.setenv("HARVESTER_UNPAYWALL_BASE_URL", "https://api.unpaywall.invalid/v2")
    monkeypatch.setenv("HARVESTER_RETRY_BACKOFF_INITIAL", "0.001")
    monkeypatch.setenv("HARVESTER_RETRY_BACKOFF_MAX", "0.002")


def test_exit_zero_on_a_fully_successful_harvest(config, patched_clients, monkeypatch, capsys):
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)

    code = main(cli_args(config, "harvest", "--topic-id", "T10159"))
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "status" in out and "COMPLETED" in out
    assert "report:" in out


def test_exit_three_when_documents_failed(config, patched_clients, monkeypatch, capsys):
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_html_bytes()}
    )
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)

    code = main(cli_args(config, "harvest", "--topic-id", "T10159"))
    assert code == EXIT_PARTIAL_FAILURE


def test_exit_four_when_the_provider_budget_is_exhausted(
    config, patched_clients, monkeypatch, capsys
):
    providers = MockProviders(works=[openalex_work(1)])
    providers.script_route(
        "openalex.works",
        [
            Behavior(
                status=429,
                json={"error": "daily credit allowance exhausted"},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "900"},
            )
        ],
    )
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)

    code = main(cli_args(config, "harvest", "--topic-id", "T10159"))
    assert code == EXIT_SUSPENDED
    out = capsys.readouterr().out
    assert "SUSPENDED" in out
    assert "resume" in out


def test_exit_two_when_the_openalex_key_is_missing(config, monkeypatch, capsys):
    monkeypatch.delenv("HARVESTER_OPENALEX_API_KEY", raising=False)
    code = main(
        [
            "--storage-root",
            str(config.storage_root),
            "--state-db",
            str(config.state_db),
            "--contact-email",
            "operator@example.org",
            "harvest",
            "--topic-id",
            "T10159",
        ]
    )
    assert code == EXIT_CONFIG_ERROR
    assert "API key" in capsys.readouterr().err


def test_exit_two_on_a_deprecated_concept_id(config, monkeypatch, capsys):
    base_env(config, monkeypatch, None)
    code = main(cli_args(config, "harvest", "--topic-id", "C169760540"))
    assert code == EXIT_CONFIG_ERROR
    assert "Topic ID" in capsys.readouterr().err


def test_exit_two_when_resuming_an_unknown_run(config, monkeypatch, capsys):
    base_env(config, monkeypatch, None)
    code = main(cli_args(config, "resume", "run-nope"))
    assert code == EXIT_CONFIG_ERROR


# ==================================================== discover / status / verify


def test_discover_is_a_dry_run(config, patched_clients, monkeypatch, capsys):
    providers = MockProviders(
        works=[openalex_work(i) for i in range(1, 4)],
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, 4)},
    )
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)

    code = main(cli_args(config, "discover", "--topic-id", "T10159"))
    assert code == EXIT_OK
    assert "discovered" in capsys.readouterr().out
    assert not list(Path(config.storage_root).glob("*.pdf"))


def test_status_reports_runs_and_corpus(config, patched_clients, monkeypatch, capsys):
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)
    main(cli_args(config, "harvest", "--topic-id", "T10159"))
    capsys.readouterr()

    assert main(cli_args(config, "status", "--json")) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["document_status_counts"]["COMPLETED"] == 1
    assert payload["artifacts"]["pdf"] == 1
    assert payload["runs"][0]["status"] == "COMPLETED"


def test_status_for_one_run_shows_resumability(config, patched_clients, monkeypatch, capsys):
    providers = MockProviders(works=[openalex_work(1)])
    providers.script_route(
        "openalex.works",
        [
            Behavior(
                status=429,
                json={"error": "daily budget exhausted"},
                headers={"X-RateLimit-Remaining": "0"},
            )
        ],
    )
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)
    main(cli_args(config, "harvest", "--topic-id", "T10159"))
    capsys.readouterr()

    main(cli_args(config, "status", "--json"))
    run_id = json.loads(capsys.readouterr().out)["runs"][0]["run_id"]

    assert main(cli_args(config, "status", "--run-id", run_id, "--json")) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "SUSPENDED"
    assert payload["resumable"] is True
    assert payload["suspend_reason"]


def test_inspect_shows_the_canonical_record(config, patched_clients, monkeypatch, capsys):
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)
    main(cli_args(config, "harvest", "--topic-id", "T10159"))
    capsys.readouterr()

    # Any DOI representation resolves to the same document.
    assert main(cli_args(config, "inspect", "https://doi.org/10.1234/MOCK.0001", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["document_id"] == document_id_for_doi("10.1234/mock.0001")
    assert payload["status"] == "COMPLETED"
    assert payload["sidecar_present"] is True
    assert payload["artifacts"]["pdf"]["sha256"]


def test_inspect_of_an_unknown_document_fails_cleanly(config, monkeypatch, capsys):
    base_env(config, monkeypatch, None)
    assert main(cli_args(config, "inspect", "10.9999/absent")) == EXIT_FAILURE
    assert "no such document" in capsys.readouterr().err


def test_verify_passes_on_a_healthy_corpus(config, patched_clients, monkeypatch, capsys):
    providers = MockProviders(
        works=[openalex_work(i) for i in range(1, 4)],
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, 4)},
    )
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)
    main(cli_args(config, "harvest", "--topic-id", "T10159"))
    capsys.readouterr()

    assert main(cli_args(config, "verify", "--deep", "--json")) == EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["completed_documents"] == 3
    assert report["sidecars_present"] == 3
    assert report["problems"] == []


def test_verify_detects_a_deleted_artifact(config, patched_clients, monkeypatch, capsys):
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)
    main(cli_args(config, "harvest", "--topic-id", "T10159"))
    capsys.readouterr()

    document_id = document_id_for_doi("10.1234/mock.0001")
    (Path(config.storage_root) / f"{document_id}.pdf").unlink()

    assert main(cli_args(config, "verify", "--json")) == EXIT_PARTIAL_FAILURE
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert any("missing" in problem["problem"] for problem in report["problems"])


def test_verify_detects_a_tampered_artifact(config, patched_clients, monkeypatch, capsys):
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)
    main(cli_args(config, "harvest", "--topic-id", "T10159"))
    capsys.readouterr()

    document_id = document_id_for_doi("10.1234/mock.0001")
    path = Path(config.storage_root) / f"{document_id}.pdf"
    path.write_bytes(make_pdf_bytes(pages=5))

    assert main(cli_args(config, "verify", "--json")) == EXIT_PARTIAL_FAILURE
    report = json.loads(capsys.readouterr().out)
    assert any("sha256 mismatch" in problem["problem"] for problem in report["problems"])


def test_verify_reports_orphan_files(config, patched_clients, monkeypatch, capsys):
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)
    main(cli_args(config, "harvest", "--topic-id", "T10159"))
    capsys.readouterr()

    (Path(config.storage_root) / "doi_unknown_0123456789ab.pdf").write_bytes(make_pdf_bytes())
    assert main(cli_args(config, "verify", "--json")) == EXIT_PARTIAL_FAILURE
    report = json.loads(capsys.readouterr().out)
    assert "doi_unknown_0123456789ab.pdf" in report["orphans"]


def test_verify_reports_abandoned_temporary_files(config, patched_clients, monkeypatch, capsys):
    """Stray .part files are surfaced, not silently ignored — but are not corpus errors."""
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)
    main(cli_args(config, "harvest", "--topic-id", "T10159"))
    capsys.readouterr()

    (Path(config.storage_root) / "doi_abandoned.pdf.deadbeef.part").write_bytes(b"partial")
    assert main(cli_args(config, "verify", "--json")) == EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True  # informational, not a corpus problem
    assert report["temporary_files"] == ["doi_abandoned.pdf.deadbeef.part"]


def test_a_fresh_harvest_also_sweeps_stale_temporary_files(
    config, patched_clients, monkeypatch, capsys, tmp_path
):
    """Cleanup must not require an explicit resume."""
    import os
    import time

    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)
    main(cli_args(config, "harvest", "--topic-id", "T10159"))
    capsys.readouterr()

    stale = Path(config.storage_root) / "doi_stale.pdf.abcd1234.part"
    stale.write_bytes(b"partial")
    old = time.time() - 3600
    os.utime(stale, (old, old))

    main(cli_args(config, "harvest", "--topic-id", "T10159"))
    capsys.readouterr()
    assert not stale.exists()


def test_retry_failed_requeues_documents(config, patched_clients, monkeypatch, capsys):
    providers = MockProviders(works=[openalex_work(1)], files={})  # 404 on the PDF
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)
    main(cli_args(config, "harvest", "--topic-id", "T10159"))
    capsys.readouterr()

    assert main(cli_args(config, "retry-failed")) == EXIT_OK
    assert "re-queued 1" in capsys.readouterr().out


def test_evidence_export_restore_cli_round_trip(
    config, patched_clients, monkeypatch, capsys, tmp_path
):
    providers = MockProviders(works=[openalex_work(1)])
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)
    assert main(cli_args(config, "discover", "--topic-id", "T10159")) == EXIT_OK
    capsys.readouterr()

    first_export = tmp_path / "evidence.json"
    assert (
        main(
            cli_args(
                config, "evidence-export", "--output", str(first_export)
            )
        )
        == EXIT_OK
    )
    source_manifest = json.loads(capsys.readouterr().out)

    restored_db = tmp_path / "restored.sqlite3"
    restore_args = cli_args(
        config, "evidence-restore", "--input", str(first_export)
    )
    restore_args[restore_args.index("--state-db") + 1] = str(restored_db)
    assert main(restore_args) == EXIT_OK
    restored_manifest = json.loads(capsys.readouterr().out)

    second_export = tmp_path / "restored-evidence.json"
    reexport_args = cli_args(
        config, "evidence-export", "--output", str(second_export)
    )
    reexport_args[reexport_args.index("--state-db") + 1] = str(restored_db)
    assert main(reexport_args) == EXIT_OK
    reexported_manifest = json.loads(capsys.readouterr().out)

    assert restored_manifest["content_sha256"] == source_manifest["content_sha256"]
    assert reexported_manifest["content_sha256"] == source_manifest["content_sha256"]


def test_full_cli_lifecycle_harvest_suspend_resume_verify(
    config, patched_clients, monkeypatch, capsys
):
    """The documented operational loop, driven entirely through the CLI."""
    providers = MockProviders(
        works=[openalex_work(i) for i in range(1, 5)],
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, 5)},
        page_size=2,
    )
    providers.script_route(
        "openalex.works",
        [
            Behavior(),
            Behavior(
                status=429,
                json={"error": "daily credit allowance exhausted"},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "60"},
            ),
        ],
    )
    patched_clients(providers, config)
    base_env(config, monkeypatch, providers)

    assert main(cli_args(config, "harvest", "--topic-id", "T10159")) == EXIT_SUSPENDED
    capsys.readouterr()

    healthy = MockProviders(
        works=[openalex_work(i) for i in range(1, 5)],
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, 5)},
        page_size=2,
    )
    patched_clients(healthy, config)
    assert main(cli_args(config, "resume")) == EXIT_OK
    capsys.readouterr()

    assert main(cli_args(config, "verify", "--deep", "--json")) == EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["completed_documents"] == 4
    assert len(list(Path(config.storage_root).glob("*.pdf"))) == 4
    assert len(list(Path(config.storage_root).glob("*.json"))) == 4
