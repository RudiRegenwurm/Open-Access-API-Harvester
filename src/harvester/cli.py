# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""Command-line interface (MASTER_SPEC sections 33, 34, 35, 36).

Commands::

    harvester harvest        run discovery + acquisition
    harvester discover       discovery only (dry run)
    harvester resume         continue an interrupted or suspended run
    harvester status         run and corpus status
    harvester inspect        show one document's canonical record
    harvester verify         verify the local corpus against state
    harvester retry-failed   re-queue failed documents for a later run
    harvester evidence-export  write a portable append-only ledger bundle
    harvester evidence-restore restore a bundle into an empty ledger

Exit codes: 0 success, 1 failure, 2 configuration error, 3 completed with document
failures, 4 suspended (provider budget), 130 interrupted.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import Config
from .evidence import read_evidence_export, restore_evidence_export, write_evidence_export
from .errors import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    EXIT_SUSPENDED,
    ConfigurationError,
    HarvesterError,
)
from .http import ClientPool
from .identity import document_id_for_doi, normalize_doi
from .logging_setup import configure_logging
from .models import DocumentStatus, RunStatus
from .orchestrator import Harvester
from .providers.openalex import OpenAlexQuery
from .reporting import render_summary
from .state import StateStore
from .storage import read_sidecar
from .util import to_pretty_json
from .verify import render_verification, verify_corpus

LOGGER = logging.getLogger("harvester.cli")


