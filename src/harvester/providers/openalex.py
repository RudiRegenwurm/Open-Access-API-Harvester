# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""OpenAlex adapter — primary discovery.

Verified provider behavior is recorded in ``docs/providers.md`` section 1. Summary of
what this module relies on:

* ``GET {base}/works`` with a comma-joined ``filter`` parameter;
* Topic filters ``topics.id`` / ``primary_topic.id`` — Concepts are deprecated and
  are not used (SPEC_PATCH section 1);
* cursor paging via ``cursor=*`` and ``meta.next_cursor``, ``per-page`` 1..200;
* authentication via the ``api_key`` query parameter (required for production since
  2026-02-13). The retired polite-pool ``mailto`` parameter is never sent.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from ..config import OpenAlexConfig
from ..errors import ConfigurationError, InvalidMetadataError, ProviderError
from ..http import ProviderClient, redact_url
from ..identity import document_id_for_doi, document_id_for_source, normalize_doi
from ..models import (
    ArtifactKind,
    FulltextCandidate,
    LogicalDocument,
    Source,
    SourceRecord,
)
from ..util import coerce_int, utc_now_iso
from ..vocabulary import normalize_countries, normalize_languages

LOGGER = logging.getLogger("harvester.providers.openalex")

#: Documented work-language filter. Verified against the live API on 2026-08-18:
#: ``language:de`` returns works whose ``language`` field is ``de``.
LANGUAGE_FILTER = "language"

#: Country of the authors' *institutional* affiliations. Chosen over the newer
#: ``authorships.countries`` deliberately: this key is backed by matched institution
#: records, whereas ``authorships.countries`` also carries countries inferred from raw
#: affiliation strings — an inference this project does not want to inherit.
#: ``institutions.country_code`` is the documented alias of the same filter.
AFFILIATION_COUNTRY_FILTER = "authorships.institutions.country_code"

#: Fields requested from OpenAlex. Restricting the projection keeps responses small
#: and makes the adapter's dependency on the provider schema explicit.
WORK_FIELDS = (
    "id,doi,title,display_name,publication_year,authorships,primary_location,"
    "best_oa_location,locations,open_access,abstract_inverted_index,primary_topic,"
    "topics,ids,type,language"
)


@dataclass(slots=True)
class OpenAlexQuery:
    """A discovery query. Preserved verbatim in run state for provenance."""

    topic_id: str | None = None
    #: When true the topic must be the work's *primary* topic.
    primary_topic_only: bool = False
    search: str | None = None
    from_publication_year: int | None = None
    to_publication_year: int | None = None
    publication_year: int | None = None
    is_oa: bool = True
    has_doi: bool = True
    oa_status: str | None = None
    #: Publication languages, ISO 639-1. Empty means *any language* — no filter at all,
    #: which is what every query did before this constraint existed.
    languages: list[str] = field(default_factory=list)
    #: Countries of the authors' institutional affiliations, ISO 3166-1 alpha-2. Empty
    #: means *any country*. This is deliberately not the country a study was conducted
    #: in, nor the publisher's country, nor the geographic subject of the research.
    affiliation_countries: list[str] = field(default_factory=list)
    extra_filters: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Canonicalise the multi-value constraints however the query was built.

        Validation and sorting belong to the model, not to its callers: the CLI, the
        web API and a query restored from run state all end up with the same codes in
        the same order, so the filter string, the preview fingerprint and the run
        record are identical for the same selection (MASTER_SPEC section 45).
        An unrecognised code raises here rather than silently narrowing a harvest.
        """
        self.languages = normalize_languages(self.languages)
        self.affiliation_countries = normalize_countries(self.affiliation_countries)

    def filter_string(self) -> str:
        """Build the deterministic ``filter=`` value.

        Values within one filter are OR-ed with ``|`` and separate filters are AND-ed
        with ``,`` — the provider's documented semantics, verified against the live API
        (``docs/providers.md`` section 1.9).
        """
        filters: list[str] = []
        if self.topic_id:
            key = "primary_topic.id" if self.primary_topic_only else "topics.id"
            filters.append(f"{key}:{normalize_topic_id(self.topic_id)}")
        if self.is_oa:
            filters.append("is_oa:true")
        if self.has_doi:
            filters.append("has_doi:true")
        if self.oa_status:
            filters.append(f"open_access.oa_status:{self.oa_status}")
        if self.languages:
            filters.append(f"{LANGUAGE_FILTER}:{'|'.join(self.languages)}")
        if self.affiliation_countries:
            filters.append(f"{AFFILIATION_COUNTRY_FILTER}:{'|'.join(self.affiliation_countries)}")
        if self.publication_year is not None:
            filters.append(f"publication_year:{self.publication_year}")
        else:
            if self.from_publication_year is not None:
                filters.append(f"from_publication_date:{self.from_publication_year}-01-01")
            if self.to_publication_year is not None:
                filters.append(f"to_publication_date:{self.to_publication_year}-12-31")
        filters.extend(self.extra_filters)
        if not filters:
            raise ConfigurationError(
                "a discovery query needs at least one filter (for example --topic-id)"
            )
        return ",".join(sorted(filters))

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": normalize_topic_id(self.topic_id) if self.topic_id else None,
            "primary_topic_only": self.primary_topic_only,
            "search": self.search,
            "publication_year": self.publication_year,
            "from_publication_year": self.from_publication_year,
            "to_publication_year": self.to_publication_year,
            "is_oa": self.is_oa,
            "has_doi": self.has_doi,
            "oa_status": self.oa_status,
            "languages": list(self.languages),
            "affiliation_countries": list(self.affiliation_countries),
            "extra_filters": list(self.extra_filters),
            "filter": self.filter_string(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OpenAlexQuery:
        return cls(
            topic_id=data.get("topic_id"),
            primary_topic_only=bool(data.get("primary_topic_only", False)),
            search=data.get("search"),
            publication_year=data.get("publication_year"),
            from_publication_year=data.get("from_publication_year"),
            to_publication_year=data.get("to_publication_year"),
            is_oa=bool(data.get("is_oa", True)),
            has_doi=bool(data.get("has_doi", True)),
            oa_status=data.get("oa_status"),
            # Absent in every query recorded before these constraints existed, and
            # absent means "any" — so an old run resumes with exactly its old scope.
            languages=normalize_languages(data.get("languages")),
            affiliation_countries=normalize_countries(data.get("affiliation_countries")),
            extra_filters=list(data.get("extra_filters") or []),
        )


def normalize_topic_id(topic_id: str) -> str:
    """Accept ``T10159`` or ``https://openalex.org/T10159``; emit the short form."""
    value = str(topic_id).strip().rstrip("/")
    if "/" in value:
        value = value.rsplit("/", 1)[-1]
    value = value.upper()
    if not value.startswith("T") or not value[1:].isdigit():
        raise ConfigurationError(
            f"invalid OpenAlex Topic ID {topic_id!r}: expected a form such as T10159. "
            "Deprecated Concept IDs (C...) are not supported in V1."
        )
    return value


