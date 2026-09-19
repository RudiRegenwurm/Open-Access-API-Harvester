"""Deterministic mock provider infrastructure (MASTER_SPEC section 51).

The whole automated suite runs against these mocks: no test depends on a live
provider. The mocks reproduce normal responses, pagination, rate limiting, daily-budget
exhaustion, timeouts, malformed responses, missing full text, alternative locations and
duplicate records.

Hostnames deliberately use the reserved ``.invalid`` TLD so nothing can accidentally
leave the machine and DNS never has to answer.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from pypdf import PdfWriter

OPENALEX_BASE = "https://api.openalex.invalid"
EPMC_BASE = "https://epmc.invalid/rest"
UNPAYWALL_BASE = "https://api.unpaywall.invalid/v2"
FILES_BASE = "https://files.invalid"
#: The query advisor. Same reserved TLD: no advisor test can leave the machine.
ADVISOR_BASE = "https://api.anthropic.invalid"

#: What the mock advisor recommends unless a test says otherwise. Deliberately the
#: kind of compact query the specification asks for, not an echo of the question.
DEFAULT_ADVICE = {
    "recommended_query": "ADHD adult remission recurrence longitudinal trajectory",
    "rationale": "Focuses on adult ADHD and longitudinal remission/recurrence while "
    "avoiding broad contextual terms in the initial discovery.",
    "deferred_terms": ["environmental demands"],
}


# --------------------------------------------------------------------- artifacts


def make_pow_challenge_bytes() -> bytes:
    """The proof-of-work page NCBI PMC serves instead of a PDF, in its observed shape.

    Reproduced from a live response on 2026-08-19 and reduced to what identifies it:
    HTTP 200, an HTML body, and the script block that hands a puzzle to the browser.
    The harvester must recognise this well enough to report it — and no further. It
    is a test fixture for detection, never a target to be solved.
    """
    return (
        b"\n\n<html>\n  <head>\n"
        b'    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        b"    <title>Preparing to download ...</title>\n"
        b"  </head>\n  <body>\n    <div id=\"main\">\n"
        b"      <h1>Preparing to download ...</h1>\n"
        b"    </div>\n  </body>\n"
        b"  <script type=\"module\">\n"
        b'    const POW_CHALLENGE = "VwR3BQpkZwplAQLhZGp4AmL2ZlV:U7HgR_Fl6"\n'
        b'    const POW_DIFFICULTY = "4"\n'
        b'    const POW_COOKIE_NAME = "cloudpmc-viewer-pow"\n'
        b"    window.ncbi.pmc.pow.init(POW_CHALLENGE, POW_DIFFICULTY, POW_COOKIE_NAME)\n"
        b"  </script>\n</html>\n"
    )


def make_pdf_bytes(*, pages: int = 2, padding: int = 2048) -> bytes:
    """A structurally valid PDF that pypdf can open.

    Size is reached with a long metadata value rather than trailing junk, so the
    xref table stays intact and the file is a genuinely valid PDF.
    """
    writer = PdfWriter()
    for _ in range(max(1, pages)):
        writer.add_blank_page(width=612, height=792)
    metadata = {"/Title": "Mock Open Access Article", "/Producer": "harvester-tests"}
    if padding > 0:
        metadata["/Subject"] = "mock " * (padding // 5)
    writer.add_metadata(metadata)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_truncated_pdf_bytes() -> bytes:
    """A PDF cut off mid-file: correct magic bytes, no %%EOF trailer."""
    full = make_pdf_bytes()
    cut = full[: len(full) // 2]
    assert b"%%EOF" not in cut, "fixture must not contain a trailer marker"
    return cut


def make_corrupt_pdf_bytes() -> bytes:
    """Correct magic bytes and a trailer, but a destroyed body."""
    return b"%PDF-1.4\n" + b"\x00\xff" * 900 + b"\ntrailer<<>>\n%%EOF\n"


def make_html_bytes(title: str = "Access denied") -> bytes:
    return (
        f"<!DOCTYPE html>\n<html><head><title>{title}</title></head>"
        f"<body><h1>{title}</h1><p>This is not a PDF.</p></body></html>"
    ).encode("utf-8")


def make_xml_bytes(pmcid: str = "PMC1000001") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<article><front><article-meta>"
        f"<article-id pub-id-type='pmcid'>{pmcid}</article-id>"
        "<title-group><article-title>Mock Open Access Article</article-title></title-group>"
        "</article-meta></front><body><sec><p>Mock full text body.</p></sec></body></article>"
    ).encode("utf-8")


def make_malformed_xml_bytes() -> bytes:
    return b'<?xml version="1.0"?>\n<article><body><p>unclosed'


def make_xxe_xml_bytes() -> bytes:
    """XML with an external entity — must be rejected by the safe parser."""
    return (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE article [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
        b"<article><body>&xxe;</body></article>"
    )


# ------------------------------------------------------------------ record builders


def openalex_work(
    index: int,
    *,
    doi: str | None = None,
    topic_id: str = "T10159",
    pdf_url: str | None = None,
    is_oa: bool = True,
    oa_status: str = "gold",
    abstract: bool = True,
    pmcid: str | None = None,
    pmid: str | None = None,
    language: str | None = "en",
    institution_countries: tuple[str, ...] = ("US",),
) -> dict[str, Any]:
    """One OpenAlex work payload in the shape the live API returns."""
    work_id = f"W{2000000 + index}"
    doi_value = doi if doi is not None else f"10.1234/mock.{index:04d}"
    url = pdf_url if pdf_url is not None else f"{FILES_BASE}/{work_id}.pdf"

    payload: dict[str, Any] = {
        "id": f"https://openalex.org/{work_id}",
        "doi": f"https://doi.org/{doi_value}" if doi_value else None,
        "title": f"Mock Open Access Article {index}",
        "display_name": f"Mock Open Access Article {index}",
        "publication_year": 2020 + (index % 5),
        "type": "article",
        # ``language`` may be None: OpenAlex genuinely reports works whose language it
        # does not know, and unknown must stay unknown rather than be read as English.
        "language": language,
        "authorships": [
            {
                "author": {"id": "https://openalex.org/A1", "display_name": "Ada Lovelace"},
                "institutions": [
                    {
                        "id": f"https://openalex.org/I{i}",
                        "display_name": f"Institute {country}",
                        "country_code": country,
                    }
                    for i, country in enumerate(institution_countries, start=1)
                ],
            },
            {"author": {"id": "https://openalex.org/A2", "display_name": "Alan Turing"}},
        ],
        "primary_location": {
            "is_oa": is_oa,
            "landing_page_url": f"{FILES_BASE}/landing/{work_id}",
            "pdf_url": url,
            "version": "publishedVersion",
            "license": "cc-by",
            "source": {"display_name": "Journal of Mock Studies", "type": "journal"},
        },
        "best_oa_location": {
            "is_oa": is_oa,
            "landing_page_url": f"{FILES_BASE}/landing/{work_id}",
            "pdf_url": url,
            "version": "publishedVersion",
            "license": "cc-by",
            "source": {"display_name": "Journal of Mock Studies", "type": "journal"},
        },
        "locations": [],
        "open_access": {"is_oa": is_oa, "oa_status": oa_status, "oa_url": url},
        "primary_topic": {
            "id": f"https://openalex.org/{topic_id}",
            "display_name": "Moral Psychology",
            "subfield": {
                "id": "https://openalex.org/subfields/3204",
                "display_name": "Developmental and Educational Psychology",
            },
            "field": {"id": "https://openalex.org/fields/32", "display_name": "Psychology"},
            "domain": {"id": "https://openalex.org/domains/2", "display_name": "Social Sciences"},
        },
        "topics": [
            {
                "id": f"https://openalex.org/{topic_id}",
                "display_name": "Moral Psychology",
                "subfield": {
                    "id": "https://openalex.org/subfields/3204",
                    "display_name": "Developmental and Educational Psychology",
                },
                "field": {"id": "https://openalex.org/fields/32", "display_name": "Psychology"},
                "domain": {
                    "id": "https://openalex.org/domains/2",
                    "display_name": "Social Sciences",
                },
            }
        ],
        "ids": {"openalex": f"https://openalex.org/{work_id}"},
    }
    if doi_value:
        payload["ids"]["doi"] = f"https://doi.org/{doi_value}"
    if pmcid:
        payload["ids"]["pmcid"] = pmcid
    if pmid:
        payload["ids"]["pmid"] = pmid
    if abstract:
        payload["abstract_inverted_index"] = {
            "Moral": [0],
            "psychology": [1],
            "studies": [2],
            "human": [3],
            "judgement": [4],
        }
    else:
        payload["abstract_inverted_index"] = None
    return payload


def epmc_result(
    *,
    doi: str,
    pmcid: str = "PMC1000001",
    pmid: str = "31000001",
    title: str | None = None,
    year: int = 2021,
    journal: str = "Journal of Mock Studies",
    is_oa: bool = True,
    pdf_url: str | None = None,
    in_epmc: bool | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": pmcid,
        "source": "PMC",
        "pmid": pmid,
        "pmcid": pmcid,
        "doi": doi,
        "title": title or "Mock Open Access Article",
        "authorString": "Lovelace A, Turing A.",
        "journalInfo": {"journal": {"title": journal}},
        "pubYear": str(year),
        "isOpenAccess": "Y" if is_oa else "N",
        # Independent of isOpenAccess on purpose: Europe PMC really does hold records
        # that are in EPMC and free to read while outside the Open-Access subset, and
        # for those ``fullTextXML`` answers 404. Tying the two together here made that
        # combination — the one that matters — impossible to express.
        "inEPMC": "Y" if (is_oa if in_epmc is None else in_epmc) else "N",
        "hasPDF": "Y" if pdf_url else "N",
        "abstractText": "Mock abstract supplied by Europe PMC.",
    }
    if pdf_url:
        result["fullTextUrlList"] = {
            "fullTextUrl": [
                {
                    "availability": "Open access",
                    "availabilityCode": "OA",
                    "documentStyle": "pdf",
                    "site": "Europe_PMC",
                    "url": pdf_url,
                }
            ]
        }
    return result


def unpaywall_record(
    *, doi: str, pdf_url: str | None, oa_status: str = "green", is_oa: bool = True
) -> dict[str, Any]:
    best = (
        {
            "url": pdf_url,
            "url_for_pdf": pdf_url,
            "url_for_landing_page": f"{FILES_BASE}/landing/unpaywall",
            "host_type": "repository",
            "version": "publishedVersion",
            "license": "cc-by",
        }
        if pdf_url
        else None
    )
    return {
        "doi": doi,
        "is_oa": is_oa,
        "oa_status": oa_status,
        "title": "Mock Open Access Article",
        "year": 2021,
        "journal_name": "Journal of Mock Studies",
        "genre": "journal-article",
        "publisher": "Mock Publishing",
        "best_oa_location": best,
        "oa_locations": [best] if best else [],
        "z_authors": [{"given": "Ada", "family": "Lovelace"}],
    }


# ---------------------------------------------------------------------- the server


@dataclass
class Behavior:
    """A scripted response for one request to a route.

    A bare ``Behavior()`` is a *pass-through*: it consumes one slot in the script but
    serves the route's normal response. That is how a script expresses "succeed once,
    then fail".
    """

    status: int | None = None
    json: Any = None
    content: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)
    #: When set, raise this exception instead of responding (timeouts, resets).
    raise_exc: Callable[[], Exception] | None = None

    @property
    def passthrough(self) -> bool:
        return self.status is None and self.json is None and self.content is None and self.raise_exc is None


class MockProviders:
    """A deterministic in-process stand-in for all three providers plus file hosting.

    ``script`` maps a route key to a list of :class:`Behavior` objects consumed in
    order; once exhausted the route falls back to its default behavior. That is how
    failure-injection tests express "fail twice, then succeed".
    """

    def __init__(
        self,
        *,
        works: list[dict[str, Any]] | None = None,
        page_size: int = 25,
        epmc_by_doi: dict[str, dict[str, Any]] | None = None,
        unpaywall_by_doi: dict[str, dict[str, Any]] | None = None,
        files: dict[str, bytes] | None = None,
        file_headers: dict[str, dict[str, str]] | None = None,
        epmc_doi_search_blind: bool = False,
    ) -> None:
        self.works = list(works or [])
        self.page_size = page_size
        self.epmc_by_doi = dict(epmc_by_doi or {})
        #: Reproduces an observed Europe PMC failure mode: a DOI query answers with a
        #: bodiless ``{"version": …}`` envelope — no ``resultList``, no error — for
        #: articles the same service returns immediately for a PMCID or PMID. Identifier
        #: lookups keep working while the DOI lookup goes blind.
        self.epmc_doi_search_blind = epmc_doi_search_blind
        self.unpaywall_by_doi = dict(unpaywall_by_doi or {})
        self.files = dict(files or {})
        self.file_headers = dict(file_headers or {})
        self.script: dict[str, list[Behavior]] = {}
        self.requests: list[httpx.Request] = []
        self.request_counts: dict[str, int] = {}
        #: Set to a positive number to make OpenAlex report a finite credit budget.
        self.openalex_credit_limit: int | None = None
        self.openalex_credits_used = 0
        #: What the mock query advisor answers. Set to a plain object to change the
        #: advice, or use ``script_route("advisor.messages", ...)`` for failures.
        self.advisor_advice: dict[str, Any] = dict(DEFAULT_ADVICE)
        #: Every advisor request body, decoded — so a test can assert what the
        #: advisor was actually told without reaching into the client.
        self.advisor_requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    # -- scripting -----------------------------------------------------------

    def script_route(self, route: str, behaviors: list[Behavior]) -> None:
        self.script[route] = list(behaviors)

    def _next_scripted(self, route: str) -> Behavior | None:
        with self._lock:
            queue = self.script.get(route)
            if not queue:
                return None
            return queue.pop(0)

    def count(self, route: str) -> int:
        with self._lock:
            return self.request_counts.get(route, 0)

    # -- transport -----------------------------------------------------------

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
        route = self._route_for(request)
        with self._lock:
            self.request_counts[route] = self.request_counts.get(route, 0) + 1

        scripted = self._next_scripted(route)
        if scripted is not None and not scripted.passthrough:
            if scripted.raise_exc is not None:
                raise scripted.raise_exc()
            status = scripted.status if scripted.status is not None else 200
            if scripted.json is not None:
                return httpx.Response(
                    status, json=scripted.json, headers=scripted.headers, request=request
                )
            return httpx.Response(
                status,
                content=scripted.content if scripted.content is not None else b"",
                headers=scripted.headers,
                request=request,
            )
        return self._default(route, request)

    def _route_for(self, request: httpx.Request) -> str:
        url = request.url
        path = url.path
        host = url.host
        if "openalex" in host:
            if path.startswith("/works/"):
                return "openalex.work"
            return "openalex.works"
        if "unpaywall" in host:
            return "unpaywall.doi"
        if "anthropic" in host:
            return "advisor.messages"
        if "epmc" in host:
            if path.endswith("/fullTextXML"):
                return "epmc.fulltextxml"
            if path.endswith("/fullTextUrlList"):
                return "epmc.fulltexturls"
            return "epmc.search"
        if "files" in host:
            return f"file:{path}"
        return f"other:{host}{path}"

    def _default(self, route: str, request: httpx.Request) -> httpx.Response:
        if route == "openalex.works":
            return self._openalex_works(request)
        if route == "openalex.work":
            return self._openalex_work(request)
        if route == "epmc.search":
            return self._epmc_search(request)
        if route == "epmc.fulltexturls":
            return self._epmc_fulltext_urls(request)
        if route == "epmc.fulltextxml":
            return httpx.Response(
                200,
                content=make_xml_bytes(),
                headers={"content-type": "application/xml"},
                request=request,
            )
        if route == "unpaywall.doi":
            return self._unpaywall(request)
        if route == "advisor.messages":
            return self._advisor(request)
        if route.startswith("file:"):
            return self._file(route[len("file:") :], request)
        return httpx.Response(404, json={"error": "not found"}, request=request)

    # -- provider defaults ----------------------------------------------------

    def _advisor(self, request: httpx.Request) -> httpx.Response:
        """A Messages-API-shaped reply carrying schema-valid query advice."""
        import json as _json

        try:
            body = _json.loads(request.content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            body = {}
        with self._lock:
            self.advisor_requests.append(body)
        return httpx.Response(
            200,
            json={
                "id": "msg_mock",
                "type": "message",
                "role": "assistant",
                "model": body.get("model", "mock-model"),
                "stop_reason": "end_turn",
                "content": [
                    {"type": "text", "text": _json.dumps(self.advisor_advice, ensure_ascii=False)}
                ],
                "usage": {"input_tokens": 100, "output_tokens": 40},
            },
            request=request,
        )

    def _openalex_headers(self, cost: int) -> dict[str, str]:
        if self.openalex_credit_limit is None:
            return {}
        with self._lock:
            self.openalex_credits_used += cost
            used = self.openalex_credits_used
        remaining = max(0, self.openalex_credit_limit - used)
        return {
            "X-RateLimit-Limit": str(self.openalex_credit_limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Credits-Used": str(cost),
            "X-RateLimit-Reset": "3600",
        }

    @staticmethod
    def _matches(work: dict[str, Any], filter_value: str) -> bool:
        """Apply the language and affiliation-country filters like the real API does.

        Only these two keys are enforced; the mock leaves the pre-existing filters to
        the fixtures, which choose their works directly. Values are OR-ed with ``|``
        and the keys are AND-ed, and matching is case-insensitive — the behaviour
        verified against the live API and recorded in ``docs/providers.md`` 1.9.
        """
        for clause in filter_value.split(","):
            key, _, raw = clause.partition(":")
            wanted = {v.strip().lower() for v in raw.split("|") if v.strip()}
            if not wanted:
                continue
            if key == "language":
                language = work.get("language")
                # An unknown language matches no selection: it is never guessed at.
                if not isinstance(language, str) or language.lower() not in wanted:
                    return False
            elif key in ("authorships.institutions.country_code", "institutions.country_code"):
                present = {
                    str(institution.get("country_code", "")).lower()
                    for authorship in work.get("authorships") or []
                    for institution in authorship.get("institutions") or []
                    if institution.get("country_code")
                }
                if not present & wanted:
                    return False
        return True

    def _openalex_works(self, request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor", "*")
        # The mock caps at its own page_size, like a real server enforcing a maximum,
        # so a test can force multi-page discovery regardless of the client's request.
        per_page = min(int(request.url.params.get("per-page", self.page_size)), self.page_size)
        # The language and affiliation-country filters are applied for real, with the
        # provider's OR-within/AND-across semantics. A test that asks for German work
        # therefore fails if the constraint never reaches the request — asserting on
        # the filter string alone would pass even then.
        matching = [w for w in self.works if self._matches(w, request.url.params.get("filter", ""))]
        start = 0 if cursor in ("*", "", None) else int(cursor)
        window = matching[start : start + per_page]
        next_start = start + len(window)
        next_cursor = str(next_start) if next_start < len(matching) else None
        return httpx.Response(
            200,
            json={
                "meta": {
                    "count": len(matching),
                    "per_page": per_page,
                    "next_cursor": next_cursor,
                },
                "results": window,
            },
            headers=self._openalex_headers(10),
            request=request,
        )

    def _openalex_work(self, request: httpx.Request) -> httpx.Response:
        wanted = request.url.path.rsplit("/", 1)[-1]
        if wanted.startswith("doi:"):
            wanted = wanted[4:]
        for work in self.works:
            doi = (work.get("doi") or "").rsplit("/", 1)[-1].lower()
            if doi and wanted.lower().endswith(doi):
                return httpx.Response(
                    200, json=work, headers=self._openalex_headers(1), request=request
                )
        return httpx.Response(
            404, json={"error": "Not found."}, headers=self._openalex_headers(1), request=request
        )

    def _epmc_search(self, request: httpx.Request) -> httpx.Response:
        """Answer a DOI, PMCID or scoped-PMID query from the same record set.

        Which identifier a query uses matters here, because the real service does not
        treat them alike: the DOI index has been seen going blind while the identifier
        indexes kept answering.
        """
        query = request.url.params.get("query", "")
        lowered = query.lower()
        results: list[dict[str, Any]] = []

        if lowered.startswith("pmcid:"):
            wanted = query.split(":", 1)[1].strip().upper()
            results = [
                r for _, r in sorted(self.epmc_by_doi.items())
                if str(r.get("pmcid") or "").upper() == wanted
            ]
        elif lowered.startswith("ext_id:"):
            # Only the SRC-scoped form resolves; the bare form is the one that was
            # observed returning nothing for records the scoped query finds.
            wanted = query.split(":", 1)[1].split()[0].strip()
            if "src:med" in lowered:
                results = [
                    r for _, r in sorted(self.epmc_by_doi.items())
                    if str(r.get("pmid") or "") == wanted
                ]
        else:
            if self.epmc_doi_search_blind:
                return httpx.Response(200, json={"version": "6.9"}, request=request)
            results = [
                r for doi, r in sorted(self.epmc_by_doi.items())
                if doi.lower() in lowered
            ]

        return httpx.Response(
            200,
            json={"hitCount": len(results), "resultList": {"result": results}},
            request=request,
        )

    def _epmc_fulltext_urls(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"fullTextUrlList": {"fullTextUrl": []}}, request=request)

    def _unpaywall(self, request: httpx.Request) -> httpx.Response:
        doi = request.url.path.split("/v2/", 1)[-1]
        record = self.unpaywall_by_doi.get(doi.lower())
        if record is None:
            return httpx.Response(
                404, json={"error": True, "message": "not found", "HTTP_status_code": 404},
                request=request,
            )
        return httpx.Response(200, json=record, request=request)

    def _file(self, path: str, request: httpx.Request) -> httpx.Response:
        content = self.files.get(path)
        if content is None:
            return httpx.Response(404, content=b"not found", request=request)
        headers = {"content-type": "application/pdf", "content-length": str(len(content))}
        headers.update(self.file_headers.get(path, {}))
        return httpx.Response(200, content=content, headers=headers, request=request)


def build_default_providers(count: int = 3, **kwargs: Any) -> MockProviders:
    """A small, fully successful corpus: N works, each with a downloadable PDF."""
    works = [openalex_work(index) for index in range(1, count + 1)]
    files = {f"/W{2000000 + index}.pdf": make_pdf_bytes() for index in range(1, count + 1)}
    return MockProviders(works=works, files=files, **kwargs)
