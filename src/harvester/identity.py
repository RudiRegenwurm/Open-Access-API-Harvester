"""Canonical identity: DOI normalization, document IDs and filesystem safety.

MASTER_SPEC sections 9 and 37.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from .errors import StorageError, UnsafeUrlError

#: A DOI is a "10." prefix, a registrant code, "/", and a suffix.
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

#: Prefixes stripped during normalization (checked case-insensitively, longest first).
_URL_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "https://www.doi.org/",
    "http://www.doi.org/",
    "doi.org/",
    "dx.doi.org/",
    "info:doi/",
    "doi:",
    "doi/",
)

#: Trailing characters that are punctuation artefacts of copy/paste or URL embedding.
#: A closing bracket is only stripped when it is unbalanced, so DOIs such as
#: ``10.1234/foo(bar)`` keep their meaningful parentheses.
_TRAILING_PUNCT = ".,;:'\"<>"
_BRACKET_PAIRS = {")": "(", "]": "[", "}": "{"}

#: Symmetric wrappers stripped when they enclose the *whole* value: opener -> closer.
_WRAPPERS = {"<": ">", "(": ")", "[": "]", "{": "}", '"': '"', "'": "'"}

#: Longest slug segment kept in a document ID before the fingerprint. Keeps the
#: complete filename (``<id>.pdf``) comfortably inside the 255-byte limit that
#: ext4/APFS/NTFS impose on a single path component.
_MAX_SLUG_LEN = 100

#: Length of the deterministic fingerprint appended to every document ID.
_FINGERPRINT_LEN = 12

_DOCUMENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,199}$")


def normalize_doi(raw: str | None) -> str | None:
    """Return the canonical form of *raw*, or ``None`` if it is not a usable DOI.

    All of the representations named in MASTER_SPEC section 9 collapse to one value.
    Meaningful DOI characters are never transformed; only URL/scheme wrappers,
    surrounding whitespace and safely identifiable trailing punctuation are removed.
    DOIs are case-insensitive, so the canonical form is lower-cased.
    """
    # Provider payloads are untrusted: a field documented as a string may arrive as a
    # number, an object or null. Anything that is not a string is simply not a DOI.
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None

    # Strip symmetric wrappers left by copy/paste, e.g. "<https://doi.org/10.1/x>"
    # or "(10.1234/abc)". Both ends must match, so a DOI that merely *contains*
    # brackets — "10.1234/foo(bar)" — is untouched.
    changed = True
    while changed and len(value) >= 2:
        changed = False
        closer = _WRAPPERS.get(value[0])
        if closer is not None and value[-1] == closer:
            value = value[1:-1].strip()
            changed = True

    # Remove URL / scheme prefixes, repeatedly (handles "doi:https://doi.org/...").
    changed = True
    while changed:
        changed = False
        lowered = value.lower()
        for prefix in _URL_PREFIXES:
            if lowered.startswith(prefix):
                value = value[len(prefix) :].strip()
                changed = True
                break

    value = _strip_trailing_punctuation(value)
    if not value:
        return None

    value = value.lower()
    if not _DOI_RE.match(value):
        return None
    return value


def _strip_trailing_punctuation(value: str) -> str:
    """Remove trailing punctuation that cannot belong to the DOI."""
    while value:
        last = value[-1]
        if last in _TRAILING_PUNCT:
            value = value[:-1]
            continue
        opener = _BRACKET_PAIRS.get(last)
        if opener is not None and value.count(opener) < value.count(last):
            # Unbalanced closer: an artefact of the surrounding text, not the DOI.
            value = value[:-1]
            continue
        break
    return value.strip()


#: Hosts whose URLs address a PMC record by its PMCID. Restricted deliberately: on any
#: other host ``PMC1234`` in a path is an arbitrary string, not an identifier.
_PMC_HOSTS = frozenset(
    {"pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "europepmc.org",
     "www.europepmc.org"}
)
_PMCID_IN_URL_RE = re.compile(r"(?:^|/|=)(PMC\d+)(?:$|[/?&.])", re.IGNORECASE)


def pmcid_from_urls(urls: Iterable[str]) -> str | None:
    """The single PMCID that *urls* unambiguously point at, or ``None``.

    This derives an identifier the providers did not state outright — the PMCID is
    sitting inside full-text URLs OpenAlex and Unpaywall already supplied — so it is
    deliberately unwilling to guess. A PMCID is returned only when the URL is on a
    host that identifies records that way, and only when every such URL agrees:
    two different PMCIDs mean the document's identity is unclear, and an unclear
    identity is worth less than none at all.

    The caller must record the result as derived, never as a provider assertion.
    """
    found: set[str] = set()
    for url in urls:
        if not isinstance(url, str) or not url:
            continue
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if (parsed.hostname or "").lower() not in _PMC_HOSTS:
            continue
        for part in (parsed.path, parsed.query):
            for match in _PMCID_IN_URL_RE.finditer(part or ""):
                found.add(match.group(1).upper())
    if len(found) != 1:
        return None
    return found.pop()


def is_valid_doi(raw: str | None) -> bool:
    return normalize_doi(raw) is not None


def _slugify(value: str) -> str:
    """Lower-case, replace every character outside ``[a-z0-9]`` with ``_``, collapse runs."""
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower())
    return slug.strip("_")


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_FINGERPRINT_LEN]


def document_id_for_doi(canonical_doi: str) -> str:
    """Build the deterministic document ID for a canonical DOI.

    Example::

        10.1371/journal.pone.0123456
            -> doi_10_1371_journal_pone_0123456_5d41402abc4b

    The trailing fingerprint is a deterministic function of the canonical DOI alone.
    It is mandatory rather than cosmetic: slugification maps ``.``, ``/`` and ``-``
    onto the same character and long DOIs are truncated, so without it two distinct
    DOIs (``10.1/a.b`` and ``10.1/a-b``) would share one filename and silently
    overwrite each other's artifacts.
    """
    slug = _slugify(canonical_doi)[:_MAX_SLUG_LEN].strip("_")
    return f"doi_{slug}_{_fingerprint(canonical_doi)}"


def document_id_for_source(source: str, source_id: str) -> str:
    """Fallback identity for a record that legitimately has no DOI.

    MASTER_SPEC section 8.1 identity hierarchy step 2: a stable provider identifier.
    """
    source_slug = _slugify(source)
    id_slug = _slugify(source_id)[:_MAX_SLUG_LEN].strip("_")
    if not source_slug or not id_slug:
        raise StorageError(
            f"cannot derive a document identity from source={source!r} id={source_id!r}"
        )
    return f"{source_slug}_{id_slug}_{_fingerprint(f'{source}:{source_id}')}"


def is_safe_document_id(document_id: str) -> bool:
    """True when *document_id* is safe to use as a single path component."""
    return bool(_DOCUMENT_ID_RE.match(document_id))


def artifact_path(storage_root: Path, document_id: str, extension: str) -> Path:
    """Resolve the flat A1 path ``<storage_root>/<document_id>.<extension>``.

    SPEC_PATCH section 3 fixes the flat layout. This function is the single place
    where a final artifact path is constructed, and it refuses to escape the root
    (MASTER_SPEC section 37: path-traversal protection, never write outside the root).
    """
    if not is_safe_document_id(document_id):
        raise StorageError(f"unsafe document_id refused: {document_id!r}")
    ext = extension.lstrip(".").lower()
    if not re.fullmatch(r"[a-z0-9]{1,8}", ext):
        raise StorageError(f"unsafe artifact extension refused: {extension!r}")

    # Containment is guaranteed by construction: the two regexes above admit no path
    # separator, no drive letter and no "..", so the joined name is necessarily a
    # single component directly under the root. The explicit check below re-states
    # that invariant so a future loosening of either regex fails loudly here.
    #
    # It deliberately does *not* compare ``candidate.resolve()`` against
    # ``root.resolve()``: on Windows, resolve() adds the ``\\?\`` extended-length
    # prefix only once a path crosses MAX_PATH, so two paths in the same directory
    # can compare unequal purely because of their length.
    name = f"{document_id}.{ext}"
    if os.path.basename(name) != name or name in (".", ".."):
        raise StorageError(f"artifact name escapes storage root: {name!r}")

    root = Path(storage_root).resolve()
    return root / name


_PRIVATE_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


def validate_remote_url(url: str, *, allow_private_hosts: bool = False) -> str:
    """Validate an untrusted remote URL before it is fetched.

    MASTER_SPEC sections 37 and 38: remote metadata is untrusted input; only HTTP(S)
    is acceptable for document acquisition, and private/loopback targets are refused
    unless deliberately allowed (tests point at a local mock server).
    """
    if not url or not isinstance(url, str):
        raise UnsafeUrlError("empty or non-string URL")
    stripped = url.strip()
    if stripped != url or "\n" in url or "\r" in url or "\t" in url:
        raise UnsafeUrlError("URL contains surrounding or embedded whitespace")

    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise UnsafeUrlError(f"unsupported URL scheme: {parts.scheme!r}")
    if not parts.hostname:
        raise UnsafeUrlError("URL has no host")
    if parts.username or parts.password:
        raise UnsafeUrlError("URL embeds credentials")

    if not allow_private_hosts:
        _reject_private_target(parts.hostname)
    return url


def _reject_private_target(hostname: str) -> None:
    host = hostname.lower().strip("[]")
    if host in _PRIVATE_HOSTNAMES:
        raise UnsafeUrlError(f"refusing loopback host: {hostname}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A name, not a literal address. Resolve it so that a hostname pointing at
        # a private address cannot be used to reach internal services.
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return  # Unresolvable now; the HTTP layer will fail cleanly and be retried.
        for info in infos:
            try:
                resolved = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            _reject_private_address(resolved, hostname)
        return
    _reject_private_address(address, hostname)


def _reject_private_address(address: ipaddress._BaseAddress, hostname: str) -> None:
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise UnsafeUrlError(f"refusing non-public network target: {hostname} -> {address}")


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """SHA-256 of a file, streamed (MASTER_SPEC section 18)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
