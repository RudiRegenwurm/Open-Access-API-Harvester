# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""Artifact validation (MASTER_SPEC sections 15, 16, 18).

A successful HTTP 200 is never sufficient. An artifact becomes "successful" only after
it passes every check below and its SHA-256 has been computed. Nothing here trusts a
Content-Type header on its own — an HTML error page served as ``application/pdf`` is
still rejected, and a correct PDF served as ``text/html`` is still accepted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .errors import BotChallengeError, InvalidPdfError, InvalidXmlError
from .identity import sha256_file

LOGGER = logging.getLogger("harvester.validation")

PDF_MAGIC = b"%PDF-"
#: Bytes read from the head of a file for magic/sniffing checks.
_SNIFF_BYTES = 2048
#: Distance from EOF searched for the PDF trailer marker.
_TRAILER_WINDOW = 4096

_HTML_MARKERS = (b"<!doctype html", b"<html", b"<head", b"<body")

#: Bytes examined when deciding whether an HTML body is an access challenge. The
#: markers sit in a script block near the end of the page, past the sniff window.
_CHALLENGE_BYTES = 16 * 1024

#: Evidence that a *challenge* was served rather than the artifact. Taken verbatim
#: from the page NCBI PMC returns for ``/articles/PMC…/pdf/…`` (observed 2026-08-19):
#: a proof-of-work puzzle whose solution sets the ``cloudpmc-viewer-pow`` cookie.
#: Deliberately narrow — these strings identify the challenge itself, not any page
#: that merely mentions robots — and deliberately inert: the harvester recognises
#: the challenge in order to report it, and never solves or works around one.
_CHALLENGE_MARKERS = (
    b"cloudpmc-viewer-pow",
    b"pow_challenge",
    b"window.ncbi.pmc.pow",
)


@dataclass(slots=True)
class ValidationResult:
    sha256: str
    size_bytes: int
    details: dict[str, object]


def looks_like_html(head: bytes) -> bool:
    """True when the response body *begins* as an HTML document.

    Only the prefix is examined. Searching the whole head for ``<body`` would
    misclassify legitimate JATS full-text XML, which contains a ``<body>`` element
    of its own.
    """
    lowered = head[:512].lstrip().lstrip(b"\xef\xbb\xbf").lstrip().lower()
    return any(lowered.startswith(marker) for marker in _HTML_MARKERS)


def looks_like_bot_challenge(data: bytes) -> bool:
    """True when a body is an automated-access challenge rather than an artifact.

    Knowing the difference matters to an operator: a rejected *file* means the
    location is broken, while a challenge means the location is fine and closed to
    us. Only the second is worth routing around via another provider.
    """
    lowered = data[:_CHALLENGE_BYTES].lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def validate_pdf(path: Path, *, min_size_bytes: int) -> ValidationResult:
    """Run the full PDF validation chain from MASTER_SPEC section 15.

    Raises :class:`InvalidPdfError` with a precise reason on any failure.
    """
    if not path.exists():
        raise InvalidPdfError(f"artifact is missing: {path.name}")
    size = path.stat().st_size
    if size == 0:
        raise InvalidPdfError("artifact is empty")

    with open(path, "rb") as handle:
        head = handle.read(_SNIFF_BYTES)
        if size > _TRAILER_WINDOW:
            handle.seek(-_TRAILER_WINDOW, 2)
        else:
            handle.seek(0)
        tail = handle.read()

    # Identity is checked before size: "this is an HTML error page" is a far more
    # actionable diagnosis for an operator than "this file is too small", and short
    # error pages would otherwise always be reported as the latter.
    if not head.startswith(PDF_MAGIC):
        if looks_like_html(head):
            # Distinguish "the location is broken" from "the location is closed to
            # automated clients": only the latter is worth trying another provider for.
            with open(path, "rb") as handle:
                if looks_like_bot_challenge(handle.read(_CHALLENGE_BYTES)):
                    raise BotChallengeError(
                        "the provider served an automated-access challenge instead of "
                        "the PDF; the file is not retrieved from this location"
                    )
            raise InvalidPdfError(
                "response is an HTML page, not a PDF (missing %PDF- magic bytes)"
            )
        raise InvalidPdfError("missing %PDF- magic bytes")

    if size < min_size_bytes:
        raise InvalidPdfError(
            f"artifact is {size} bytes, below the configured minimum of {min_size_bytes}"
        )

    # A complete PDF ends with the %%EOF trailer marker. Its absence is the usual
    # signature of a truncated download.
    if b"%%EOF" not in tail:
        raise InvalidPdfError("PDF is truncated: no %%EOF trailer marker")

    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            # Encrypted content is not circumvented (MASTER_SPEC section 59).
            raise InvalidPdfError("PDF is encrypted and cannot be validated")
        page_count = len(reader.pages)
        if page_count < 1:
            raise InvalidPdfError("PDF parsed but contains no pages")
        # Touch the first page so structurally broken cross-reference tables surface here
        # rather than downstream.
        _ = reader.pages[0]
    except InvalidPdfError:
        raise
    except (PyPdfError, ValueError, OSError, RecursionError) as exc:
        raise InvalidPdfError(f"PDF parser rejected the artifact: {exc}") from exc

    return ValidationResult(
        sha256=sha256_file(path),
        size_bytes=size,
        details={"pages": page_count, "magic": "%PDF-"},
    )


_XML_DECL_RE = re.compile(rb"^\s*(<\?xml|<!DOCTYPE|<)", re.IGNORECASE)


def validate_xml(path: Path, *, min_size_bytes: int) -> ValidationResult:
    """Validate an XML artifact (MASTER_SPEC section 16).

    Parsing uses :mod:`defusedxml`, so entity-expansion and external-entity attacks in
    untrusted provider content cannot affect this process (MASTER_SPEC section 37).
    """
    if not path.exists():
        raise InvalidXmlError(f"artifact is missing: {path.name}")
    size = path.stat().st_size
    if size == 0:
        raise InvalidXmlError("artifact is empty")

    with open(path, "rb") as handle:
        head = handle.read(_SNIFF_BYTES)
    if looks_like_html(head):
        raise InvalidXmlError("response is an HTML page, not XML full text")
    if not _XML_DECL_RE.match(head):
        raise InvalidXmlError("artifact does not begin with XML markup")

    if size < min_size_bytes:
        raise InvalidXmlError(
            f"artifact is {size} bytes, below the configured minimum of {min_size_bytes}"
        )

    try:
        tree = SafeElementTree.parse(str(path))
        root = tree.getroot()
    except DefusedXmlException as exc:
        raise InvalidXmlError(f"unsafe XML construct rejected: {exc}") from exc
    except SafeElementTree.ParseError as exc:
        raise InvalidXmlError(f"XML is not well-formed: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise InvalidXmlError(f"XML could not be parsed: {exc}") from exc

    if root is None:
        raise InvalidXmlError("XML has no root element")

    return ValidationResult(
        sha256=sha256_file(path),
        size_bytes=size,
        details={"root": str(root.tag)},
    )


def content_type_is_plausible(kind: str, content_type: str | None) -> bool:
    """Sanity-check a Content-Type header.

    Advisory only: many OA repositories mislabel PDFs. A failure here is logged as
    evidence but the byte-level checks decide.
    """
    if not content_type:
        return True
    value = content_type.split(";", 1)[0].strip().lower()
    if kind == "pdf":
        return value in ("application/pdf", "application/x-pdf", "application/octet-stream")
    if kind == "xml":
        return value.endswith("/xml") or value.endswith("+xml") or value in (
            "text/plain",
            "application/octet-stream",
        )
    return True
