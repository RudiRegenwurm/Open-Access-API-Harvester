"""Europe PMC adapter — secondary identity/metadata cross-check and XML/full-text source.

SPEC_PATCH section 4 replaces the vague "verification via Europe PMC" with explicit
cross-check semantics: match stable identifiers, map PMCID/PMID where supplied, gather
full-text availability evidence, and *preserve* provider disagreement instead of
normalising it away. Provenance therefore says ``cross_checked_via``, never
``verified_via``.

Verified endpoints (``docs/providers.md`` section 2):

* ``GET {base}/search?query=DOI:"<doi>"&format=json&resultType=core``
* ``GET {base}/search?query=PMCID:<pmcid>&format=json&resultType=core``
* ``GET {base}/search?query=EXT_ID:<pmid> AND SRC:MED&format=json&resultType=core``
* ``GET {base}/{PMCID}/fullTextXML``

The DOI query is the primary lookup but is not dependable on its own: on 2026-08-19
it returned a bodiless ``{"version": …}`` envelope (or HTTP 404) for DOIs that had
resolved correctly hours earlier, while the PMCID and scoped-PMID queries answered
every time. Callers therefore fall back to an identifier lookup rather than concluding
that Europe PMC does not know the article.

Full-text locations are read from the ``fullTextUrlList`` object **embedded in the
``core`` search result**. The standalone ``/{source}/{id}/fullTextUrlList`` endpoint
that older documentation describes was probed live on 2026-08-16 and returns HTTP 404
for every identifier form, so it is not used.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..config import EuropePmcConfig
from ..errors import ProviderError
from ..http import ProviderClient, redact_url
from ..identity import normalize_doi
from ..models import ArtifactKind, CrossCheck, FulltextCandidate, Source, SourceRecord
from ..util import coerce_int, utc_now_iso

LOGGER = logging.getLogger("harvester.providers.europe_pmc")

_PMCID_RE = re.compile(r"^PMC\d+$", re.IGNORECASE)
_PMID_RE = re.compile(r"^\d+$")

#: ``availabilityCode`` values Europe PMC uses for legitimately open content.
#: Anything else is left alone — the harvester never works around access control.
_OPEN_AVAILABILITY = frozenset({"OA", "F"})


class EuropePmcAdapter:
    source = Source.EUROPE_PMC

    def __init__(self, client: ProviderClient, config: EuropePmcConfig) -> None:
        self._client = client
        self._config = config

    @property
    def _base(self) -> str:
        return self._config.base_url.rstrip("/")

    # ------------------------------------------------------------------- lookup

    def lookup_by_doi(self, canonical_doi: str) -> SourceRecord | None:
        """Find the Europe PMC record for a DOI, or ``None`` when it knows none."""
        url = f"{self._base}/search"
        params = {
            "query": f'DOI:"{canonical_doi}"',
            "format": "json",
            "resultType": "core",
            "pageSize": self._config.page_size,
        }
        response = self._client.request(
            "GET", url, params=params, operation="europe_pmc.search"
        )
        payload = _json_body(response, url)
        results = ((payload.get("resultList") or {}).get("result")) or []
        if not isinstance(results, list):
            raise ProviderError(
                "europe_pmc: malformed resultList", url=redact_url(url), retryable=False
            )

        for result in results:
            if not isinstance(result, dict):
                continue
            if normalize_doi(result.get("doi")) == canonical_doi:
                return self._normalize_result(result)
        return None

    def lookup_by_pmcid(self, pmcid: str) -> SourceRecord | None:
        if not _PMCID_RE.match(pmcid.strip()):
            return None
        return self._search_one(
            f"PMCID:{pmcid.strip().upper()}", operation="europe_pmc.search_pmcid"
        )

    def lookup_by_pmid(self, pmid: str) -> SourceRecord | None:
        """Find the Europe PMC record for a PubMed ID, or ``None``.

        ``SRC:MED`` scopes the search to the PubMed corpus the PMID belongs to. The
        bare ``EXT_ID:<pmid>`` form is ambiguous across corpora and was observed
        returning no result for records the scoped query resolves immediately
        (probed live 2026-08-19), so the scope is not optional.
        """
        value = (pmid or "").strip()
        if not _PMID_RE.match(value):
            return None
        return self._search_one(
            f"EXT_ID:{value} AND SRC:MED", operation="europe_pmc.search_pmid"
        )

    def _search_one(self, query: str, *, operation: str) -> SourceRecord | None:
        url = f"{self._base}/search"
        params = {"query": query, "format": "json", "resultType": "core", "pageSize": 1}
        response = self._client.request("GET", url, params=params, operation=operation)
        results = ((_json_body(response, url).get("resultList") or {}).get("result")) or []
        for result in results:
            if isinstance(result, dict):
                return self._normalize_result(result)
        return None

    def fulltext_xml_url(self, pmcid: str) -> str | None:
        """The ``fullTextXML`` URL for an OA full-text article, if the PMCID is usable."""
        value = (pmcid or "").strip().upper()
        if not _PMCID_RE.match(value):
            return None
        return f"{self._base}/{value}/fullTextXML"

    # -------------------------------------------------------------- cross-check

    def cross_check(
        self, canonical_doi: str | None, record: SourceRecord, reference: dict[str, Any]
    ) -> CrossCheck:
        """Compare Europe PMC's view against the canonical (OpenAlex-derived) view.

        Records what matched, what Europe PMC additionally knows (PMID/PMCID), what
        full-text evidence exists, and every field where the two providers disagree.
        No value is overwritten here; disagreement is evidence, not noise.
        """
        matched_on: list[str] = []
        if canonical_doi and record.doi == canonical_doi:
            matched_on.append("doi")
        for key in ("pmid", "pmcid"):
            reference_value = (reference.get("identifiers") or {}).get(key)
            record_value = record.identifiers.get(key)
            if reference_value and record_value and str(reference_value) == str(record_value):
                matched_on.append(key)

        differences: dict[str, dict[str, Any]] = {}
        for field_name in ("title", "publication_year", "journal"):
            reference_value = reference.get(field_name)
            record_value = getattr(record, field_name)
            if reference_value in (None, "") or record_value in (None, ""):
                continue
            if _normalise_for_comparison(reference_value) != _normalise_for_comparison(record_value):
                differences[field_name] = {
                    "openalex": reference_value,
                    "europe_pmc": record_value,
                }

        return CrossCheck(
            source=Source.EUROPE_PMC,
            matched_on=matched_on,
            identifiers=dict(record.identifiers),
            metadata_differences=differences,
            fulltext_availability={
                "is_open_access": record.is_oa,
                "in_epmc": record.extra.get("in_epmc"),
                "has_pdf": record.extra.get("has_pdf"),
                "has_fulltext_xml": record.extra.get("has_fulltext_xml"),
                "candidate_count": len(record.candidates),
            },
            checked_at=record.fetched_at,
        )

    # ------------------------------------------------------------ normalization

    def _normalize_result(self, result: dict[str, Any]) -> SourceRecord:
        article_id = str(result.get("id") or "").strip()
        article_source = str(result.get("source") or "").strip()
        pmcid = _clean(result.get("pmcid"))
        pmid = _clean(result.get("pmid"))
        doi = normalize_doi(result.get("doi"))

        identifiers: dict[str, str] = {}
        if doi:
            identifiers["doi"] = doi
        if pmid:
            identifiers["pmid"] = pmid
        if pmcid:
            identifiers["pmcid"] = pmcid.upper()
        if article_id and article_source:
            identifiers["europe_pmc"] = f"{article_source}/{article_id}"

        in_epmc = _yes_no(result.get("inEPMC"))
        has_pdf = _yes_no(result.get("hasPDF"))
        is_oa = _yes_no(result.get("isOpenAccess"))

        candidates = _candidates_from_url_list(result.get("fullTextUrlList"))

        # fullTextXML is served for the Open-Access full-text subset only, and
        # ``inEPMC`` is not that subset: a record can be in Europe PMC and free to
        # read while ``isOpenAccess`` is "N", in which case the endpoint answers 404
        # (observed for PMC11299760 and PMC11299761 on 2026-08-19). Asking anyway
        # produced a predictable NOT_FOUND per document, so the Open-Access flag is
        # now the condition rather than one of two alternatives.
        has_fulltext_xml = bool(pmcid) and is_oa is True
        if has_fulltext_xml:
            xml_url = self.fulltext_xml_url(pmcid or "")
            if xml_url:
                candidates.append(
                    FulltextCandidate(
                        url=xml_url,
                        kind=ArtifactKind.XML,
                        source=Source.EUROPE_PMC,
                        host_type="repository",
                    )
                )

        return SourceRecord(
            source=Source.EUROPE_PMC,
            source_id=identifiers.get("europe_pmc") or pmcid or pmid or article_id or "unknown",
            fetched_at=utc_now_iso(),
            doi=doi,
            title=_clean(result.get("title")),
            authors=_authors(result),
            publication_year=coerce_int(result.get("pubYear")),
            journal=_journal(result),
            abstract=_clean(result.get("abstractText")),
            is_oa=is_oa,
            oa_status=None,  # Europe PMC does not report an oa_status vocabulary.
            identifiers=identifiers,
            candidates=candidates,
            extra={
                "in_epmc": in_epmc,
                "has_pdf": has_pdf,
                "has_fulltext_xml": has_fulltext_xml,
                "article_source": article_source or None,
                "article_id": article_id or None,
                "is_open_access": is_oa,
                "license": _clean(result.get("license")),
            },
            raw_payload=dict(result),
        )


# ---------------------------------------------------------------------- helpers


def _json_body(response: Any, url: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError(
            f"europe_pmc: malformed JSON response: {exc}",
            url=redact_url(url),
            retryable=False,
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderError(
            "europe_pmc: expected a JSON object", url=redact_url(url), retryable=False
        )
    return payload


def _candidates_from_url_list(url_list: Any) -> list[FulltextCandidate]:
    """Turn a ``fullTextUrlList`` structure into candidates, PDFs first."""
    if not isinstance(url_list, dict):
        return []
    entries = url_list.get("fullTextUrl")
    if not isinstance(entries, list):
        return []

    pdfs: list[FulltextCandidate] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = _clean(entry.get("url"))
        if not url or url in seen:
            continue
        availability_code = str(entry.get("availabilityCode") or "").strip().upper()
        if availability_code and availability_code not in _OPEN_AVAILABILITY:
            # Not flagged open by the provider: left alone rather than worked around.
            continue
        if str(entry.get("documentStyle") or "").strip().lower() != "pdf":
            # Only direct PDF locations are acquisition candidates; HTML landing
            # pages would merely be rejected by validation.
            continue
        seen.add(url)
        pdfs.append(
            FulltextCandidate(
                url=url,
                kind=ArtifactKind.PDF,
                source=Source.EUROPE_PMC,
                host_type=_clean(entry.get("site")),
            )
        )
    return pdfs


def _authors(result: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    author_list = (result.get("authorList") or {}).get("author")
    if isinstance(author_list, list):
        for author in author_list:
            if not isinstance(author, dict):
                continue
            name = _clean(author.get("fullName")) or _clean(author.get("lastName"))
            if name and name not in authors:
                authors.append(name)
    if authors:
        return authors
    author_string = _clean(result.get("authorString"))
    if not author_string:
        return []
    for part in author_string.rstrip(".").split(","):
        name = part.strip()
        if name and name not in authors:
            authors.append(name)
    return authors


def _journal(result: dict[str, Any]) -> str | None:
    journal_info = result.get("journalInfo")
    if isinstance(journal_info, dict):
        journal = journal_info.get("journal")
        if isinstance(journal, dict):
            return _clean(journal.get("title"))
    return _clean(result.get("journalTitle"))


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _yes_no(value: Any) -> bool | None:
    """Europe PMC uses ``"Y"``/``"N"``. Anything else stays unknown, not False."""
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    if text == "Y":
        return True
    if text == "N":
        return False
    return None


def _normalise_for_comparison(value: Any) -> str:
    return " ".join(str(value).split()).strip().casefold().rstrip(".")
