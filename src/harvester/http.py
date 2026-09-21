# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""HTTP transport: provider-aware rate limiting, retry classification, budget tracking.

MASTER_SPEC sections 26, 27, 28, 38, 39 and SPEC_PATCH sections 2 and 7.

The single most important distinction implemented here is that an HTTP 429 is *two*
operationally different conditions:

* transient throttling      -> configured retry/backoff (honouring ``Retry-After``)
* daily budget exhaustion   -> :class:`BudgetExhaustedError`, which the orchestrator
                               turns into a checkpointed, resumable suspension.

Neither 429 nor 409 is hard-coded as *the* budget signal; the decision is made from the
provider's rate-limit headers and response body. See ``docs/providers.md`` section 1.8.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .config import Config, ProviderConfig, RetryConfig
from .errors import (
    AuthenticationError,
    BudgetExhaustedError,
    HarvesterError,
    HttpError,
    NetworkError,
    NotFoundError,
    ProviderError,
    RateLimitedError,
    TimeoutError_,
)
from .util import coerce_int

LOGGER = logging.getLogger("harvester.http")

#: Query parameters whose values must never appear in logs, provenance or reports.
SECRET_QUERY_PARAMS = frozenset({"api_key", "apikey", "key", "token", "access_token", "email"})

#: HTTP statuses that justify another attempt (MASTER_SPEC section 26).
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Statuses that indicate the request itself is wrong and will stay wrong.
PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 405, 406, 410, 414, 422, 451})

_BUDGET_BODY_RE = re.compile(
    r"(daily|credit|quota|budget|allowance)\w*\s*(limit|exceeded|exhaust|out of|spent|reached)"
    r"|(exceeded|exhaust\w*|out of|no more|insufficient)\s*\w*\s*(daily|credit|quota|budget)",
    re.IGNORECASE,
)


