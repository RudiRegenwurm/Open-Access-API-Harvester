"""Discovery preview — Assisted Search V1 (SPEC_ASSISTED_SEARCH_V1 sections 22-28).

A preview answers one question: *is this query searching the scientific neighbourhood
I meant?* It therefore runs the **real** discovery path — the same
:class:`~harvester.providers.openalex.OpenAlexAdapter`, the same
:class:`~harvester.providers.openalex.OpenAlexQuery`, the same normalization — and
then stops.

Nothing here writes. The module never imports the state store, the acquirer or the
storage layer, so a preview cannot create a run, a corpus document, a seen marker, an
artifact or a retry-state change even by accident. The existing ``--dry-run`` harvest
is a different thing: it is a real, recorded run that populates state, which is
exactly what a preview must not do.

The fingerprint here is what makes an approval expire. It covers the effective query
and every structured discovery filter, so any change the user makes to either
invalidates the previous preview.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field, replace
from typing import Any

from .config import Config
from .http import ClientPool
from .models import LogicalDocument, Source
from .providers.openalex import OpenAlexAdapter, OpenAlexQuery
from .util import to_json

LOGGER = logging.getLogger("harvester.preview")

#: Hard ceiling on preview results (specification section 24). Not a suggestion: the
#: page size sent to the provider is clamped to it too, so a preview never pulls a
#: large result set just to show ten rows.
PREVIEW_LIMIT = 10


@dataclass(slots=True)
class PreviewRecord:
    """One discovered work, in the fields the preview UI shows (section 25)."""

    title: str | None
    publication_year: int | None
    authors: list[str] = field(default_factory=list)
    doi: str | None = None
    journal: str | None = None
    oa_status: str | None = None
    is_oa: bool | None = None
    discovered_via: list[str] = field(default_factory=list)
    document_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "publication_year": self.publication_year,
            "authors": list(self.authors),
            "first_author": self.authors[0] if self.authors else None,
            "doi": self.doi,
            "journal": self.journal,
            "oa_status": self.oa_status,
            "is_oa": self.is_oa,
            "discovered_via": list(self.discovered_via),
            "document_id": self.document_id,
        }


@dataclass(slots=True)
class PreviewResult:
    """The outcome of one preview. Held in memory only; never persisted."""

    records: list[PreviewRecord]
    #: Provider-reported size of the whole result set, when it reports one.
    total_count: int | None
    limit: int
    fingerprint: str
    provider: str = Source.OPENALEX.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [record.to_dict() for record in self.records],
            "count": len(self.records),
            "total_count": self.total_count,
            "limit": self.limit,
            "fingerprint": self.fingerprint,
            "provider": self.provider,
        }


def query_fingerprint(query: OpenAlexQuery) -> str:
    """Stable identity of the discovery inputs (specification section 27).

    Derived from the query's own canonical dictionary, which carries the search text,
    every structured filter and the composed provider filter string. Two queries share
    a fingerprint exactly when they would send the same discovery request — so editing
    the query, a year bound, the OA setting, the topic or a DOI filter all produce a
    different fingerprint and therefore invalidate a previous preview approval.
    """
    return hashlib.sha256(to_json(query.to_dict()).encode("utf-8")).hexdigest()


def preview_discovery(
    config: Config,
    clients: ClientPool,
    query: OpenAlexQuery,
    *,
    limit: int = PREVIEW_LIMIT,
) -> PreviewResult:
    """Run discovery for *query* and return at most *limit* normalized records.

    Semantically side-effect-free: one provider read, no writes anywhere.
    """
    bounded = max(1, min(int(limit), PREVIEW_LIMIT))
    # Ask the provider for exactly as many records as the preview can show. The
    # adapter reads its page size from the config object it was given, so a narrowed
    # copy is all that is needed — the adapter itself stays untouched.
    openalex = replace(config.openalex, per_page=bounded)
    adapter = OpenAlexAdapter(clients.openalex, openalex)

    page = adapter.discover_page(query)
    records = [_record(document) for document, _ in page.records[:bounded]]
    LOGGER.info(
        "preview: %d record(s) shown of %s reported for filter %s",
        len(records),
        page.total_count if page.total_count is not None else "unknown",
        query.filter_string(),
    )
    return PreviewResult(
        records=records,
        total_count=page.total_count,
        limit=bounded,
        fingerprint=query_fingerprint(query),
    )


def _record(document: LogicalDocument) -> PreviewRecord:
    return PreviewRecord(
        title=document.title,
        publication_year=document.publication_year,
        authors=list(document.authors),
        doi=document.doi,
        journal=document.journal,
        oa_status=document.oa_status,
        is_oa=document.is_oa,
        discovered_via=list(document.discovered_via),
        document_id=document.document_id,
    )


__all__ = [
    "PREVIEW_LIMIT",
    "PreviewRecord",
    "PreviewResult",
    "preview_discovery",
    "query_fingerprint",
]
