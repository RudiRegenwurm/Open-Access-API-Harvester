# OpenAlex content fallback — 2 October 2026

Status: implemented on main 5d562a7cc0a86760ca3da6a87058da7967382f3d;
merge is not authorized until live acceptance and Rudolf Kiechle's approval.

## Source correction

2c37c4e3be658d9df2ad6fc5626f0b82b5f1d4de is the local 16 September
`feature/notanda-ui-ux-v1` release-preparation commit for 1.3.0. It changes the
version and operations documentation. The previously cited 00a2f45 fallback
commit could not be recovered in the available checkouts and is not represented
as transferred or tested. This implementation was rebuilt against current main.

## Implementation and trade-off

Discovery requests `content_urls`. Only provider-supplied HTTPS URLs on the exact
content.openalex.org host, with the same work ID and supported format, qualify.
No URL synthesis, credentials in candidates, or model/database migration.

PDF order: existing OpenAlex/Europe PMC locations, Unpaywall, then cached OpenAlex
PDF. XML order: existing XML locations, then GROBID TEI when XML policy is enabled.
Successful ordinary retrieval avoids the corresponding paid content download.
Without an API key, normalization offers no cached content candidates.

Authentication is injected at HTTP dispatch. The existing OpenAlex client shares
rate limiting, credit accounting, and the configured ceiling with discovery.
Provider-reported costs take precedence; otherwise successful content downloads
count 100 credits and HTTP failures count zero. Ordinary PDF/XML validation,
size limits, SHA-256, atomic publication and cleanup apply. TEI root information
is preserved by the existing XML validator; no JATS conversion is claimed.

The ledger includes `request_url` for OpenAlex discovery and PDF/XML acquisition,
using the same URL and parameters as the transport, with credentials redacted.
This is the attempted request URL; the acquisition outcome also records the
resolved URL. Cross-check/Unpaywall metadata URLs are outside this small change.
Transport exception text and credential echoes in HTTP error bodies are excluded
from persisted errors. Export and raw database/file scans are regression tested.

Using the shared OpenAlex client means content uses its timeout configuration.
Rollback: revert this feature commit; no stored-data migration is required.

## Verification

16 deterministic content tests cover publisher 403 fallback, valid PDF/TEI,
skipping paid PDF after publisher success, XML disabled, missing key, identity/
host/query boundary, malformed content fields, budget suspension, HTTP 401/404,
HTML rejection, cleanup, and secret leaks through error bodies/network exceptions.
Full offline suite: **748 passed, 1 skipped, 8 live tests deselected**.
The same test scans the ledger export, database, sidecars, artifacts, reports and
captured application logs for the configured test credential.

## Live acceptance

The public metadata request for W2158993832 succeeds and provides PDF/TEI URLs.
The unauthenticated PDF and TEI calls both return HTTP 401, as recorded in
`openalex-live-preflight-2026-10-02.json`.
Authenticated PDF/TEI acquisition is still pending: no OpenAlex key is configured
in this execution session. This is not a successful live acceptance.

Once the existing key is configured, run against an isolated temporary corpus:
check the offered URLs, an actual fallback after a failed publisher acquisition,
validated PDF/TEI hashes, ledger request URLs, secret scans, corpus verification
and credit accounting. Do not merge or publish before recording the result.