def redact_url(url: str) -> str:
    """Return *url* with secret query-parameter values masked.

    MASTER_SPEC sections 19/29/37 and AC-017: credentials and contact addresses must
    not reach logs, provenance or run reports. Every URL that leaves this process for
    an operator-visible destination goes through here first.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable-url>"
    if not parts.query:
        return _strip_userinfo(parts)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    redacted = [
        (key, "REDACTED" if key.lower() in SECRET_QUERY_PARAMS and value else value)
        for key, value in pairs
    ]
    return _strip_userinfo(parts._replace(query=urlencode(redacted)))


def _strip_userinfo(parts: Any) -> str:
    netloc = parts.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    return urlunsplit(parts._replace(netloc=netloc))


class RateLimiter:
    """Thread-safe token bucket enforcing a sustained request rate."""

    def __init__(
        self,
        requests_per_second: float,
        *,
        burst: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be > 0")
        self.rate = requests_per_second
        self.capacity = float(burst if burst is not None else max(1.0, requests_per_second))
        self._tokens = self.capacity
        self._updated = monotonic()
        self._monotonic = monotonic
        self._sleep = sleeper
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        while True:
            with self._lock:
                now = self._monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self.rate
            self._sleep(max(wait, 0.001))


@dataclass(slots=True)
class BudgetState:
    """Latest provider budget information, from response headers."""

    limit: int | None = None
    remaining: int | None = None
    used_by_this_process: int = 0
    reset_in_seconds: float | None = None
    last_updated: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "used_by_this_process": self.used_by_this_process,
            "reset_in_seconds": self.reset_in_seconds,
            "last_updated": self.last_updated,
        }


class ProviderClient:
    """One rate-limited, retrying HTTP client per provider."""

    def __init__(
        self,
        name: str,
        provider_config: ProviderConfig,
        retry_config: RetryConfig,
        *,
        user_agent: str,
        max_redirects: int = 5,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        daily_credit_ceiling: int | None = None,
        credit_cost: Callable[[httpx.Response], int] | None = None,
    ) -> None:
        self.name = name
        self.config = provider_config
        self.retry = retry_config
        self.budget = BudgetState()
        self.daily_credit_ceiling = daily_credit_ceiling
        self._credit_cost = credit_cost
        self._sleep = sleeper
        self._rng = rng or random.Random()
        self._limiter = RateLimiter(provider_config.requests_per_second, sleeper=sleeper)
        self._semaphore = threading.BoundedSemaphore(provider_config.concurrency)
        self._budget_lock = threading.Lock()
        self._suspended: BudgetExhaustedError | None = None
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                provider_config.timeout_seconds, connect=provider_config.connect_timeout_seconds
            ),
            follow_redirects=True,
            max_redirects=max_redirects,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            transport=transport,
        )

    # ---------------------------------------------------------------- lifecycle

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ProviderClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ requests

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any | None = None,
        operation: str = "request",
        on_attempt: Callable[[int, HarvesterError | None, httpx.Response | None], None]
        | None = None,
    ) -> httpx.Response:
        """Perform a request with the configured retry policy.

        Raises a :class:`HarvesterError` subclass on failure; the category tells the
        caller whether the condition is transient, permanent, or a budget suspension.
        """
        last_error: HarvesterError | None = None
        for attempt in range(1, self.retry.max_attempts + 1):
            self._raise_if_suspended()
            try:
                response = self._single_request(
                    method, url, params=params, headers=headers, json=json
                )
            except HarvesterError as exc:
                last_error = exc
                if on_attempt is not None:
                    on_attempt(attempt, exc, None)
                if isinstance(exc, BudgetExhaustedError):
                    raise
                if not exc.retryable or attempt >= self.retry.max_attempts:
                    raise
                self._sleep(self._backoff_delay(attempt, exc))
                continue
            if on_attempt is not None:
                on_attempt(attempt, None, response)
            return response
        assert last_error is not None  # pragma: no cover - loop always sets it
        raise last_error

    def stream(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ):
        """Open a streaming response, rate-limited and concurrency-bounded.

        Retries are the caller's business here because a partially consumed stream
        cannot be replayed; :mod:`harvester.acquisition` retries at the download level.
        """
        return _StreamContext(self, method, url, params=params, headers=headers)

    def _single_request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        json: Any | None = None,
    ) -> httpx.Response:
        self._limiter.acquire()
        with self._semaphore:
            try:
                # ``json`` is only forwarded when a body was actually supplied, so the
                # GET requests every provider adapter makes stay byte-identical.
                extra: dict[str, Any] = {} if json is None else {"json": json}
                response = self._client.request(
                    method,
                    url,
                    params=dict(params or {}),
                    headers=dict(headers or {}),
                    **extra,
                )
            except httpx.TimeoutException as exc:
                raise TimeoutError_(
                    f"{self.name}: request timed out", url=redact_url(url)
                ) from exc
            except httpx.TooManyRedirects as exc:
                raise HttpError(
                    f"{self.name}: too many redirects", url=redact_url(url)
                ) from exc
            except httpx.HTTPError as exc:
                raise NetworkError(
                    f"{self.name}: {type(exc).__name__}: {exc}", url=redact_url(url)
                ) from exc
        self._observe_budget(response)
        self._raise_for_status(response)
        return response

    # -------------------------------------------------------------- classification

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        url = redact_url(str(response.request.url))
        snippet = _body_snippet(response)

        if status in (429, 409, 402, 403):
            exhausted = self._budget_exhaustion(response, snippet)
            if exhausted is not None:
                raise exhausted

        if status == 429:
            raise RateLimitedError(
                f"{self.name}: rate limited (HTTP 429)",
                retry_after=_retry_after_seconds(response, self.retry.max_retry_after_seconds),
                http_status=status,
                url=url,
            )
        if status in (401, 403):
            raise AuthenticationError(
                f"{self.name}: authentication/authorisation failed (HTTP {status})",
                http_status=status,
                url=url,
            )
        if status == 404:
            raise NotFoundError(
                f"{self.name}: not found (HTTP 404)", http_status=status, url=url
            )
        if status in PERMANENT_STATUSES:
            raise HttpError(
                f"{self.name}: HTTP {status}: {snippet}", http_status=status, url=url
            )
        if status in RETRYABLE_STATUSES or 500 <= status < 600:
            raise ProviderError(
                f"{self.name}: HTTP {status}: {snippet}",
                retryable=True,
                http_status=status,
                url=url,
            )
        raise HttpError(f"{self.name}: HTTP {status}: {snippet}", http_status=status, url=url)

    def _budget_exhaustion(
        self, response: httpx.Response, snippet: str
    ) -> BudgetExhaustedError | None:
        """Decide whether this error response means the daily budget is spent.

        Evidence, in order: an explicit ``X-RateLimit-Remaining: 0`` header, or a body
        naming credit/quota/budget exhaustion. A 429 without either is ordinary
        throttling; a 409 without either is a plain conflict.
        """
        remaining = coerce_int(response.headers.get("x-ratelimit-remaining"))
        body_says_budget = bool(_BUDGET_BODY_RE.search(snippet))
        if remaining is not None and remaining > 0 and not body_says_budget:
            return None
        if remaining is None and not body_says_budget:
            return None
        reset = _float_or_none(response.headers.get("x-ratelimit-reset"))
        error = BudgetExhaustedError(
            f"{self.name}: daily budget exhausted (HTTP {response.status_code}): {snippet}",
            provider=self.name,
            reset_in_seconds=reset,
            limit=coerce_int(response.headers.get("x-ratelimit-limit")),
            remaining=remaining,
            http_status=response.status_code,
            url=redact_url(str(response.request.url)),
        )
        self._mark_suspended(error)
        return error

    def _observe_budget(self, response: httpx.Response) -> None:
        """Track credit consumption and enforce the optional local safety ceiling."""
        headers = response.headers
        cost = None
        if self._credit_cost is not None:
            cost = self._credit_cost(response)
        if cost is None:
            cost = coerce_int(headers.get("x-ratelimit-credits-used")) or 0

        with self._budget_lock:
            self.budget.used_by_this_process += max(int(cost), 0)
            limit = coerce_int(headers.get("x-ratelimit-limit"))
            remaining = coerce_int(headers.get("x-ratelimit-remaining"))
            reset = _float_or_none(headers.get("x-ratelimit-reset"))
            if limit is not None:
                self.budget.limit = limit
            if remaining is not None:
                self.budget.remaining = remaining
            if reset is not None:
                self.budget.reset_in_seconds = reset
            used = self.budget.used_by_this_process
            ceiling = self.daily_credit_ceiling

        if ceiling is not None and used >= ceiling:
            self._mark_suspended(
                BudgetExhaustedError(
                    f"{self.name}: local daily credit ceiling reached "
                    f"({used}/{ceiling} credits used by this process)",
                    provider=self.name,
                    reset_in_seconds=self.budget.reset_in_seconds,
                    limit=ceiling,
                    remaining=0,
                    details={"local_ceiling": True},
                )
            )
        elif remaining is not None and remaining <= 0:
            self._mark_suspended(
                BudgetExhaustedError(
                    f"{self.name}: provider reports 0 remaining credits",
                    provider=self.name,
                    reset_in_seconds=self.budget.reset_in_seconds,
                    limit=self.budget.limit,
                    remaining=0,
                )
            )

    def _mark_suspended(self, error: BudgetExhaustedError) -> None:
        with self._budget_lock:
            if self._suspended is None:
                self._suspended = error

    def _raise_if_suspended(self) -> None:
        """Refuse further work once the budget is spent — no uncontrolled retry loop."""
        with self._budget_lock:
            suspended = self._suspended
        if suspended is not None:
            raise suspended

    @property
    def suspended_error(self) -> BudgetExhaustedError | None:
        with self._budget_lock:
            return self._suspended

    def sleep_before_retry(self, attempt: int, error: HarvesterError) -> float:
        """Wait the configured backoff before retrying *attempt*; returns the delay.

        Used by the download path, which cannot reuse :meth:`request`'s retry loop
        because a partially consumed stream is not replayable.
        """
        delay = self._backoff_delay(attempt, error)
        self._sleep(delay)
        return delay

    def _backoff_delay(self, attempt: int, error: HarvesterError) -> float:
        """Exponential backoff with jitter (MASTER_SPEC section 26)."""
        retry_after = getattr(error, "retry_after", None)
        if retry_after is not None:
            return min(float(retry_after), self.retry.max_retry_after_seconds)
        base = self.retry.backoff_initial_seconds * (self.retry.backoff_multiplier ** (attempt - 1))
        base = min(base, self.retry.backoff_max_seconds)
        if self.retry.jitter_ratio <= 0:
            return base
        spread = base * self.retry.jitter_ratio
        return max(0.0, base + self._rng.uniform(-spread, spread))


class _StreamContext:
    """Context manager pairing rate limiting/concurrency with a streaming response."""

    def __init__(
        self,
        client: ProviderClient,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
    ) -> None:
        self._client = client
        self._method = method
        self._url = url
        self._params = dict(params or {})
        self._headers = dict(headers or {})
        self._stream = None
        self._acquired = False

    def __enter__(self) -> httpx.Response:
        self._client._raise_if_suspended()
        self._client._limiter.acquire()
        self._client._semaphore.acquire()
        self._acquired = True
        self._stream = self._client._client.stream(
            self._method, self._url, params=self._params, headers=self._headers
        )
        try:
            response = self._stream.__enter__()
        except httpx.TimeoutException as exc:
            self._release()
            raise TimeoutError_(
                f"{self._client.name}: request timed out", url=redact_url(self._url)
            ) from exc
        except httpx.TooManyRedirects as exc:
            self._release()
            raise HttpError(
                f"{self._client.name}: too many redirects", url=redact_url(self._url)
            ) from exc
        except httpx.HTTPError as exc:
            self._release()
            raise NetworkError(
                f"{self._client.name}: {type(exc).__name__}: {exc}", url=redact_url(self._url)
            ) from exc
        try:
            self._client._observe_budget(response)
            if response.status_code >= 400:
                response.read()
                self._client._raise_for_status(response)
        except BaseException:
            self.__exit__(*(None, None, None))
            raise
        return response

    def __exit__(self, *exc: object) -> None:
        if self._stream is not None:
            try:
                self._stream.__exit__(*exc)  # type: ignore[arg-type]
            finally:
                self._stream = None
                self._release()
        else:
            self._release()

    def _release(self) -> None:
        if self._acquired:
            self._acquired = False
            self._client._semaphore.release()


def _retry_after_seconds(response: httpx.Response, cap: float) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    value = _float_or_none(raw)
    if value is None:
        # HTTP-date form. Parse conservatively; on failure fall back to normal backoff.
        try:
            from email.utils import parsedate_to_datetime

            target = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if target is None:
            return None
        import datetime as _dt

        now = _dt.datetime.now(_dt.timezone.utc)
        if target.tzinfo is None:
            target = target.replace(tzinfo=_dt.timezone.utc)
        value = (target - now).total_seconds()
    return max(0.0, min(value, cap))


def _float_or_none(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _body_snippet(response: httpx.Response, limit: int = 300) -> str:
    try:
        text = response.text
    except Exception:  # pragma: no cover - unread stream
        return ""
    return " ".join(text.split())[:limit]


class ClientPool:
    """Owns one :class:`ProviderClient` per provider plus the download client."""

    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        user_agent = config.effective_user_agent()
        common = {
            "user_agent": user_agent,
            "transport": transport,
            "sleeper": sleeper,
            "rng": rng,
            "max_redirects": config.downloads.max_redirects,
        }
        self.openalex = ProviderClient(
            "openalex",
            config.openalex,
            config.retry,
            daily_credit_ceiling=config.openalex.daily_credit_ceiling,
            credit_cost=_openalex_credit_cost,
            **common,
        )
        self.europe_pmc = ProviderClient(
            "europe_pmc", config.europe_pmc, config.retry, **common
        )
        self.unpaywall = ProviderClient("unpaywall", config.unpaywall, config.retry, **common)
        download_provider = ProviderConfig(
            enabled=True,
            concurrency=config.downloads.concurrency,
            requests_per_second=config.downloads.requests_per_second,
            timeout_seconds=config.downloads.timeout_seconds,
            connect_timeout_seconds=config.downloads.connect_timeout_seconds,
        )
        self.downloads = ProviderClient("downloads", download_provider, config.retry, **common)

    def all(self) -> tuple[ProviderClient, ...]:
        return (self.openalex, self.europe_pmc, self.unpaywall, self.downloads)

    def suspended_error(self) -> BudgetExhaustedError | None:
        for client in self.all():
            error = client.suspended_error
            if error is not None:
                return error
        return None

    def close(self) -> None:
        for client in self.all():
            client.close()

    def __enter__(self) -> ClientPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _openalex_credit_cost(response: httpx.Response) -> int | None:
    """Credit cost of an OpenAlex call.

    Uses the provider's own accounting when present, otherwise the documented
    schedule (list = 10 credits, singleton = 1). See ``docs/providers.md`` 1.7.
    """
    reported = coerce_int(response.headers.get("x-ratelimit-credits-used"))
    if reported is not None:
        return reported
    path = response.request.url.path.rstrip("/")
    segments = [segment for segment in path.split("/") if segment]
    return 1 if len(segments) >= 2 else 10

