"""Unit tests: DOI normalization, document identity, filesystem and URL safety.

Covers AC-001 (DOI normalization) and part of AC-014 (path traversal).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harvester.errors import StorageError, UnsafeUrlError
from harvester.identity import (
    artifact_path,
    document_id_for_doi,
    document_id_for_source,
    is_safe_document_id,
    is_valid_doi,
    normalize_doi,
    sha256_bytes,
    validate_remote_url,
)

# --------------------------------------------------------------------- AC-001

SPEC_REPRESENTATIONS = [
    "10.1234/ABC",
    "doi:10.1234/ABC",
    "https://doi.org/10.1234/ABC",
    "https://dx.doi.org/10.1234/ABC",
]


def test_ac001_all_spec_representations_normalize_to_one_value():
    """AC-001: every representation named in MASTER_SPEC section 9 collapses to one DOI."""
    normalized = {normalize_doi(value) for value in SPEC_REPRESENTATIONS}
    assert normalized == {"10.1234/abc"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  10.1234/abc  ", "10.1234/abc"),
        ("http://doi.org/10.1234/abc", "10.1234/abc"),
        ("http://dx.doi.org/10.1234/abc", "10.1234/abc"),
        ("https://www.doi.org/10.1234/abc", "10.1234/abc"),
        ("doi.org/10.1234/abc", "10.1234/abc"),
        ("info:doi/10.1234/abc", "10.1234/abc"),
        ("<https://doi.org/10.1234/abc>", "10.1234/abc"),
        ("DOI:10.1234/ABC", "10.1234/abc"),
        ("10.1234/abc.", "10.1234/abc"),
        ("10.1234/abc,", "10.1234/abc"),
        ("10.1234/abc;", "10.1234/abc"),
        ("(10.1234/abc)", "10.1234/abc"),
        ("10.1371/journal.pone.0123456", "10.1371/journal.pone.0123456"),
    ],
)
def test_normalization_variants(raw, expected):
    assert normalize_doi(raw) == expected


def test_meaningful_doi_characters_are_never_transformed():
    """A DOI whose suffix legitimately contains punctuation keeps it."""
    assert normalize_doi("10.1234/foo(bar)") == "10.1234/foo(bar)"
    assert normalize_doi("10.1234/a-b_c.d") == "10.1234/a-b_c.d"
    assert normalize_doi("10.1234/A:B;C") == "10.1234/a:b;c"


@pytest.mark.parametrize(
    "raw", [None, "", "   ", "not-a-doi", "10.12/x", "https://example.org/", "10.1234", "doi:"]
)
def test_invalid_dois_are_rejected_not_repaired(raw):
    assert normalize_doi(raw) is None
    assert is_valid_doi(raw) is False


# ------------------------------------------------------------------- identity


def test_document_id_is_deterministic_and_readable():
    doi = "10.1371/journal.pone.0123456"
    first = document_id_for_doi(doi)
    assert first == document_id_for_doi(doi)
    assert first.startswith("doi_10_1371_journal_pone_0123456_")
    assert is_safe_document_id(first)


def test_document_id_distinguishes_dois_that_slugify_identically():
    """``.``, ``/`` and ``-`` all slugify to ``_``; the fingerprint keeps IDs distinct."""
    a = document_id_for_doi("10.1234/a.b")
    b = document_id_for_doi("10.1234/a-b")
    c = document_id_for_doi("10.1234/a/b")
    assert len({a, b, c}) == 3


def test_document_id_is_bounded_for_pathological_dois():
    doi = "10.1234/" + "x" * 5000
    document_id = document_id_for_doi(doi)
    assert len(document_id) < 200
    assert is_safe_document_id(document_id)


def test_source_fallback_identity_is_stable():
    first = document_id_for_source("openalex", "W2741809807")
    assert first == document_id_for_source("openalex", "W2741809807")
    assert first.startswith("openalex_w2741809807_")
    assert first != document_id_for_source("openalex", "W2741809808")


def test_source_identity_refuses_unusable_inputs():
    with pytest.raises(StorageError):
        document_id_for_source("", "")


# --------------------------------------------------------------- path safety


def test_artifact_path_is_flat_and_inside_the_root(tmp_path: Path):
    """SPEC_PATCH section 3: siblings in one flat directory, no per-document folder."""
    root = tmp_path / "corpus"
    root.mkdir()
    document_id = document_id_for_doi("10.1234/abc")
    pdf = artifact_path(root, document_id, "pdf")
    json_path = artifact_path(root, document_id, ".JSON")
    assert pdf.parent == root.resolve()
    assert pdf.name == f"{document_id}.pdf"
    assert json_path.name == f"{document_id}.json"


@pytest.mark.parametrize(
    "malicious",
    [
        "../../etc/passwd",
        "..\\..\\windows\\system32",
        "/etc/passwd",
        "a/b",
        "a\\b",
        "doi_10_1234_abc/../../escape",
        "",
        ".",
        "..",
        "con:",
        "with space",
        "UPPER",
        "x" * 500,
    ],
)
def test_ac014_path_traversal_and_unsafe_ids_are_refused(tmp_path: Path, malicious):
    """AC-014: remote metadata cannot cause a write outside the storage root."""
    assert not is_safe_document_id(malicious)
    with pytest.raises(StorageError):
        artifact_path(tmp_path, malicious, "pdf")


@pytest.mark.parametrize("extension", ["../pdf", "pd/f", "", "p" * 20, "exe;"])
def test_unsafe_extensions_are_refused(tmp_path: Path, extension):
    with pytest.raises(StorageError):
        artifact_path(tmp_path, "doi_10_1234_abc_0123456789ab", extension)


# ---------------------------------------------------------------- URL safety


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.invalid/file.pdf",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:application/pdf;base64,AAAA",
        "",
        "https://",
        "https://user:pass@example.invalid/x.pdf",
        "https://example.invalid/x.pdf\nHost: evil",
        " https://example.invalid/x.pdf",
    ],
)
def test_unsafe_urls_are_refused(url):
    with pytest.raises(UnsafeUrlError):
        validate_remote_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x.pdf",
        "https://localhost/x.pdf",
        "http://10.0.0.1/x.pdf",
        "http://192.168.1.1/x.pdf",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/x.pdf",
        "http://0.0.0.0/x.pdf",
    ],
)
def test_private_and_loopback_targets_are_refused_by_default(url):
    with pytest.raises(UnsafeUrlError):
        validate_remote_url(url)


def test_private_targets_can_be_allowed_explicitly():
    assert validate_remote_url("http://127.0.0.1/x.pdf", allow_private_hosts=True)


def test_public_https_url_is_accepted():
    url = "https://files.invalid/W1.pdf"
    assert validate_remote_url(url) == url


def test_sha256_is_the_standard_digest():
    assert sha256_bytes(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
