# Notanda — Open-Access API Harvester

**Notanda** is the public product name. The repository, Python distribution,
package and CLI keep their established technical names
`Open-Access-API-Harvester`, `oa-harvester`, `harvester` and `harvester`.

This repository publishes the reviewed 1.3.0 product-core source. It is not a hosted
service and the recommended accompanied beta path remains `beta@notanda.io`.

A public **Installer Preview** may be available under GitHub Releases solely as a
technical pre-release. Those installers are unsigned on Windows/Linux and ad-hoc signed
on macOS, may trigger SmartScreen/Gatekeeper warnings, and are **not the recommended
installation path for non-technical users**.

A headless, resumable, idempotent CLI pipeline that discovers Open-Access scholarly
works through **OpenAlex Topics**, cross-checks them against **Europe PMC**, falls back
to **Unpaywall** for OA locations, validates every downloaded artifact, and stores the
result as a deterministic flat corpus ready for a downstream pre-ingestion pipeline.
Schema 4 additionally retains provider observations, merge decisions and acquisitions
in an append-only Evidence Ledger while the established read model remains compatible.

```text
DISCOVERY → NORMALIZATION → DEDUPLICATION → ACQUISITION → VALIDATION → INGESTION → PROVENANCE/REPORTING
```

It is not a web application, document-management system, search engine, OCR system or
LLM application. It is a document harvesting pipeline with persistent state.

---

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

### With the local control center (no command line needed after this)

```bash
harvester serve
```

Opens a local web UI at <http://localhost:8765/> for harvesting, browsing the corpus,
inspecting runs and provenance, verifying integrity, retrying failures and editing
settings. Credentials can be entered in its Settings page on first run.

**New Harvest** offers two search modes. *Conventional Search* uses the query you type,
exactly as typed. *Assisted Search* turns a plain-language research question into one
compact retrieval query you can edit, previews up to ten real discovery results without
downloading anything, and only then hands the approved query to the same harvest
pipeline. Assisted Search needs a query-advisor credential; Conventional Search does not
and is unaffected when the advisor is missing or unavailable.

### From the command line

```bash
export HARVESTER_OPENALEX_API_KEY='…'          # free: openalex.org/settings/api
export HARVESTER_CONTACT_EMAIL='ops@example.org'  # required by Unpaywall
export HARVESTER_ADVISOR_API_KEY='…'           # optional: Assisted Search in the UI only

harvester discover --topic-id T10159 --limit 25    # dry run, downloads nothing
harvester harvest  --topic-id T10159 --limit 25    # real harvest
harvester verify --deep                            # check the corpus against state
```

Both drive the same engine and the same state database: a run started in the browser
can be resumed from the terminal.

## Output

The downstream contract is a **flat** directory of deterministic sibling files:

```text
<storage_root>/
├── doi_10_1371_journal_pone_0123456_5d41402abc4b.pdf     primary full text
├── doi_10_1371_journal_pone_0123456_5d41402abc4b.xml     when legitimately available
└── doi_10_1371_journal_pone_0123456_5d41402abc4b.json    mandatory sidecar
```

The sidecar keeps bibliographic metadata, artifact metadata, provenance and harvest
state in separate blocks. Missing information is `null`; nothing is ever invented.

```json
{
  "document_id": "doi_10_1371_journal_pone_0123456_5d41402abc4b",
  "doi": "10.1371/journal.pone.0123456",
  "title": "…",
  "abstract": "…",
  "oa_status": "gold",
  "oa_status_source": "openalex",
  "domain_tags": ["Social Sciences", "Psychology", "…"],
  "artifacts": { "pdf": { "sha256": "…", "size_bytes": 123456, "…": "…" } },
  "provenance": {
    "discovered_via": ["openalex"],
    "cross_checked_via": ["europe_pmc"],
    "acquired_via": "unpaywall",
    "resolved_url": "…",
    "http_status": 200
  },
  "harvest": { "run_id": "…", "status": "COMPLETED", "attempts": 1 }
}
```

## What it guarantees

