"""Unit tests: artifact validation. Covers AC-005, AC-006 and part of AC-012."""

from __future__ import annotations

from pathlib import Path

import pytest

from harvester.errors import InvalidPdfError, InvalidXmlError
from harvester.identity import sha256_bytes
from harvester.validation import (
    content_type_is_plausible,
    looks_like_html,
    validate_pdf,
    validate_xml,
)
from mocks import (
    make_corrupt_pdf_bytes,
    make_html_bytes,
    make_malformed_xml_bytes,
    make_pdf_bytes,
    make_truncated_pdf_bytes,
    make_xml_bytes,
    make_xxe_xml_bytes,
)


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------ happy path


def test_valid_pdf_passes_and_yields_sha256(tmp_path: Path):
    data = make_pdf_bytes()
    path = write(tmp_path, "good.pdf", data)
    result = validate_pdf(path, min_size_bytes=512)
    assert result.sha256 == sha256_bytes(data)
    assert result.size_bytes == len(data)
    assert result.details["pages"] >= 1


def test_valid_xml_passes_and_yields_sha256(tmp_path: Path):
    data = make_xml_bytes()
    path = write(tmp_path, "good.xml", data)
    result = validate_xml(path, min_size_bytes=64)
    assert result.sha256 == sha256_bytes(data)
    assert result.details["root"] == "article"


# --------------------------------------------------------------------- AC-005


def test_ac005_html_saved_as_pdf_is_rejected(tmp_path: Path):
    """AC-005: an HTML page with a .pdf filename must be rejected."""
    path = write(tmp_path, "trap.pdf", make_html_bytes())
    with pytest.raises(InvalidPdfError) as excinfo:
        validate_pdf(path, min_size_bytes=10)
    assert "HTML" in str(excinfo.value)


def test_html_is_rejected_even_when_served_as_application_pdf(tmp_path: Path):
    """A correct Content-Type header does not make HTML a PDF."""
    assert content_type_is_plausible("pdf", "application/pdf") is True
    path = write(tmp_path, "trap.pdf", make_html_bytes())
    with pytest.raises(InvalidPdfError):
        validate_pdf(path, min_size_bytes=10)


def test_html_detection_tolerates_leading_whitespace_and_comments():
    assert looks_like_html(b"\n\n   <!DOCTYPE html><html>")
    assert looks_like_html(b"<html lang='en'>")
    assert not looks_like_html(b"%PDF-1.7\n")


# --------------------------------------------------------------------- AC-006


def test_ac006_truncated_pdf_is_rejected(tmp_path: Path):
    """AC-006: a truncated PDF never becomes a successful artifact."""
    path = write(tmp_path, "cut.pdf", make_truncated_pdf_bytes())
    with pytest.raises(InvalidPdfError) as excinfo:
        validate_pdf(path, min_size_bytes=10)
    assert "truncated" in str(excinfo.value).lower()


def test_ac006_corrupt_pdf_body_is_rejected(tmp_path: Path):
    path = write(tmp_path, "corrupt.pdf", make_corrupt_pdf_bytes())
    with pytest.raises(InvalidPdfError):
        validate_pdf(path, min_size_bytes=10)


# ---------------------------------------------------------------- other checks


def test_empty_artifact_is_rejected(tmp_path: Path):
    with pytest.raises(InvalidPdfError, match="empty"):
        validate_pdf(write(tmp_path, "empty.pdf", b""), min_size_bytes=10)
    with pytest.raises(InvalidXmlError, match="empty"):
        validate_xml(write(tmp_path, "empty.xml", b""), min_size_bytes=10)


def test_undersized_artifact_is_rejected(tmp_path: Path):
    path = write(tmp_path, "tiny.pdf", make_pdf_bytes())
    with pytest.raises(InvalidPdfError, match="below the configured minimum"):
        validate_pdf(path, min_size_bytes=10_000_000)


def test_missing_magic_bytes_are_rejected(tmp_path: Path):
    path = write(tmp_path, "plain.pdf", b"just some text" * 100)
    with pytest.raises(InvalidPdfError, match="magic bytes"):
        validate_pdf(path, min_size_bytes=10)


def test_missing_file_is_reported_not_crashed(tmp_path: Path):
    with pytest.raises(InvalidPdfError, match="missing"):
        validate_pdf(tmp_path / "nope.pdf", min_size_bytes=10)
    with pytest.raises(InvalidXmlError, match="missing"):
        validate_xml(tmp_path / "nope.xml", min_size_bytes=10)


def test_malformed_xml_is_rejected(tmp_path: Path):
    path = write(tmp_path, "bad.xml", make_malformed_xml_bytes())
    with pytest.raises(InvalidXmlError, match="not well-formed"):
        validate_xml(path, min_size_bytes=10)


def test_xml_external_entity_is_rejected(tmp_path: Path):
    """Untrusted provider XML cannot pull in local files (MASTER_SPEC section 37)."""
    path = write(tmp_path, "xxe.xml", make_xxe_xml_bytes())
    with pytest.raises(InvalidXmlError):
        validate_xml(path, min_size_bytes=10)


def test_html_saved_as_xml_is_rejected(tmp_path: Path):
    path = write(tmp_path, "trap.xml", make_html_bytes())
    with pytest.raises(InvalidXmlError):
        validate_xml(path, min_size_bytes=10)


@pytest.mark.parametrize(
    "kind,content_type,expected",
    [
        ("pdf", "application/pdf", True),
        ("pdf", "application/pdf; charset=binary", True),
        ("pdf", "text/html", False),
        ("pdf", None, True),
        ("xml", "application/xml", True),
        ("xml", "text/xml", True),
        ("xml", "application/jats+xml", True),
        ("xml", "image/png", False),
    ],
)
def test_content_type_plausibility_is_advisory(kind, content_type, expected):
    assert content_type_is_plausible(kind, content_type) is expected
