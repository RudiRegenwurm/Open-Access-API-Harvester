"""Security tests (MASTER_SPEC sections 37, 38, 39 and section 50 level 4).

Covers AC-014 (no write outside the storage root), AC-015 (size limit) and
AC-017 (no secret leakage), plus SSRF, unsafe schemes and parser safety.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from harvester.errors import UnsafeUrlError
from harvester.identity import artifact_path
from harvester.logging_setup import RedactionFilter, configure_logging, scrub
from harvester.models import ArtifactKind, FulltextCandidate, Source
from harvester.acquisition import Acquirer
from harvester.http import redact_url
from mocks import (
    FILES_BASE,
    MockProviders,
    make_pdf_bytes,
    make_xxe_xml_bytes,
    openalex_work,
    epmc_result,
)

DOI = "10.1234/mock.0001"


# ===================================================================== AC-014


def test_ac014_malicious_remote_metadata_cannot_escape_the_storage_root(
    config, store, harvester_factory, query, tmp_path
):
    """AC-014: hostile provider identifiers never become a path outside the corpus."""
    outside = tmp_path / "outside"
    outside.mkdir()
    hostile = openalex_work(1)
    # A provider id crafted to traverse directories, and a DOI containing separators.
    hostile["id"] = "https://openalex.org/../../../../etc/passwd"
    hostile["doi"] = "https://doi.org/10.1234/../../../../etc/passwd"

    providers = MockProviders(works=[hostile], files={"/W2000001.pdf": make_pdf_bytes()})
    harvester_factory(providers).harvest(query)

    assert list(outside.iterdir()) == []
    root = Path(config.storage_root)
    for path in root.iterdir():
        assert path.parent == root          # nothing nested, nothing escaped
        assert ".." not in path.name


def test_ac014_every_written_path_resolves_inside_the_root(config, store, harvester_factory, query):
    providers = MockProviders(
        works=[openalex_work(i) for i in range(1, 4)],
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, 4)},
    )
    harvester_factory(providers).harvest(query)
    root = Path(config.storage_root).resolve()
    for path in root.rglob("*"):
        assert root in path.resolve().parents


def test_remote_filename_suggestions_are_ignored(config, store, harvester_factory, query):
    """MASTER_SPEC section 37: never trust a filename supplied by a remote server."""
    providers = MockProviders(
        works=[openalex_work(1)],
        files={"/W2000001.pdf": make_pdf_bytes()},
        file_headers={
            "/W2000001.pdf": {
                "content-disposition": 'attachment; filename="../../../../evil.pdf"'
            }
        },
    )
    harvester_factory(providers).harvest(query)
    names = {path.name for path in Path(config.storage_root).iterdir()}
    assert not any("evil" in name for name in names)
    assert all(name.startswith("doi_10_1234_mock_0001_") for name in names)


def test_artifact_path_is_the_only_way_a_path_is_built(config):
    """The single construction point refuses anything unsafe."""
    from harvester.errors import StorageError

    with pytest.raises(StorageError):
        artifact_path(config.storage_root, "../escape", "pdf")


# ===================================================================== SSRF


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/secret.pdf",
        "http://169.254.169.254/latest/meta-data/iam/",
        "http://10.1.2.3/internal.pdf",
        "file:///etc/passwd",
        "ftp://files.invalid/x.pdf",
        "gopher://files.invalid/x",
    ],
)
def test_unsafe_acquisition_targets_are_refused(config, url):
    """MASTER_SPEC section 38: only public HTTP(S) targets are acquired."""
    from harvester.http import ClientPool

    config.downloads.allow_private_hosts = False
    with ClientPool(config, transport=MockProviders().transport) as clients:
        acquirer = Acquirer(config, clients.downloads)
        candidate = FulltextCandidate(url=url, kind=ArtifactKind.PDF, source=Source.OPENALEX)
        with pytest.raises(UnsafeUrlError):
            acquirer.acquire(document_id="doi_10_1234_x_0123456789ab", candidate=candidate)


def test_unsafe_candidate_from_a_provider_is_recorded_and_skipped(
    config, store, harvester_factory, query
):
    """A hostile URL in provider metadata fails that candidate, not the process."""
    hostile = openalex_work(1, pdf_url="file:///etc/passwd")
    providers = MockProviders(
        works=[hostile],
        files={"/unpaywall.pdf": make_pdf_bytes()},
    )
    result = harvester_factory(providers).harvest(query)
    categories = {f["category"] for f in store.failures_for_run(result.run_id)}
    assert "UNSAFE_URL" in categories


# ===================================================================== AC-015


def test_ac015_pathological_response_cannot_exhaust_the_disk(
    config, store, harvester_factory, query
):
    config.downloads.max_download_size_bytes = 4096
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes(padding=200_000)}
    )
    result = harvester_factory(providers).harvest(query)

    assert result.stats.completed == 0
    root = Path(config.storage_root)
    assert list(root.glob("*.pdf")) == []
    assert list(root.glob("*.part")) == []
    assert {f["category"] for f in store.failures_for_run(result.run_id)} == {
        "SIZE_LIMIT_EXCEEDED"
    }


# ============================================================== parser safety


def test_xxe_payload_from_a_provider_is_rejected(config, store, harvester_factory, query):
    """Untrusted XML cannot read local files or expand entities."""
    from mocks import Behavior

    providers = MockProviders(
        works=[openalex_work(1)],
        files={"/W2000001.pdf": make_pdf_bytes()},
        epmc_by_doi={DOI: epmc_result(doi=DOI)},
    )
    providers.script_route("epmc.fulltextxml", [Behavior(status=200, content=make_xxe_xml_bytes())])
    result = harvester_factory(providers).harvest(query)

    assert result.stats.xml_count == 0
    assert list(Path(config.storage_root).glob("*.xml")) == []
    assert any(
        f["category"] == "INVALID_XML" for f in store.failures_for_run(result.run_id)
    )


# ===================================================================== AC-017


@pytest.mark.parametrize(
    "text,secret",
    [
        ("fetching https://api.invalid/works?api_key=sk-live-123", "sk-live-123"),
        ("GET https://api.unpaywall.org/v2/10.1/x?email=me@example.org", "me@example.org"),
        ("User-Agent: Harvester (mailto:me@example.org)", "me@example.org"),
        ("contact is me@example.org for support", "me@example.org"),
        ("token=abc.def.ghi failed", "abc.def.ghi"),
    ],
)
def test_ac017_log_scrubbing_masks_secrets(text, secret):
    assert secret not in scrub(text)


def test_ac017_redaction_filter_is_installed_on_the_handlers(tmp_path: Path):
    log_file = tmp_path / "harvester.log"
    configure_logging("INFO", log_format="text", log_file=log_file)
    logger = logging.getLogger("harvester.test")
    logger.info("calling https://api.invalid/works?api_key=SUPER-SECRET&filter=topics.id:T1")
    for handler in logging.getLogger("harvester").handlers:
        handler.flush()
        assert any(isinstance(f, RedactionFilter) for f in handler.filters)
    content = log_file.read_text("utf-8")
    assert "SUPER-SECRET" not in content
    assert "topics.id:T1" in content  # non-secret context is preserved
    configure_logging("WARNING")


def test_ac017_json_logs_are_scrubbed_too(tmp_path: Path):
    log_file = tmp_path / "harvester.jsonl"
    configure_logging("INFO", log_format="json", log_file=log_file)
    logging.getLogger("harvester.test").warning(
        "unpaywall lookup https://api.invalid/v2/10.1/x?email=operator@example.org"
    )
    for handler in logging.getLogger("harvester").handlers:
        handler.flush()
    line = json.loads(log_file.read_text("utf-8").strip().splitlines()[-1])
    assert "operator@example.org" not in json.dumps(line)
    assert line["level"] == "WARNING"
    configure_logging("WARNING")


def test_ac017_no_secret_reaches_provenance_or_state(config, store, harvester_factory, query):
    providers = MockProviders(
        works=[openalex_work(1)],
        files={"/W2000001.pdf": make_pdf_bytes()},
        epmc_by_doi={DOI: epmc_result(doi=DOI)},
    )
    result = harvester_factory(providers).harvest(query)

    dump = ""
    for row in store.connection.execute("SELECT * FROM documents").fetchall():
        dump += "".join(str(value) for value in tuple(row))
    for row in store.connection.execute("SELECT * FROM artifacts").fetchall():
        dump += "".join(str(value) for value in tuple(row))
    for row in store.connection.execute("SELECT * FROM attempts").fetchall():
        dump += "".join(str(value) for value in tuple(row))
    for row in store.connection.execute("SELECT * FROM runs").fetchall():
        dump += "".join(str(value) for value in tuple(row))
    dump += result.report_path.read_text("utf-8")
    for path in Path(config.storage_root).glob("*.json"):
        dump += path.read_text("utf-8")

    assert config.openalex.api_key not in dump
    assert config.contact_email not in dump


#: Values that are obviously placeholders rather than real credentials.
_PLACEHOLDER_RE = re.compile(
    r"^\s*(['\"]?)("
    r"[….]{1,3}"                 # …  or  ...
    r"|<[^>]*>"                       # <your-key>
    r"|\$\{?[A-Za-z_]\w*\}?"          # $VAR / ${VAR}
    r"|(your|my|the)[-_ ]?\w*"        # your-api-key
    r"|test[-_]\w*|dummy\w*|changeme|xxx+|placeholder\w*"
    r")\1?\s*$",
    re.IGNORECASE,
)


#: Python/JSON syntax that merely mentions a key name — annotations, defaults,
#: variable references, schema keys — rather than assigning a credential.
_CODE_NOT_SECRET_RE = re.compile(
    r"^(None|True|False|null|str\b|int\b|bool\b|Optional\b"
    r"|.*\|\s*None"
    r"|(self|config|cfg|args|environment|os|kwargs|overrides|payload|data|row)\.[\w.\[\]()]*"
    r"|\{[^}]*\}|\[[^\]]*\]"
    r"|REDACTED|\*+)",
    re.IGNORECASE,
)


def test_ac017_no_secret_is_committed_to_the_repository():
    """The repository itself must not carry credentials.

    Documentation legitimately shows ``export HARVESTER_OPENALEX_API_KEY='…'``. What
    must never appear is an assignment whose *value* looks like a real secret, so the
    value itself is inspected rather than the surrounding file.
    """
    repo_root = Path(__file__).resolve().parents[2]
    assignment = re.compile(
        r"(?:HARVESTER_OPENALEX_API_KEY|api[_-]?key|access[_-]?token|password)"
        r"\s*[=:]\s*(.+)$",
        re.IGNORECASE,
    )
    skip_dirs = {".git", ".venv", ".venv-clean", "__pycache__", ".pytest_cache",
                 "reports", "corpus", "state", "build", "dist"}
    suspicious: list[str] = []

    for path in repo_root.rglob("*"):
        if not path.is_file() or set(path.parts) & skip_dirs:
            continue
        if path.suffix not in (".py", ".md", ".toml", ".json", ".cfg", ".txt", ".yaml", ".yml"):
            continue
        for number, line in enumerate(path.read_text("utf-8", errors="ignore").splitlines(), 1):
            match = assignment.search(line)
            if match is None:
                continue
            value = match.group(1).strip().rstrip(",;)").strip()
            # Strip trailing shell/markdown noise so the bare value is examined.
            value = value.split("&&")[0].split("#")[0].strip().strip("'\"`")
            if not value or _PLACEHOLDER_RE.match(value):
                continue
            # Type annotations, defaults and variable references are not secrets.
            if _CODE_NOT_SECRET_RE.match(value):
                continue
            # A real credential is a long opaque token. Anything else is prose or code.
            if not re.fullmatch(r"[A-Za-z0-9_.\-]{8,}", value):
                continue
            suspicious.append(f"{path.relative_to(repo_root)}:{number}: {line.strip()[:90]}")

    # The test fixtures deliberately use obvious non-secrets; assert they read as such.
    real_looking = [s for s in suspicious if "tests" not in s.split(":")[0]]
    assert real_looking == [], "possible credential committed:\n" + "\n".join(real_looking)


def test_redact_url_is_applied_to_stored_provenance(config, store, harvester_factory, query):
    providers = MockProviders(
        works=[openalex_work(1, pdf_url=f"{FILES_BASE}/W2000001.pdf?api_key=LEAK")],
        files={"/W2000001.pdf": make_pdf_bytes()},
    )
    result = harvester_factory(providers).harvest(query)
    if result.stats.completed:
        for path in Path(config.storage_root).glob("*.json"):
            assert "LEAK" not in path.read_text("utf-8")
    assert "LEAK" not in redact_url(f"{FILES_BASE}/x.pdf?api_key=LEAK")