| Guarantee | How |
| --- | --- |
| **Nothing false succeeds** | HTTP 200 is never enough: magic bytes, trailer, parser openability, size bounds and SHA-256 must all pass before an artifact exists under its final name. |
| **Atomic artifacts** | Every download goes to a unique `.part` file and is published with a single atomic rename after validation. |
| **Resumable** | Discovery cursors and document state are checkpointed continuously; `harvester resume` continues without repeating completed work. |
| **Idempotent** | Re-running the same harvest downloads nothing and produces a byte-identical corpus. |
| **Deduplicated** | One logical document per canonical DOI, however many providers report it. |
| **No silent loss** | Every failure is a structured row in the state database and appears in the run report. |
| **Historical evidence** | Re-observations, field-level merge decisions and acquisitions append immutable rows; current `documents`, `source_records` and `artifacts` remain compatible projections. |
| **Budget-safe** | Provider daily-budget exhaustion checkpoints and suspends cleanly (exit 4) instead of hammering the API. |
| **Secret-safe** | API keys and contact addresses are redacted from logs, provenance, reports and state. |
| **Lawful** | Only OA locations that a provider explicitly flags as open are fetched. Access controls are recorded, never circumvented. |

## Commands

| Command | Purpose |
| --- | --- |
| `serve` | open the local web control center |
| `harvest` | discover + acquire |
| `discover` | discovery and normalization only (dry run) |
| `resume` | continue an interrupted or suspended run |
| `status` | run and corpus status (`--json`) |
| `inspect` | one document's canonical record (`--json`) |
| `verify` | check the corpus against state (`--deep`, `--json`) |
| `retry-failed` | re-queue failed documents |
| `evidence-export` | write a deterministic, independently readable ledger JSON bundle (`--output`) |
| `evidence-restore` | validate and restore a bundle into an empty ledger (`--input`) |

Exit codes: `0` success · `1` failure · `2` configuration error · `3` completed with
document failures · `4` suspended (provider budget) · `130` interrupted.

## Tests

```bash
pytest            # offline suite — no network, no credentials required
pytest -m live    # controlled real-provider smoke tests
```

The offline suite runs against deterministic in-process mock providers covering normal
responses, pagination, throttling, budget exhaustion, timeouts, malformed payloads,
missing full text, alternative locations and duplicates.

## Documentation

| File | Contents |
| --- | --- |
| [`LICENSE`](LICENSE) | MIT license for the product code and documentation, subject to the stated exceptions |
| [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) | bundled font licenses and separately licensed dependencies |
| [`TRADEMARKS.md`](TRADEMARKS.md) | treatment of the Notanda name and visual identity assets |
| [`CODE_SIGNING_POLICY.md`](CODE_SIGNING_POLICY.md) | Windows code signing policy for the SignPath Foundation application |
| [`INSTALL-MACOS.md`](INSTALL-MACOS.md) | macOS Installer Preview and Gatekeeper exception procedure |

## Code signing policy

**Free code signing provided by SignPath.io, certificate by SignPath Foundation.**

Windows release signing is intended to use SignPath Foundation's free Open Source path.
Every production signing request requires explicit manual approval by the maintainer.
The complete policy is documented in [`CODE_SIGNING_POLICY.md`](CODE_SIGNING_POLICY.md).
No current unsigned Installer Preview should be interpreted as already signed or approved
by SignPath Foundation.

## Dependencies

`httpx` (streaming HTTP with a pluggable transport), `pypdf` (PDF structural
validation), `defusedxml` (safe XML parsing). Everything else is the standard library:
configuration, SQLite persistence, CLI, logging, hashing, concurrency, retry — and the
web UI, which uses `http.server` plus a front end with no build step, so `harvester
serve` is the only command needed to run it.

## License

Unless a file or notice says otherwise, the product source code and documentation
in this repository are licensed under the [MIT License](LICENSE), copyright 2026
Rudolf Kiechle.

The bundled IBM Plex and Source Serif 4 font files are not MIT-licensed; they remain
under the SIL Open Font License 1.1 included beside the files. External Python
dependencies are not vendored and retain their own licenses. The Notanda name,
emblem, wordmark, lockup, favicons and avatar are excluded from the MIT grant.
Details and exact file scopes are in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and
[`TRADEMARKS.md`](TRADEMARKS.md).
