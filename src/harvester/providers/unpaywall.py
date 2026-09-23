# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""Unpaywall adapter — DOI-based fallback OA-location resolver.

Verified behavior (``docs/providers.md`` section 3): ``GET {base}/{doi}?email=<contact>``.
No API key; the email parameter is mandatory under the current API terms. A ``404``
means Unpaywall does not know the DOI — a structured "not found", not a harvester
failure.

Unpaywall is a fallback resolver, never a discovery engine (MASTER_SPEC section 43).
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import UnpaywallConfig
from ..errors import ConfigurationError, NotFoundError, ProviderError
from ..http import ProviderClient, redact_url
from ..identity import normalize_doi
from ..models import ArtifactKind, FulltextCandidate, Source, SourceRecord
from ..util import coerce_int, utc_now_iso

LOGGER = logging.getLogger("harvester.providers.unpaywall")

#: Deterministic ranking keys. Lower sorts first.
_HOST_TYPE_RANK = {"publisher": 0, "repository": 1}
_VERSION_RANK = {"publishedversion": 0, "acceptedversion": 1, "submittedversion": 2}


class UnpaywallAdapter:
    source = Source.UNPAYWALL

    def __init__(
        self, client: ProviderClient, config: UnpaywallConfig, *, contact_email: str | None
    ) -> None:
        self._client = client
        self._config = config
        self._email = contact_email

    def resolve(self, canonical_doi: str) -> SourceRecord | None:
        """Return Unpaywall's OA record for *canonical_doi*, or ``None`` if unknown."""
        if not self._email:
            raise ConfigurationError(
                "Unpaywall requires a contact email; set HARVESTER_CONTACT_EMAIL "
                "or disable the provider"
            )
        url = f"{self._config.base_url.rstrip('/')}/{canonical_doi}"
        try:
            response = self._client.request(
                "GET", url, params={"email": self._email}, operation="unpaywall.resolve"
            )
        except NotFoundError:
            return None
        payload = _json_body(response, url)
        if payload.get("error"):
            message = str(payload.get("message") or payload.get("error"))
            if "not found" in message.lower():
                return None
            raise ProviderError(
                f"unpaywall: {message}", url=redact_url(url), retryable=False
            )
        return self._normalize(payload, canonical_doi)

    # ------------------------------------------------------------ normalization

    def _normalize(self, payload: dict[str, Any], canonical_doi: str) -> SourceRecord:
        doi = normalize_doi(payload.get("doi")) or canonical_doi
        locations = _ranked_locations(payload)
        candidates: list[FulltextCandidate] = []
        landing_pages: list[str] = []
        seen: set[str] = set()

        for location in locations:
            landing = _clean(location.get("url_for_landing_page")) or _clean(location.get("url"))
            if landing and landing not in landing_pages:
                landing_pages.append(landing)
            pdf_url = _clean(location.get("url_for_pdf"))
            if not pdf_url or pdf_url in seen:
                continue
            seen.add(pdf_url)
            candidates.append(
                FulltextCandidate(
                    url=pdf_url,
                    kind=ArtifactKind.PDF,
                    source=Source.UNPAYWALL,
                    host_type=_clean(location.get("host_type")),
                    version=_clean(location.get("version")),
                    license=_clean(location.get("license")),
                    landing_page_url=landing,
                )
            )

        is_oa = payload.get("is_oa")
        return SourceRecord(
            source=Source.UNPAYWALL,
            source_id=doi,
            fetched_at=utc_now_iso(),
            doi=doi,
            title=_clean(payload.get("title")),
            authors=_authors(payload),
            publication_year=coerce_int(payload.get("year")),
            journal=_clean(payload.get("journal_name")),
            abstract=None,  # Unpaywall does not supply abstracts. Never invented.
            is_oa=is_oa if isinstance(is_oa, bool) else None,
            oa_status=_clean(payload.get("oa_status")),
            identifiers={"doi": doi},
            candidates=candidates,
            landing_page_urls=landing_pages,
            extra={
                "genre": _clean(payload.get("genre")),
                "publisher": _clean(payload.get("publisher")),
                "has_repository_copy": payload.get("has_repository_copy"),
                "oa_location_count": len(locations),
            },
            raw_payload=dict(payload),
        )


def _ranked_locations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Order OA locations deterministically (MASTER_SPEC section 13).

    ``best_oa_location`` — Unpaywall's own choice — first, then the remaining
    locations by host type, then version, then URL for a total order.
    """
    locations: list[dict[str, Any]] = []
    best = payload.get("best_oa_location")
    if isinstance(best, dict):
        locations.append(best)

    rest = [
        location
        for location in (payload.get("oa_locations") or [])
        if isinstance(location, dict) and location is not best
    ]
    rest.sort(key=_location_sort_key)

    seen = {(_clean(best.get("url_for_pdf")), _clean(best.get("url"))) if isinstance(best, dict) else None}
    for location in rest:
        key = (_clean(location.get("url_for_pdf")), _clean(location.get("url")))
        if key in seen:
            continue
        seen.add(key)
        locations.append(location)
    return locations


def _location_sort_key(location: dict[str, Any]) -> tuple[int, int, str]:
    host_type = str(location.get("host_type") or "").strip().lower()
    version = str(location.get("version") or "").strip().lower()
    return (
        _HOST_TYPE_RANK.get(host_type, len(_HOST_TYPE_RANK)),
        _VERSION_RANK.get(version, len(_VERSION_RANK)),
        str(location.get("url_for_pdf") or location.get("url") or ""),
    )


def _authors(payload: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    for author in payload.get("z_authors") or []:
        if not isinstance(author, dict):
            continue
        given = _clean(author.get("given"))
        family = _clean(author.get("family"))
        name = " ".join(part for part in (given, family) if part)
        if name and name not in authors:
            authors.append(name)
    return authors


def _json_body(response: Any, url: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError(
            f"unpaywall: malformed JSON response: {exc}", url=redact_url(url), retryable=False
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderError(
            "unpaywall: expected a JSON object", url=redact_url(url), retryable=False
        )
    return payload


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None
