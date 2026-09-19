"""Final storage: the flat A1 corpus and its mandatory JSON sidecars.

SPEC_PATCH section 3 fixes the downstream-facing layout as a hard product invariant::

    <storage_root>/
    ├── <document_id>.pdf
    ├── <document_id>.xml      (when legitimately available)
    └── <document_id>.json     (mandatory)

MASTER_SPEC section 20 requires the sidecar to distinguish bibliographic metadata,
artifact metadata, provenance and harvest state. No value in it is fabricated.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .errors import StorageError
from .http import redact_url
from .identity import artifact_path, sha256_file
from .models import ArtifactKind, ArtifactRecord
from .util import parse_json, to_pretty_json, utc_now_iso

LOGGER = logging.getLogger("harvester.storage")

#: 1.1 added the top-level ``document_status``. The change is additive and backwards
#: compatible — every 1.0 field kept its name and meaning — but a consumer must be able
#: to tell from the file alone whether the field is there to be read.
SIDECAR_SCHEMA_VERSION = "1.1"


@dataclass(slots=True)
class SidecarInputs:
    document_id: str
    metadata: dict[str, Any]
    artifacts: dict[str, ArtifactRecord]
    run_id: str
    status: str
    attempts: int


def storage_paths(storage_root: Path, document_id: str) -> dict[str, Path]:
    """The three deterministic sibling paths for one logical document."""
    return {
        "pdf": artifact_path(storage_root, document_id, "pdf"),
        "xml": artifact_path(storage_root, document_id, "xml"),
        "json": artifact_path(storage_root, document_id, "json"),
    }


def build_sidecar(inputs: SidecarInputs) -> dict[str, Any]:
    """Assemble the canonical sidecar document."""
    metadata = inputs.metadata
    artifacts = inputs.artifacts
    primary = artifacts.get(ArtifactKind.PDF.value) or artifacts.get(ArtifactKind.XML.value)

    discovered_via = list(metadata.get("discovered_via") or [])
    cross_checks = list(metadata.get("cross_checks") or [])
    cross_checked_via = []
    for check in cross_checks:
        source = check.get("source")
        if source and source not in cross_checked_via:
            cross_checked_via.append(source)

    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "document_id": inputs.document_id,
        # The harvester's own view of this document — COMPLETED, FAILED_PERMANENT,
        # FAILED_RETRYABLE. Stated at the top level and under an unambiguous name
        # because the only other status here is ``oa_status``, which is the
        # bibliographic Open-Access status of the *article* ("gold", "green") and says
        # nothing about whether this harvest succeeded. Reading one for the other is
        # exactly the mistake this name prevents. Also kept under ``harvest.status``,
        # where it has always been, so existing readers are unaffected.
        "document_status": inputs.status,
        # -- bibliographic ---------------------------------------------------
        "doi": metadata.get("doi"),
        "title": metadata.get("title"),
        "authors": list(metadata.get("authors") or []),
        "publication_year": metadata.get("publication_year"),
        "journal": metadata.get("journal"),
        "abstract": metadata.get("abstract"),
        "domain_tags": list(metadata.get("domain_tags") or []),
        "topics": list(metadata.get("topics") or []),
        "identifiers": dict(metadata.get("identifiers") or {}),
        "is_oa": metadata.get("is_oa"),
        "oa_status": metadata.get("oa_status"),
        "oa_status_source": metadata.get("oa_status_source"),
        "source": (discovered_via[0] if discovered_via else None),
        "file_path": primary.filename if primary else None,
        # -- artifacts -------------------------------------------------------
        "artifacts": {kind: record.to_dict() for kind, record in sorted(artifacts.items())},
        # -- provenance ------------------------------------------------------
        "provenance": {
            "discovered_via": discovered_via,
            "cross_checked_via": cross_checked_via,
            "acquired_via": primary.source.value if primary else None,
            "original_url": primary.original_url if primary else None,
            "resolved_url": primary.resolved_url if primary else None,
            "retrieved_at": primary.retrieved_at if primary else None,
            "http_status": primary.http_status if primary else None,
            # Candidate URLs come from providers and may carry signed tokens, so the
            # operator-visible copy is redacted. The raw URL stays in internal state
            # only, where re-acquisition needs it (MASTER_SPEC section 19: provenance
            # must not contain secrets).
            "candidate_urls": [
                {
                    "url": redact_url(c["url"]) if c.get("url") else None,
                    "kind": c.get("kind"),
                    "source": c.get("source"),
                }
                for c in (metadata.get("candidates") or [])
            ],
            "cross_checks": cross_checks,
            "oa_status_source": metadata.get("oa_status_source"),
        },
        # -- harvest state ---------------------------------------------------
        "harvest": {
            "run_id": inputs.run_id,
            "status": inputs.status,
            "attempts": inputs.attempts,
            "written_at": utc_now_iso(),
            "harvester_version": __version__,
        },
    }


def write_sidecar(storage_root: Path, document_id: str, sidecar: dict[str, Any]) -> Path:
    """Write ``<document_id>.json`` atomically.

    The sidecar is mandatory for a successfully ingested document, so it is written
    the same way as an artifact: temporary file, fsync, atomic rename.
    """
    path = artifact_path(storage_root, document_id, "json")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = to_pretty_json(sidecar)
    handle, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".part", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
        raise StorageError(f"could not write sidecar {path.name}: {exc}") from exc
    return path


def read_sidecar(storage_root: Path, document_id: str) -> dict[str, Any] | None:
    path = artifact_path(storage_root, document_id, "json")
    if not path.exists():
        return None
    try:
        return parse_json(path.read_text(encoding="utf-8"), None)
    except OSError as exc:  # pragma: no cover - unreadable file
        raise StorageError(f"could not read sidecar {path.name}: {exc}") from exc


@dataclass(slots=True)
class ArtifactCheck:
    document_id: str
    kind: str
    present: bool
    sha256_matches: bool | None
    valid: bool | None
    problem: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "kind": self.kind,
            "present": self.present,
            "sha256_matches": self.sha256_matches,
            "valid": self.valid,
            "problem": self.problem,
        }


def check_artifact_file(
    storage_root: Path,
    document_id: str,
    record: ArtifactRecord,
    *,
    deep: bool,
    min_pdf_size_bytes: int,
    min_xml_size_bytes: int,
) -> ArtifactCheck:
    """Compare one recorded artifact against the file on disk."""
    from .validation import validate_pdf, validate_xml  # local import: avoids a cycle

    path = artifact_path(storage_root, document_id, record.kind.value)
    if not path.exists():
        return ArtifactCheck(
            document_id=document_id,
            kind=record.kind.value,
            present=False,
            sha256_matches=None,
            valid=None,
            problem="file recorded in state is missing from the corpus",
        )

    actual = sha256_file(path)
    matches = actual == record.sha256
    problem = None if matches else "sha256 mismatch between state and file"

    valid: bool | None = None
    if deep:
        try:
            if record.kind is ArtifactKind.PDF:
                validate_pdf(path, min_size_bytes=min_pdf_size_bytes)
            else:
                validate_xml(path, min_size_bytes=min_xml_size_bytes)
            valid = True
        except Exception as exc:  # validation errors are the finding, not a crash
            valid = False
            problem = f"{problem + '; ' if problem else ''}{exc}"

    return ArtifactCheck(
        document_id=document_id,
        kind=record.kind.value,
        present=True,
        sha256_matches=matches,
        valid=valid,
        problem=problem,
    )


def list_corpus_documents(storage_root: Path) -> list[str]:
    """Document IDs visible in the corpus, inferred from the mandatory sidecars."""
    root = Path(storage_root)
    if not root.exists():
        return []
    return sorted(path.stem for path in root.glob("*.json") if path.is_file())


def orphan_artifacts(storage_root: Path, known_document_ids: set[str]) -> list[str]:
    """Artifact files in the corpus that no state record accounts for."""
    root = Path(storage_root)
    if not root.exists():
        return []
    orphans: list[str] = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".pdf", ".xml", ".json"):
            continue
        if path.stem not in known_document_ids:
            orphans.append(path.name)
    return orphans