# ------------------------------------------------------------------ argument parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harvester",
        description=(
            "Notanda: discover Open-Access scholarly works via "
            "OpenAlex Topics, cross-check them against Europe PMC, fall back to "
            "Unpaywall, and store validated PDF/XML artifacts with mandatory JSON "
            "sidecars in a flat local corpus."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"harvester {__version__}")
    _add_global_options(parser)

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    harvest = subparsers.add_parser(
        "harvest",
        help="discover and acquire Open-Access documents",
        description="Run a complete harvest: discovery, deduplication, acquisition, "
        "validation and ingestion.",
    )
    _add_global_options(harvest, subcommand=True)
    _add_query_options(harvest)
    harvest.add_argument(
        "--limit", type=int, default=None, help="stop discovery after N records (safe testing)"
    )
    harvest.add_argument(
        "--dry-run",
        action="store_true",
        help="discover and normalize only; write no artifacts",
    )
    harvest.add_argument("--run-id", default=None, help="use an explicit run identifier")
    harvest.set_defaults(func=cmd_harvest)

    discover = subparsers.add_parser(
        "discover",
        help="discovery only (equivalent to 'harvest --dry-run')",
        description="Perform discovery and normalization without downloading anything.",
    )
    _add_global_options(discover, subcommand=True)
    _add_query_options(discover)
    discover.add_argument("--limit", type=int, default=None, help="stop after N records")
    discover.add_argument("--run-id", default=None, help="use an explicit run identifier")
    discover.set_defaults(func=cmd_discover)

    resume = subparsers.add_parser(
        "resume",
        help="continue an interrupted or suspended run",
        description="Resume a run without repeating successfully completed documents.",
    )
    _add_global_options(resume, subcommand=True)
    resume.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help="run to resume; defaults to the most recent resumable run",
    )
    resume.set_defaults(func=cmd_resume)

    status = subparsers.add_parser(
        "status", help="show run and corpus status", description="Report run and corpus state."
    )
    _add_global_options(status, subcommand=True)
    status.add_argument("--run-id", default=None, help="report on one run")
    status.add_argument("--limit", type=int, default=10, help="how many runs to list")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=cmd_status)

    inspect = subparsers.add_parser(
        "inspect",
        help="show the canonical record of one document",
        description="Print the stored metadata, artifacts and provenance for a document.",
    )
    _add_global_options(inspect, subcommand=True)
    inspect.add_argument("identifier", help="a DOI (any representation) or a document_id")
    inspect.add_argument("--json", action="store_true", help="machine-readable output")
    inspect.set_defaults(func=cmd_inspect)

    verify = subparsers.add_parser(
        "verify",
        help="verify the local corpus against persistent state",
        description="Check sidecar presence, artifact presence, SHA-256 and "
        "state/filesystem consistency.",
    )
    _add_global_options(verify, subcommand=True)
    verify.add_argument(
        "--deep",
        action="store_true",
        help="also re-run PDF/XML structural validation on every artifact",
    )
    verify.add_argument("--json", action="store_true", help="machine-readable output")
    verify.set_defaults(func=cmd_verify)

    serve = subparsers.add_parser(
        "serve",
        help="open the local web control center",
        description="Start the local web UI and open it in a browser. The UI drives the "
        "same engine and the same state database as these commands.",
    )
    _add_global_options(serve, subcommand=True)
    serve.add_argument("--host", default="127.0.0.1", help="interface to bind (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8765, help="port to listen on (default: 8765)")
    serve.add_argument(
        "--no-browser", action="store_true", help="do not open a browser automatically"
    )
    serve.set_defaults(func=cmd_serve)

    retry = subparsers.add_parser(
        "retry-failed",
        help="re-queue failed documents",
        description="Move FAILED_* / SKIPPED documents back to QUEUED so a later "
        "harvest or resume retries them.",
    )
    _add_global_options(retry, subcommand=True)
    retry.add_argument("--run-id", default=None, help="restrict to one run")
    retry.set_defaults(func=cmd_retry_failed)

    evidence_export = subparsers.add_parser(
        "evidence-export",
        help="export the append-only Evidence Ledger as portable JSON",
        description="Write the Evidence Ledger tables, counts and content digest as "
        "independently readable JSON. Mutable V1 projections and corpus bytes are excluded.",
    )
    _add_global_options(evidence_export, subcommand=True)
    evidence_export.add_argument(
        "--output", type=Path, required=True, help="destination JSON file"
    )
    evidence_export.set_defaults(func=cmd_evidence_export)

    evidence_restore = subparsers.add_parser(
        "evidence-restore",
        help="restore a portable JSON bundle into an empty Evidence Ledger",
        description="Validate and atomically restore an evidence export. The target "
        "ledger must be empty; legacy V1 projections are not fabricated.",
    )
    _add_global_options(evidence_restore, subcommand=True)
    evidence_restore.add_argument(
        "--input", type=Path, required=True, help="source JSON export"
    )
    evidence_restore.set_defaults(func=cmd_evidence_restore)

    return parser


def _add_global_options(parser: argparse.ArgumentParser, *, subcommand: bool = False) -> None:
    """Attach the configuration options.

    The same options are accepted before *and* after the subcommand, so both
    ``harvester --storage-root X harvest`` and ``harvester harvest --storage-root X``
    work. On the subparser the defaults are ``SUPPRESS``: without it argparse would
    write the subparser's own ``None`` defaults over values already parsed from the
    top-level parser, silently discarding them.
    """
    missing = argparse.SUPPRESS if subcommand else None
    flag_missing = argparse.SUPPRESS if subcommand else False

    group = parser.add_argument_group("configuration")
    group.add_argument(
        "--config", type=Path, default=missing, help="path to a JSON config file"
    )
    group.add_argument(
        "--storage-root", default=missing, help="final corpus directory (flat layout)"
    )
    group.add_argument("--state-db", default=missing, help="SQLite state database path")
    group.add_argument("--reports-dir", default=missing, help="directory for run reports")
    group.add_argument(
        "--contact-email",
        default=missing,
        help="contact address; required by Unpaywall and used in the User-Agent",
    )
    group.add_argument(
        "--openalex-api-key",
        default=missing,
        help="OpenAlex API key (prefer the HARVESTER_OPENALEX_API_KEY environment variable)",
    )
    group.add_argument(
        "--allow-keyless-openalex",
        action="store_true",
        default=missing,
        help="use OpenAlex without a key (tiny testing allowance only, not for production)",
    )
    group.add_argument(
        "--daily-credit-ceiling",
        type=int,
        default=missing,
        help="local OpenAlex daily-credit safety ceiling; reaching it suspends the run cleanly",
    )
    group.add_argument(
        "--xml-policy",
        choices=("preferred", "required", "disabled"),
        default=missing,
        help="XML acquisition policy (default: preferred)",
    )
    group.add_argument("--concurrency", type=int, default=missing, help="download concurrency")
    group.add_argument(
        "--max-attempts", type=int, default=missing, help="retry attempts per request"
    )
    group.add_argument(
        "--no-europe-pmc", action="store_true", default=flag_missing, help="disable Europe PMC"
    )
    group.add_argument(
        "--no-unpaywall", action="store_true", default=flag_missing, help="disable Unpaywall"
    )
    group.add_argument(
        "--log-level",
        default=missing,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="logging verbosity (default: INFO)",
    )
    group.add_argument(
        "--log-format", default=missing, choices=("text", "json"), help="log output format"
    )
    group.add_argument("--log-file", default=missing, help="also write logs to this file")


def _add_query_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("discovery query")
    group.add_argument(
        "--topic-id",
        default=None,
        help="OpenAlex Topic ID, e.g. T10159 (Topics are canonical; Concepts are deprecated)",
    )
    group.add_argument(
        "--primary-topic-only",
        action="store_true",
        help="match primary_topic.id instead of topics.id",
    )
    group.add_argument("--search", default=None, help="free-text search term")
    group.add_argument("--year", type=int, default=None, help="exact publication year")
    group.add_argument("--from-year", type=int, default=None, help="earliest publication year")
    group.add_argument("--to-year", type=int, default=None, help="latest publication year")
    group.add_argument("--oa-status", default=None, help="filter on open_access.oa_status")
    group.add_argument(
        "--no-oa-filter", action="store_true", help="do not restrict to Open Access"
    )
    group.add_argument("--no-doi-filter", action="store_true", help="do not require a DOI")
    group.add_argument(
        "--language",
        dest="languages",
        action="append",
        default=None,
        metavar="ISO639-1",
        help=(
            "publication language, e.g. en (repeatable; several are OR-ed). "
            "Omit for any language."
        ),
    )
    group.add_argument(
        "--affiliation-country",
        dest="affiliation_countries",
        action="append",
        default=None,
        metavar="ISO3166-1",
        help=(
            "country of an author's institutional affiliation, e.g. DE (repeatable; "
            "several are OR-ed). Not the country a study was conducted in. "
            "Omit for any country."
        ),
    )
    group.add_argument(
        "--filter",
        dest="extra_filters",
        action="append",
        default=None,
        metavar="KEY:VALUE",
        help="additional raw OpenAlex filter (repeatable)",
    )


# ---------------------------------------------------------------------- utilities


#: dotted configuration path -> argparse destination
_ARG_TO_CONFIG = {
    "storage_root": "storage_root",
    "state_db": "state_db",
    "reports_dir": "reports_dir",
    "contact_email": "contact_email",
    "xml_policy": "xml_policy",
    "log_level": "log_level",
    "log_format": "log_format",
    "log_file": "log_file",
    "openalex.api_key": "openalex_api_key",
    "openalex.allow_keyless": "allow_keyless_openalex",
    "openalex.daily_credit_ceiling": "daily_credit_ceiling",
    "downloads.concurrency": "concurrency",
    "retry.max_attempts": "max_attempts",
}


def _overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for dotted, attribute in _ARG_TO_CONFIG.items():
        # Subparser copies use argparse.SUPPRESS, so an unsupplied option is simply
        # absent from the namespace rather than present as None.
        value = getattr(args, attribute, None)
        if value is not None:
            overrides[dotted] = value
    if getattr(args, "no_europe_pmc", False):
        overrides["europe_pmc.enabled"] = False
    if getattr(args, "no_unpaywall", False):
        overrides["unpaywall.enabled"] = False
    return overrides


def _load_config(args: argparse.Namespace) -> Config:
    config = Config.load(
        config_path=getattr(args, "config", None), overrides=_overrides_from_args(args)
    )
    configure_logging(
        config.log_level, log_format=config.log_format, log_file=config.log_file
    )
    return config


def _query_from_args(args: argparse.Namespace) -> OpenAlexQuery:
    return OpenAlexQuery(
        topic_id=args.topic_id,
        primary_topic_only=bool(args.primary_topic_only),
        search=args.search,
        publication_year=args.year,
        from_publication_year=args.from_year,
        to_publication_year=args.to_year,
        is_oa=not args.no_oa_filter,
        has_doi=not args.no_doi_filter,
        oa_status=args.oa_status,
        languages=list(getattr(args, "languages", None) or []),
        affiliation_countries=list(getattr(args, "affiliation_countries", None) or []),
        extra_filters=list(args.extra_filters or []),
    )


def _exit_code_for(result: Any) -> int:
    if result.status is RunStatus.SUSPENDED:
        return EXIT_SUSPENDED
    if result.status is RunStatus.INTERRUPTED:
        return EXIT_INTERRUPTED
    if result.status is RunStatus.FAILED:
        return EXIT_FAILURE
    if result.stats.failed_permanent or result.stats.failed_retryable:
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


def _print(text: str, stream: Any = None) -> None:
    print(text, file=stream or sys.stdout)


# ----------------------------------------------------------------------- commands


def cmd_harvest(args: argparse.Namespace, *, dry_run: bool | None = None) -> int:
    config = _load_config(args)
    dry = bool(args.dry_run) if dry_run is None else dry_run
    config.validate(require_openalex=True)
    if config.openalex.allow_keyless and not config.openalex.api_key:
        LOGGER.warning(
            "running OpenAlex without an API key: this is a smoke/demo allowance only "
            "and is not suitable for production harvesting"
        )

    query = _query_from_args(args)
    query.filter_string()  # fail fast on an unusable query

    with StateStore(config.state_db) as store, ClientPool(config) as clients:
        harvester = Harvester(config, store, clients)
        result = harvester.harvest(
            query,
            limit=args.limit,
            dry_run=dry,
            run_id=getattr(args, "run_id", None),
            # The CLI always states the query itself; there is no advisor on this path
            # and no advisor metadata is invented for it (Assisted Search V1 §32).
            search_provenance={
                "search_mode": "conventional",
                "effective_search_query": query.search,
            },
        )
    _print(render_summary(result.stats))
    if result.report_path is not None:
        _print(f"report: {result.report_path}")
    return _exit_code_for(result)


def cmd_discover(args: argparse.Namespace) -> int:
    return cmd_harvest(args, dry_run=True)


def cmd_resume(args: argparse.Namespace) -> int:
    config = _load_config(args)
    config.validate(require_openalex=True)
    with StateStore(config.state_db) as store, ClientPool(config) as clients:
        run_id = args.run_id
        if run_id is None:
            run = store.latest_run(
                statuses=(RunStatus.SUSPENDED, RunStatus.INTERRUPTED, RunStatus.RUNNING,
                          RunStatus.FAILED)
            )
            if run is None:
                _print("no resumable run found", sys.stderr)
                return EXIT_CONFIG_ERROR
            run_id = run.run_id
            _print(f"resuming most recent resumable run: {run_id}")
        harvester = Harvester(config, store, clients)
        result = harvester.resume(run_id)
    _print(render_summary(result.stats))
    if result.report_path is not None:
        _print(f"report: {result.report_path}")
    return _exit_code_for(result)


def cmd_status(args: argparse.Namespace) -> int:
    config = _load_config(args)
    with StateStore(config.state_db) as store:
        if args.run_id:
            run = store.get_run(args.run_id)
            if run is None:
                _print(f"unknown run: {args.run_id}", sys.stderr)
                return EXIT_FAILURE
            payload: dict[str, Any] = {
                "run_id": run.run_id,
                "status": run.status.value,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "dry_run": run.dry_run,
                "record_limit": run.record_limit,
                "discovery_complete": run.discovery_complete,
                "discovery_pages": run.discovery_pages,
                "discovery_seen": run.discovery_seen,
                "resumable": run.status
                in (RunStatus.SUSPENDED, RunStatus.INTERRUPTED, RunStatus.RUNNING),
                "suspend_reason": run.suspend_reason,
                "suspend_details": run.suspend_details,
                "query": run.query,
                "document_status_counts": store.status_counts(run.run_id),
                "artifacts": store.artifact_counts(run.run_id),
            }
        else:
            payload = {
                "storage_root": str(config.storage_root),
                "state_db": str(config.state_db),
                "document_status_counts": store.status_counts(),
                "artifacts": store.artifact_counts(),
                "runs": [
                    {
                        "run_id": run.run_id,
                        "status": run.status.value,
                        "started_at": run.started_at,
                        "finished_at": run.finished_at,
                        "discovery_complete": run.discovery_complete,
                        "suspend_reason": run.suspend_reason,
                    }
                    for run in store.list_runs(args.limit)
                ],
            }

    if args.json:
        _print(to_pretty_json(payload).rstrip())
        return EXIT_OK

    if args.run_id:
        _print(f"run {payload['run_id']}")
        for key in (
            "status",
            "started_at",
            "finished_at",
            "dry_run",
            "record_limit",
            "discovery_complete",
            "discovery_pages",
            "discovery_seen",
            "resumable",
            "suspend_reason",
        ):
            _print(f"  {key:<20}: {payload[key]}")
        _print(f"  {'documents':<20}: {payload['document_status_counts']}")
        _print(f"  {'artifacts':<20}: {payload['artifacts']}")
    else:
        _print(f"storage root : {payload['storage_root']}")
        _print(f"state db     : {payload['state_db']}")
        _print(f"documents    : {payload['document_status_counts']}")
        _print(f"artifacts    : {payload['artifacts']}")
        _print("runs:")
        for run in payload["runs"]:
            suffix = f"  ({run['suspend_reason']})" if run["suspend_reason"] else ""
            _print(f"  {run['run_id']}  {run['status']:<12} {run['started_at']}{suffix}")
    return EXIT_OK


def cmd_inspect(args: argparse.Namespace) -> int:
    config = _load_config(args)
    identifier = args.identifier.strip()
    doi = normalize_doi(identifier)
    document_id = document_id_for_doi(doi) if doi else identifier

    with StateStore(config.state_db) as store:
        row = store.get_document_row(document_id)
        if row is None:
            _print(f"no such document in state: {identifier}", sys.stderr)
            return EXIT_FAILURE
        metadata = store.get_metadata(document_id)
        artifacts = {kind: record.to_dict() for kind, record in store.get_artifacts(document_id).items()}
        payload = {
            "document_id": document_id,
            "status": row["status"],
            "attempts": row["attempts"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "metadata": metadata,
            "artifacts": artifacts,
            "sidecar_present": read_sidecar(config.storage_root, document_id) is not None,
            "last_error": row["last_error_json"],
        }

    if args.json:
        _print(to_pretty_json(payload).rstrip())
        return EXIT_OK

    _print(f"document {document_id}")
    _print(f"  status        : {payload['status']}")
    _print(f"  doi           : {metadata.get('doi')}")
    _print(f"  title         : {metadata.get('title')}")
    _print(f"  year          : {metadata.get('publication_year')}")
    _print(f"  journal       : {metadata.get('journal')}")
    _print(f"  oa_status     : {metadata.get('oa_status')} (source: {metadata.get('oa_status_source')})")
    _print(f"  abstract      : {'present' if metadata.get('abstract') else 'null (not supplied)'}")
    _print(f"  domain_tags   : {', '.join(metadata.get('domain_tags') or []) or '-'}")
    _print(f"  discovered_via: {', '.join(metadata.get('discovered_via') or []) or '-'}")
    _print(f"  cross_checks  : {len(metadata.get('cross_checks') or [])}")
    _print(f"  candidates    : {len(metadata.get('candidates') or [])}")
    _print(f"  artifacts     : {', '.join(sorted(artifacts)) or '-'}")
    _print(f"  sidecar       : {'present' if payload['sidecar_present'] else 'absent'}")
    if payload["last_error"]:
        _print(f"  last error    : {payload['last_error']}")
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    config = _load_config(args)
    with StateStore(config.state_db) as store:
        report = verify_corpus(config, store, deep=args.deep)
    if args.json:
        _print(to_pretty_json(report.to_dict()).rstrip())
    else:
        _print(render_verification(report))
    return EXIT_OK if report.ok else EXIT_PARTIAL_FAILURE


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the local web control center (MASTER_SPEC section 33: an added command)."""
    from .webui import serve as serve_ui

    _load_config(args)
    # The UI is a client of the same configuration; it must not require a valid
    # provider setup merely to start, because fixing that setup is one of its jobs.
    config_path = getattr(args, "config", None) or Path("harvester.json")
    return serve_ui(
        config_path=Path(config_path),
        config_overrides=_overrides_from_args(args),
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )


def cmd_retry_failed(args: argparse.Namespace) -> int:
    config = _load_config(args)
    with StateStore(config.state_db) as store:
        if args.run_id:
            candidates = store.documents_for_run(args.run_id)
        else:
            candidates = store.all_document_ids()
        failed = [
            document_id
            for document_id in candidates
            if store.get_document_status(document_id)
            in (
                DocumentStatus.FAILED_RETRYABLE,
                DocumentStatus.FAILED_PERMANENT,
                DocumentStatus.SKIPPED,
            )
        ]
        changed = store.reset_documents_for_retry(failed)
    _print(f"re-queued {changed} document(s) for retry")
    return EXIT_OK


def cmd_evidence_export(args: argparse.Namespace) -> int:
    config = _load_config(args)
    with StateStore(config.state_db) as store:
        result = write_evidence_export(store, args.output)
    _print(to_pretty_json(result).rstrip())
    return EXIT_OK


def cmd_evidence_restore(args: argparse.Namespace) -> int:
    config = _load_config(args)
    bundle = read_evidence_export(args.input)
    with StateStore(config.state_db) as store:
        result = restore_evidence_export(store, bundle)
    _print(to_pretty_json(result).rstrip())
    return EXIT_OK


# --------------------------------------------------------------------------- main


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigurationError as exc:
        _print(f"configuration error: {exc}", sys.stderr)
        return EXIT_CONFIG_ERROR
    except HarvesterError as exc:
        _print(f"error [{exc.category.value}]: {exc}", sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        _print("interrupted", sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
