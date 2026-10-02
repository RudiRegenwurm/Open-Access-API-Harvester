# Release acceptance: notanda 1.3.2 and notanda-mcp 0.1.0b2

Rudi approved both merges and publications on 2026-10-02 after local Windows live acceptance.

## OpenAlex content fallback

Tested source: `55bc909bc69a775e5064383a56b0b916e5964251`.
Work `W2158993832` (DOI `10.1029/2001rg000106`): actual Wiley PDF request returned HTTP 403, followed by HTTP 200 for the OpenAlex cached PDF and GROBID-XML through production acquisition methods.

Each content response supplied `x-ratelimit-credits-used: 100` and `x-ratelimit-cost-usd: 0.01`. Shared usage tracking counted 200 credits per run. Artifact hashes and provenance were verified. Database, export and file scans passed, as did the application log scan with the production logging configuration. The first diagnostic harness enabled additional unfiltered HTTPX INFO messages in memory; no raw diagnostic output was persisted, and the corrected harness passed a repeat run. Two content runs cost 400 credits / USD 0.04 in total.

Fallback and security regression tests: 39 passed locally.

## MCP Beta 2

Tested source: `0d967d5dac81194d88d9a7bb72e6ae530aade030`.
Installed locally as `notanda-mcp==0.1.0b2` with the pinned published `notanda==1.3.1` core. A real stdio client searched OpenAlex for `bibliometrics` with limit 3 and received three results, HTTP 200. After terminating the search server, a new server process without an API key returned the identical result via `get_evidence` with the original receipt. Receipt SHA-256 matched the manifest; an incorrect receipt produced `receipt_mismatch`. Evidence files remained unchanged, and secret scans passed.

MCP regression tests: 23 passed locally.

Beta status remains. Thomas's independent installation and usability test is still pending. The MCP dependency remains pinned to its tested 1.3.1 core; the standalone harvester release is 1.3.2.

No credentials are included in this record.