@dataclass(slots=True)
class DiscoveryPage:
    records: list[tuple[LogicalDocument, SourceRecord]]
    next_cursor: str | None
    total_count: int | None
    raw_count: int
    http_status: int


class OpenAlexAdapter:
    """Primary discovery adapter."""

    source = Source.OPENALEX

    def __init__(self, client: ProviderClient, config: OpenAlexConfig) -> None:
        self._client = client
        self._config = config

    # ------------------------------------------------------------------ discovery

    def discover_page(self, query: OpenAlexQuery, cursor: str = "*") -> DiscoveryPage:
        """Fetch one page of works."""
        params: dict[str, Any] = {
            "filter": query.filter_string(),
            "per-page": self._config.per_page,
            "cursor": cursor,
            "select": WORK_FIELDS,
        }
        if query.search:
            params["search"] = query.search
        if self._config.api_key:
            params["api_key"] = self._config.api_key

        url = f"{self._config.base_url.rstrip('/')}/works"
        response = self._client.request("GET", url, params=params, operation="openalex.discover")
        payload = _json_body(response, url)

        results = payload.get("results")
        if not isinstance(results, list):
            raise ProviderError(
                "openalex: response has no 'results' list", url=redact_url(url), retryable=False
            )
        meta = payload.get("meta") or {}
        records: list[tuple[LogicalDocument, SourceRecord]] = []
        for work in results:
            if not isinstance(work, dict):
                continue
            try:
                records.append(self.normalize_work(work))
            except InvalidMetadataError as exc:
                # Never silently discarded: reported to the orchestrator's failure sink.
                LOGGER.warning("openalex: skipping unusable record: %s", exc)
                continue
        return DiscoveryPage(
            records=records,
            next_cursor=meta.get("next_cursor") or None,
            total_count=coerce_int(meta.get("count")),
            raw_count=len(results),
            http_status=response.status_code,
        )

    def iter_discovery(
        self, query: OpenAlexQuery, *, cursor: str = "*", max_pages: int | None = None
    ) -> Iterator[DiscoveryPage]:
        """Yield discovery pages until the cursor is exhausted."""
        pages = 0
        current = cursor
        while True:
            page = self.discover_page(query, cursor=current)
            yield page
            pages += 1
            if not page.next_cursor or page.raw_count == 0:
                return
            if max_pages is not None and pages >= max_pages:
                return
            if page.next_cursor == current:
                LOGGER.warning("openalex: cursor did not advance; stopping to avoid a loop")
                return
            current = page.next_cursor

    def fetch_work_by_doi(self, canonical_doi: str) -> tuple[LogicalDocument, SourceRecord] | None:
        """Singleton lookup used by the ``inspect`` command."""
        params: dict[str, Any] = {"select": WORK_FIELDS}
        if self._config.api_key:
            params["api_key"] = self._config.api_key
        url = f"{self._config.base_url.rstrip('/')}/works/doi:{canonical_doi}"
        from ..errors import NotFoundError

        try:
            response = self._client.request("GET", url, params=params, operation="openalex.get")
        except NotFoundError:
            return None
        return self.normalize_work(_json_body(response, url))

    # ---------------------------------------------------------------- normalization

    def normalize_work(self, work: dict[str, Any]) -> tuple[LogicalDocument, SourceRecord]:
        """Turn one OpenAlex work into the normalized model.

        Every value comes from the payload. Nothing is inferred, defaulted to a
        plausible-looking value, or summarised (MASTER_SPEC section 11).
        """
        openalex_id = _short_id(work.get("id"))
        if not openalex_id:
            raise InvalidMetadataError("openalex: work has no id")

        doi = normalize_doi(work.get("doi"))
        title = _clean_text(work.get("title") or work.get("display_name"))
        authors = _authors(work)
        year = coerce_int(work.get("publication_year"))
        journal = _journal(work)
        abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
        open_access = _as_dict(work.get("open_access"))
        is_oa = open_access.get("is_oa")
        oa_status = open_access.get("oa_status")
        topics = _topics(work)
        domain_tags = _domain_tags(topics)
        identifiers = _identifiers(work, openalex_id, doi)
        candidates, landing_pages = _locations(work)

        document_id = (
            document_id_for_doi(doi)
            if doi
            else document_id_for_source(Source.OPENALEX.value, openalex_id)
        )
        fetched_at = utc_now_iso()

        source_record = SourceRecord(
            source=Source.OPENALEX,
            source_id=openalex_id,
            fetched_at=fetched_at,
            doi=doi,
            title=title,
            authors=authors,
            publication_year=year,
            journal=journal,
            abstract=abstract,
            is_oa=is_oa if isinstance(is_oa, bool) else None,
            oa_status=oa_status if isinstance(oa_status, str) else None,
            domain_tags=domain_tags,
            topics=topics,
            identifiers=identifiers,
            candidates=candidates,
            landing_page_urls=landing_pages,
            extra={
                "type": work.get("type"),
                "language": work.get("language"),
                "oa_url": (open_access.get("oa_url") or None),
                "abstract_available": work.get("abstract_inverted_index") is not None,
            },
            raw_payload=dict(work),
        )

        document = LogicalDocument(
            document_id=document_id,
            doi=doi,
            title=title,
            authors=authors,
            publication_year=year,
            journal=journal,
            abstract=abstract,
            oa_status=source_record.oa_status,
            oa_status_source=Source.OPENALEX.value if source_record.oa_status else None,
            is_oa=source_record.is_oa,
            domain_tags=domain_tags,
            topics=topics,
            identifiers=identifiers,
            candidates=list(candidates),
            discovered_via=[Source.OPENALEX.value],
            source_records=[source_record],
        )
        return document, source_record


