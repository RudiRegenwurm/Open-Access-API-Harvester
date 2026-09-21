# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""Normalized domain model (MASTER_SPEC section 8).

Core business logic operates exclusively on these types. Provider-specific response
structures never leave the adapter modules (MASTER_SPEC sections 3.7 and 40).

Missing information is represented as ``None`` / empty, never invented
(MASTER_SPEC sections 11 and 44, SPEC_PATCH sections 5 and 6).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class Source(str, enum.Enum):
    OPENALEX = "openalex"
    EUROPE_PMC = "europe_pmc"
    UNPAYWALL = "unpaywall"


class ArtifactKind(str, enum.Enum):
    PDF = "pdf"
    XML = "xml"


class DocumentStatus(str, enum.Enum):
    """Logical-document state machine (MASTER_SPEC section 23)."""

    DISCOVERED = "DISCOVERED"
    NORMALIZED = "NORMALIZED"
    QUEUED = "QUEUED"
    ACQUIRING = "ACQUIRING"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    SKIPPED = "SKIPPED"


#: Permitted transitions. Impossible transitions are rejected (MASTER_SPEC section 23).
ALLOWED_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.DISCOVERED: frozenset(
        {DocumentStatus.NORMALIZED, DocumentStatus.SKIPPED, DocumentStatus.FAILED_PERMANENT}
    ),
    DocumentStatus.NORMALIZED: frozenset(
        {DocumentStatus.QUEUED, DocumentStatus.SKIPPED, DocumentStatus.FAILED_PERMANENT}
    ),
    DocumentStatus.QUEUED: frozenset(
        {DocumentStatus.ACQUIRING, DocumentStatus.SKIPPED, DocumentStatus.FAILED_PERMANENT}
    ),
    DocumentStatus.ACQUIRING: frozenset(
        {
            DocumentStatus.VALIDATING,
            DocumentStatus.COMPLETED,
            DocumentStatus.FAILED_RETRYABLE,
            DocumentStatus.FAILED_PERMANENT,
            DocumentStatus.SKIPPED,
            # A process killed mid-acquisition is reclaimed by the next run.
            DocumentStatus.QUEUED,
        }
    ),
    DocumentStatus.VALIDATING: frozenset(
        {
            DocumentStatus.COMPLETED,
            DocumentStatus.FAILED_RETRYABLE,
            DocumentStatus.FAILED_PERMANENT,
            DocumentStatus.QUEUED,
        }
    ),
    # Reconciliation may demote a COMPLETED document whose file vanished
    # (MASTER_SPEC section 48).
    DocumentStatus.COMPLETED: frozenset({DocumentStatus.QUEUED, DocumentStatus.FAILED_RETRYABLE}),
    DocumentStatus.FAILED_RETRYABLE: frozenset(
        {DocumentStatus.QUEUED, DocumentStatus.FAILED_PERMANENT, DocumentStatus.SKIPPED}
    ),
    DocumentStatus.FAILED_PERMANENT: frozenset({DocumentStatus.QUEUED}),
    DocumentStatus.SKIPPED: frozenset({DocumentStatus.QUEUED}),
}

#: States a document may occupy and still be picked up for acquisition work.
PENDING_STATUSES = frozenset(
    {
        DocumentStatus.DISCOVERED,
        DocumentStatus.NORMALIZED,
        DocumentStatus.QUEUED,
        DocumentStatus.ACQUIRING,
        DocumentStatus.VALIDATING,
        DocumentStatus.FAILED_RETRYABLE,
    }
)


def transition_allowed(current: DocumentStatus, target: DocumentStatus) -> bool:
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


class RunStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    SUSPENDED = "SUSPENDED"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"


@dataclass(slots=True, frozen=True)
class FulltextCandidate:
    """A candidate acquisition location supplied by a provider."""

    url: str
    kind: ArtifactKind
    source: Source
    #: "publisher" | "repository" | ... as reported by the provider; never inferred.
    host_type: str | None = None
    #: "publishedVersion" | "acceptedVersion" | "submittedVersion" | None.
    version: str | None = None
    license: str | None = None
    landing_page_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind.value,
            "source": self.source.value,
            "host_type": self.host_type,
            "version": self.version,
            "license": self.license,
            "landing_page_url": self.landing_page_url,
        }


