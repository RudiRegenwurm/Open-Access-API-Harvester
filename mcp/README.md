# Notanda local MCP server — Beta

<!-- mcp-name: io.github.RudiRegenwurm/notanda -->

**Every OpenAlex search stores its request, response and checksums so sources can
be checked later.**

**Beta: an external developer's unassisted acceptance test is still pending.**
Technical tests do not replace that external usability and installation check.

This directory exposes Notanda's provenance-aware OpenAlex retrieval to local MCP
clients. It is a separate `notanda-mcp` package, not a hosted service and not
part of the `notanda` 1.3.1 package. It depends on that released core.

The component provides exactly two tools:

- `search_literature`: search OpenAlex for OA works with a DOI and persist the
  request, provider observation, returned result and hashes under one evidence ID;
  the returned `receipt` is the SHA-256 of `manifest.json`.
- `get_evidence`: verify those hashes and return the original result by evidence ID
  without repeating the provider request. An optional caller-held `receipt` also
  detects replacement of the payloads together with their manifest.

Both tool descriptions and every successful response warn that titles and metadata
are unverified third-party content, not instructions.

It does not download full text, write to the Notanda corpus or Evidence Ledger,
modify the web UI, expose a network server, or provide general agent reasoning.

## Provenance and status

The implementation is adapted from the isolated branch `experiment/local-mcp`,
commit `366bc2d4827aa915e2e58a82cd567163ed63d4be`. The sealed handoff package bound
that source to 20 passing MCP tests and a successful live OpenAlex run. For the
public repository integration, the duplicated Notanda runtime was replaced by the
released `notanda==1.3.1` PyPI dependency; MCP remains pinned to the tested SDK
version `1.30.0`. [`SOURCE_PROVENANCE.json`](SOURCE_PROVENANCE.json) records the
source hashes and the deliberate integration changes in machine-readable form.

Five of the six original acceptance criteria are technically satisfied. The sixth —
an external developer with no Notanda knowledge completing installation, search,
restart and independent verification without assistance — remains **externally to be
verified** until that result is returned.

## Install the Beta package

Use Python 3.11 or 3.12 in a virtual environment:

```bash
python -m pip install "notanda-mcp==0.1.0b2"
```

For a client that uses uvx, configure this command and arguments (replace the
evidence directory with a writable absolute path on your computer):

```text
uvx --python 3.12 notanda-mcp@0.1.0b2 --evidence-dir ABSOLUTE_LOCAL_EVIDENCE_PATH
```

Set `HARVESTER_OPENALEX_API_KEY` in the MCP client's local environment. The key is
required for searches and must not be placed in the command arguments. The registry
manifest declares this variable as secret and the evidence path as a required input.

## Install from this repository

Use Python 3.11 or 3.12. From the repository root:

```bash
python -m venv .venv-mcp
# Linux/macOS:
. .venv-mcp/bin/activate
# Windows PowerShell:
# .venv-mcp\Scripts\Activate.ps1
python -m pip install -e ./mcp
```

This installs the MCP component from the checkout and resolves the reviewed Notanda
core as `notanda==1.3.1` from PyPI.

Configure an OpenAlex API key locally. For example, in PowerShell without placing the
key in terminal history:

```powershell
$mcpSecureKey = Read-Host 'OpenAlex API key' -AsSecureString
$env:HARVESTER_OPENALEX_API_KEY = [System.Net.NetworkCredential]::new('', $mcpSecureKey).Password
```

The existing Notanda configuration mechanisms also work: `HARVESTER_CONFIG` or
`HARVESTER_OPENALEX_API_KEY`. Never add credentials to evidence directories, Git,
screenshots or returned test reports.

## Run and verify

The supplied real stdio client starts the server, initializes MCP, verifies the two
tool names and executes a search:

```bash
python mcp/tools/mcp_client.py \
  --evidence-dir ./tmp/mcp-evidence \
  --query "scientific reproducibility" \
  --limit 2
```

The response contains an `evidence_id` and a `receipt`. Keep both outside the evidence
directory. A second process can verify that receipt and retrieve the stored response
without another provider request:

```bash
python mcp/tools/mcp_client.py \
  --evidence-dir ./tmp/mcp-evidence \
  --id EVIDENCE_ID \
  --receipt RECEIPT
```

For compatibility, `get_evidence` also works without `receipt` and performs the
previous payload hash and identity checks. Those checks detect corruption, but a
coordinated replacement of payloads and manifest is detectable only when the original
receipt is supplied.

The independent verifier uses only the Python standard library:

```bash
python -S mcp/tools/verify_mcp_evidence.py \
  ./tmp/mcp-evidence/EVIDENCE_ID
```

For another local MCP client, configure the virtual environment's `notanda-mcp`
executable with these arguments:

```text
--evidence-dir ABSOLUTE_LOCAL_EVIDENCE_PATH
```

Transport is stdio. The server has no user interface and opens no listening port.

## Evidence contract

Each search creates one directory named by its 32-character evidence ID:

| File | Contents |
| --- | --- |
| `request.json` | supplied and effective query, limit, MCP client name/version, source experiment commit, installed Notanda version and server hash |
| `provider.json` | attempted OpenAlex URL after secret redaction, timestamps, HTTP status and captured parsed JSON |
| `response.json` | the persisted result, including structured failures and the untrusted-content notice; the detached receipt is added to the MCP response after sealing |
| `manifest.json` | byte length and SHA-256 for the three payload files |

The manifest alone detects corruption or one-file modification. It is not a digital
signature and, without a caller-held receipt, does not detect coordinated replacement
of payloads and manifest. Comparing the optional receipt anchors the exact manifest
observed during the original search.

## Limits and failure behavior

- Query length: 1–1000 characters. Result limit: integer 1–10.
- One OpenAlex request, no pagination, no automatic retry.
- 20-second operation timeout, 10-second connection timeout, 4 MiB response ceiling.
- Only `https://api.openalex.org/works` is accepted as the provider endpoint.
- Missing key: `configuration_error`; invalid input: `invalid_arguments`.
- Provider/transport failure: `provider_error`; no provider exception text is stored.
- Interrupted write: `incomplete`; hash or identity mismatch: `integrity_failure`;
  supplied receipt mismatch: `receipt_mismatch`.
- Provider text is persisted as untrusted data, never interpreted as instructions.

## Tests

Install the development extra and run the 23 deterministic checks:

```bash
python -m pip install -e "./mcp[dev]"
python -m pytest mcp/tests
```

The tests cover successful retrieval, restart, independent verification, corruption,
invalid input, HTTP 401/429/500, timeouts, malformed and oversized responses, storage
failure, process death, secret redaction and real two-process stdio MCP operation.