# ---------------------------------------------------------------------- helpers


def reconstruct_abstract(inverted_index: Any) -> str | None:
    """Rebuild an abstract from OpenAlex's ``abstract_inverted_index``.

    SPEC_PATCH section 5: reconstruct deterministically when the data exist, and
    return ``None`` — never invented or summarised text — when they do not.
    """
    if not isinstance(inverted_index, dict) or not inverted_index:
        return None
    positions: dict[int, str] = {}
    for token, indices in inverted_index.items():
        if not isinstance(token, str) or not isinstance(indices, list):
            continue
        for index in indices:
            position = coerce_int(index)
            if position is None or position < 0:
                continue
            # A duplicated position would make the result order-dependent; keep the
            # lexicographically smaller token so the output stays deterministic.
            existing = positions.get(position)
            if existing is None or token < existing:
                positions[position] = token
    if not positions:
        return None
    text = " ".join(positions[key] for key in sorted(positions))
    text = " ".join(text.split())
    return text or None


def _json_body(response: Any, url: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError(
            f"openalex: malformed JSON response: {exc}", url=redact_url(url), retryable=False
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderError(
            "openalex: expected a JSON object", url=redact_url(url), retryable=False
        )
    return payload


def _short_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().rstrip("/").rsplit("/", 1)[-1]


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _as_list(value: Any) -> list[Any]:
    """Untrusted payload fields declared as arrays may arrive as anything."""
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _authors(work: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    for authorship in _as_list(work.get("authorships")):
        if not isinstance(authorship, dict):
            continue
        author = _as_dict(authorship.get("author"))
        name = _clean_text(author.get("display_name"))
        if name and name not in authors:
            authors.append(name)
    return authors


def _journal(work: dict[str, Any]) -> str | None:
    source = _as_dict(_as_dict(work.get("primary_location")).get("source"))
    return _clean_text(source.get("display_name"))


def _topics(work: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the Topic hierarchy exactly as supplied (SPEC_PATCH section 1)."""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_topics: list[Any] = []
    primary = work.get("primary_topic")
    if isinstance(primary, dict):
        raw_topics.append((primary, True))
    for topic in _as_list(work.get("topics")):
        if isinstance(topic, dict):
            raw_topics.append((topic, False))

    for topic, is_primary in raw_topics:
        topic_id = _short_id(topic.get("id"))
        if not topic_id or topic_id in seen:
            continue
        seen.add(topic_id)
        entry: dict[str, Any] = {
            "id": topic_id,
            "display_name": _clean_text(topic.get("display_name")),
            "is_primary": is_primary,
        }
        for level in ("subfield", "field", "domain"):
            node = topic.get(level)
            if isinstance(node, dict):
                entry[level] = {
                    "id": _short_id(node.get("id")),
                    "display_name": _clean_text(node.get("display_name")),
                }
        entries.append(entry)
    return entries


def _domain_tags(topics: list[dict[str, Any]]) -> list[str]:
    """Source-derived hierarchy tags: domain -> field -> subfield -> topic.

    Ordered outermost-first and de-duplicated while preserving first appearance, so
    the same payload always yields the same list (MASTER_SPEC section 45).
    """
    tags: list[str] = []
    for level in ("domain", "field", "subfield"):
        for topic in topics:
            node = topic.get(level)
            if isinstance(node, dict):
                name = node.get("display_name")
                if name and name not in tags:
                    tags.append(name)
    for topic in topics:
        name = topic.get("display_name")
        if name and name not in tags:
            tags.append(name)
    return tags


def _identifiers(work: dict[str, Any], openalex_id: str, doi: str | None) -> dict[str, str]:
    identifiers: dict[str, str] = {"openalex": openalex_id}
    if doi:
        identifiers["doi"] = doi
    ids = _as_dict(work.get("ids"))
    for key in ("pmid", "pmcid", "mag"):
        value = ids.get(key)
        if isinstance(value, str) and value.strip():
            identifiers[key] = value.strip().rstrip("/").rsplit("/", 1)[-1]
    return identifiers


def _locations(work: dict[str, Any]) -> tuple[list[FulltextCandidate], list[str]]:
    """Collect OA acquisition candidates in a deterministic priority order.

    Order: ``best_oa_location`` first (OpenAlex's own choice), then
    ``primary_location``, then the remaining ``locations`` in payload order.
    Only locations flagged OA by the provider are used — no access control is
    circumvented (MASTER_SPEC section 59).
    """
    candidates: list[FulltextCandidate] = []
    landing_pages: list[str] = []
    seen: set[tuple[str, str]] = set()

    ordered: list[dict[str, Any]] = []
    for key in ("best_oa_location", "primary_location"):
        location = work.get(key)
        if isinstance(location, dict):
            ordered.append(location)
    for location in _as_list(work.get("locations")):
        if isinstance(location, dict):
            ordered.append(location)

    open_access = _as_dict(work.get("open_access"))
    oa_url = open_access.get("oa_url")

    for location in ordered:
        if location.get("is_oa") is False:
            continue
        landing = location.get("landing_page_url")
        if isinstance(landing, str) and landing and landing not in landing_pages:
            landing_pages.append(landing)
        pdf_url = location.get("pdf_url")
        if not isinstance(pdf_url, str) or not pdf_url.strip():
            continue
        key = (pdf_url, ArtifactKind.PDF.value)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            FulltextCandidate(
                url=pdf_url.strip(),
                kind=ArtifactKind.PDF,
                source=Source.OPENALEX,
                host_type=_string_or_none(_as_dict(location.get("source")).get("type"))
                or _string_or_none(location.get("host_type")),
                version=_string_or_none(location.get("version")),
                license=_string_or_none(location.get("license")),
                landing_page_url=_string_or_none(landing),
            )
        )

    if isinstance(oa_url, str) and oa_url.strip():
        key = (oa_url.strip(), ArtifactKind.PDF.value)
        if key not in seen:
            seen.add(key)
            candidates.append(
                FulltextCandidate(
                    url=oa_url.strip(), kind=ArtifactKind.PDF, source=Source.OPENALEX
                )
            )
    return candidates, landing_pages


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