@dataclass(slots=True)
class SourceRecord:
    """One provider's representation of a work (MASTER_SPEC section 8.2)."""

    source: Source
    source_id: str
    fetched_at: str
    doi: str | None = None
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    publication_year: int | None = None
    journal: str | None = None
    abstract: str | None = None
    is_oa: bool | None = None
    oa_status: str | None = None
    domain_tags: list[str] = field(default_factory=list)
    topics: list[dict[str, Any]] = field(default_factory=list)
    identifiers: dict[str, str] = field(default_factory=dict)
    candidates: list[FulltextCandidate] = field(default_factory=list)
    landing_page_urls: list[str] = field(default_factory=list)
    #: Provider-specific evidence retained verbatim for provenance/cross-checking.
    extra: dict[str, Any] = field(default_factory=dict)
    #: Native provider record before normalization. It is intentionally excluded from
    #: ``to_dict`` so legacy current-state projections and operator-facing responses do
    #: not suddenly duplicate large payloads. Evidence Ledger writes persist it in the
    #: append-only ``source_observations.raw_json`` column instead.
    raw_payload: dict[str, Any] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "source_id": self.source_id,
            "fetched_at": self.fetched_at,
            "doi": self.doi,
            "title": self.title,
            "authors": list(self.authors),
            "publication_year": self.publication_year,
            "journal": self.journal,
            "abstract": self.abstract,
            "is_oa": self.is_oa,
            "oa_status": self.oa_status,
            "domain_tags": list(self.domain_tags),
            "topics": list(self.topics),
            "identifiers": dict(self.identifiers),
            "candidates": [c.to_dict() for c in self.candidates],
            "landing_page_urls": list(self.landing_page_urls),
            "extra": dict(self.extra),
        }


@dataclass(slots=True)
class ArtifactRecord:
    """A validated local artifact (MASTER_SPEC sections 8.3 and 18)."""

    kind: ArtifactKind
    filename: str
    sha256: str
    size_bytes: int
    retrieved_at: str
    source: Source
    original_url: str
    resolved_url: str
    http_status: int
    content_type: str | None = None
    md5: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "kind": self.kind.value,
            "filename": self.filename,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "retrieved_at": self.retrieved_at,
            "source": self.source.value,
            "original_url": self.original_url,
            "resolved_url": self.resolved_url,
            "http_status": self.http_status,
            "content_type": self.content_type,
        }
        if self.md5 is not None:
            data["md5"] = self.md5
        return data


@dataclass(slots=True)
class CrossCheck:
    """Europe PMC cross-check evidence (SPEC_PATCH section 4).

    This records identifier agreement and metadata disagreement. It is explicitly not
    a claim that Europe PMC semantically certifies the record.
    """

    source: Source
    matched_on: list[str] = field(default_factory=list)
    identifiers: dict[str, str] = field(default_factory=dict)
    metadata_differences: dict[str, dict[str, Any]] = field(default_factory=dict)
    fulltext_availability: dict[str, Any] = field(default_factory=dict)
    checked_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "matched_on": list(self.matched_on),
            "identifiers": dict(self.identifiers),
            "metadata_differences": dict(self.metadata_differences),
            "fulltext_availability": dict(self.fulltext_availability),
            "checked_at": self.checked_at,
        }


@dataclass(slots=True)
class LogicalDocument:
    """A scholarly work, independent of any individual artifact.

    Bibliographic metadata, artifact metadata, provenance and harvest state are kept
    in distinct groups (MASTER_SPEC section 20).
    """

    document_id: str
    doi: str | None = None
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    publication_year: int | None = None
    journal: str | None = None
    abstract: str | None = None
    #: Provider-reported OA status plus its source (SPEC_PATCH section 6).
    oa_status: str | None = None
    oa_status_source: str | None = None
    is_oa: bool | None = None
    domain_tags: list[str] = field(default_factory=list)
    topics: list[dict[str, Any]] = field(default_factory=list)
    identifiers: dict[str, str] = field(default_factory=dict)

    status: DocumentStatus = DocumentStatus.DISCOVERED
    source_records: list[SourceRecord] = field(default_factory=list)
    candidates: list[FulltextCandidate] = field(default_factory=list)
    artifacts: dict[str, ArtifactRecord] = field(default_factory=dict)
    cross_checks: list[CrossCheck] = field(default_factory=list)
    discovered_via: list[str] = field(default_factory=list)
    attempts: int = 0

    @property
    def sources(self) -> list[str]:
        return sorted({record.source.value for record in self.source_records})
