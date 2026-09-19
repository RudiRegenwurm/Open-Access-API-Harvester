"""Unit tests: rate limiting, retry classification, backoff, budget detection.

Covers AC-008 (429 triggers configured retry), AC-009 (persistent 404 is permanent),
and the SPEC_PATCH section 7 amendment that a 429 is two distinct conditions.
"""

from __future__ import annotations

import random

import httpx
import pytest

from harvester.config import ProviderConfig, RetryConfig
from harvester.errors import (
    AuthenticationError,
    BudgetExhaustedError,
    HttpError,
    NetworkError,
    NotFoundError,
    ProviderError,
    RateLimitedError,
    TimeoutError_,
)
from harvester.http import ProviderClient, RateLimiter, redact_url


class Sleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_client(handler, *, max_attempts: int = 3, sleeper=None, **kwargs) -> ProviderClient:
    return ProviderClient(
        "testprovider",
        ProviderConfig(requests_per_second=1000, concurrency=4),
        RetryConfig(
            max_attempts=max_attempts,
            backoff_initial_seconds=2.0,
            backoff_multiplier=2.0,
            backoff_max_seconds=60.0,
            jitter_ratio=0.0,
        ),
        user_agent="test-agent",
        transport=httpx.MockTransport(handler),
        sleeper=sleeper or Sleeper(),
        rng=random.Random(1234),
        **kwargs,
    )


def responder(*responses):
    """Return a handler yielding the given responses in order, repeating the last."""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        status, payload, headers = item
        return httpx.Response(status, json=payload, headers=headers or {}, request=request)

    return handler


# ------------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    "url,expected_absent",
    [
        ("https://api.invalid/works?api_key=SECRET", "SECRET"),
        ("https://api.invalid/v2/10.1/x?email=me@example.org", "me@example.org"),
        ("https://api.invalid/x?token=abc123", "abc123"),
        ("https://user:pw@api.invalid/x", "pw"),
    ],
)
def test_redact_url_masks_secrets(url, expected_absent):
    assert expected_absent not in redact_url(url)


def test_redact_url_preserves_non_secret_parameters():
    redacted = redact_url("https://api.invalid/works?filter=topics.id:T1&api_key=SECRET")
    assert "topics.id" in redacted
    assert "SECRET" not in redacted


# ---------------------------------------------------------------- rate limiter


def test_rate_limiter_waits_once_the_burst_is_spent():
    now = [0.0]
    waits: list[float] = []

    def monotonic() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(2.0, burst=2, monotonic=monotonic, sleeper=sleep)
    limiter.acquire()
    limiter.acquire()
    assert waits == []
    limiter.acquire()
    assert waits and waits[0] == pytest.approx(0.5, abs=0.01)


def test_rate_limiter_rejects_nonsense_rates():
    with pytest.raises(ValueError):
        RateLimiter(0)


# ------------------------------------------------------------ classification


def test_success_returns_the_response():
    client = make_client(responder((200, {"ok": True}, None)))
    response = client.request("GET", "https://api.invalid/x")
    assert response.json() == {"ok": True}
    client.close()


def test_ac008_rate_limit_triggers_configured_retry_then_succeeds():
    """AC-008 / SPEC_PATCH section 7: a transient 429 retries with backoff."""
    sleeper = Sleeper()
    client = make_client(
        responder(
            (429, {"error": "slow down"}, {"X-RateLimit-Remaining": "500"}),
            (429, {"error": "slow down"}, {"X-RateLimit-Remaining": "400"}),
            (200, {"ok": True}, {"X-RateLimit-Remaining": "300"}),
        ),
        sleeper=sleeper,
    )
    response = client.request("GET", "https://api.invalid/x")
    assert response.status_code == 200
    assert sleeper.calls == [2.0, 4.0]  # exponential, jitter disabled for the test
    client.close()


def test_retry_budget_is_finite_and_then_raises():
    sleeper = Sleeper()
    client = make_client(
        responder((429, {"error": "slow"}, {"X-RateLimit-Remaining": "5"})),
        max_attempts=3,
        sleeper=sleeper,
    )
    with pytest.raises(RateLimitedError):
        client.request("GET", "https://api.invalid/x")
    assert len(sleeper.calls) == 2  # 3 attempts -> 2 sleeps, no endless loop
    client.close()


def test_retry_after_header_overrides_backoff():
    sleeper = Sleeper()
    client = make_client(
        responder(
            (429, {"e": 1}, {"Retry-After": "7", "X-RateLimit-Remaining": "5"}),
            (200, {"ok": True}, {"X-RateLimit-Remaining": "4"}),
        ),
        sleeper=sleeper,
    )
    client.request("GET", "https://api.invalid/x")
    assert sleeper.calls == [7.0]
    client.close()


def test_absurd_retry_after_is_capped():
    sleeper = Sleeper()
    client = make_client(
        responder(
            (429, {"e": 1}, {"Retry-After": "999999", "X-RateLimit-Remaining": "5"}),
            (200, {"ok": True}, None),
        ),
        sleeper=sleeper,
    )
    client.request("GET", "https://api.invalid/x")
    assert sleeper.calls == [300.0]
    client.close()


@pytest.mark.parametrize("status", [500, 502, 503, 504, 408])
def test_server_errors_are_retried(status):
    sleeper = Sleeper()
    client = make_client(
        responder((status, {"e": 1}, None), (200, {"ok": True}, None)), sleeper=sleeper
    )
    assert client.request("GET", "https://api.invalid/x").status_code == 200
    assert len(sleeper.calls) == 1
    client.close()


def test_ac009_persistent_404_is_permanent_and_not_retried():
    """AC-009: a persistent 404 is a structured failure, not an endless retry."""
    sleeper = Sleeper()
    client = make_client(responder((404, {"e": "gone"}, None)), sleeper=sleeper)
    with pytest.raises(NotFoundError) as excinfo:
        client.request("GET", "https://api.invalid/x")
    assert excinfo.value.retryable is False
    assert sleeper.calls == []  # never slept, never retried
    client.close()


@pytest.mark.parametrize("status", [400, 410, 422, 451])
def test_client_errors_are_permanent(status):
    sleeper = Sleeper()
    client = make_client(responder((status, {"e": 1}, None)), sleeper=sleeper)
    with pytest.raises(HttpError) as excinfo:
        client.request("GET", "https://api.invalid/x")
    assert excinfo.value.retryable is False
    assert sleeper.calls == []
    client.close()


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_are_permanent(status):
    client = make_client(responder((status, {"e": "denied"}, None)))
    with pytest.raises(AuthenticationError):
        client.request("GET", "https://api.invalid/x")
    client.close()


def test_timeouts_are_retried_then_surface():
    sleeper = Sleeper()
    client = make_client(
        responder(httpx.ConnectTimeout("timed out")), max_attempts=2, sleeper=sleeper
    )
    with pytest.raises(TimeoutError_):
        client.request("GET", "https://api.invalid/x")
    assert len(sleeper.calls) == 1
    client.close()


def test_connection_errors_are_retried():
    sleeper = Sleeper()
    client = make_client(
        responder(httpx.ConnectError("connection reset")), max_attempts=2, sleeper=sleeper
    )
    with pytest.raises(NetworkError):
        client.request("GET", "https://api.invalid/x")
    assert len(sleeper.calls) == 1
    client.close()


def test_backoff_is_capped_and_jittered():
    sleeper = Sleeper()
    client = ProviderClient(
        "p",
        ProviderConfig(requests_per_second=1000),
        RetryConfig(
            max_attempts=6,
            backoff_initial_seconds=2.0,
            backoff_multiplier=2.0,
            backoff_max_seconds=10.0,
            jitter_ratio=0.25,
        ),
        user_agent="test",
        transport=httpx.MockTransport(responder((503, {"e": 1}, None))),
        sleeper=sleeper,
        rng=random.Random(7),
    )
    with pytest.raises(ProviderError):
        client.request("GET", "https://api.invalid/x")
    assert len(sleeper.calls) == 5
    assert all(0 <= delay <= 12.5 for delay in sleeper.calls)
    assert sleeper.calls[-1] <= 12.5
    client.close()


# ------------------------------------------------- budget vs transient throttle


def test_budget_exhaustion_on_429_with_zero_remaining_suspends():
    """SPEC_PATCH section 2: exhaustion checkpoints instead of retrying."""
    sleeper = Sleeper()
    client = make_client(
        responder(
            (
                429,
                {"error": "daily credit limit exceeded"},
                {
                    "X-RateLimit-Limit": "100000",
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "3600",
                },
            )
        ),
        sleeper=sleeper,
    )
    with pytest.raises(BudgetExhaustedError) as excinfo:
        client.request("GET", "https://api.invalid/x")
    error = excinfo.value
    assert error.retryable is False
    assert error.reset_in_seconds == 3600
    assert error.limit == 100000
    assert sleeper.calls == []  # no uncontrolled retry loop
    client.close()


def test_budget_exhaustion_is_also_recognised_on_409():
    """The API-key announcement cites 409; neither status is hard-coded alone."""
    client = make_client(
        responder(
            (409, {"error": "You have exceeded your daily credit allowance"}, {}),
        )
    )
    with pytest.raises(BudgetExhaustedError):
        client.request("GET", "https://api.invalid/x")
    client.close()


def test_409_without_budget_evidence_is_an_ordinary_conflict():
    client = make_client(responder((409, {"error": "version conflict"}, {})))
    with pytest.raises(HttpError) as excinfo:
        client.request("GET", "https://api.invalid/x")
    assert not isinstance(excinfo.value, BudgetExhaustedError)
    client.close()


def test_429_with_credits_remaining_is_transient_not_budget():
    sleeper = Sleeper()
    client = make_client(
        responder(
            (429, {"error": "too fast"}, {"X-RateLimit-Remaining": "99000"}),
            (200, {"ok": True}, {"X-RateLimit-Remaining": "98990"}),
        ),
        sleeper=sleeper,
    )
    assert client.request("GET", "https://api.invalid/x").status_code == 200
    assert sleeper.calls == [2.0]
    client.close()


def test_once_suspended_further_requests_are_refused_immediately():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429,
            json={"error": "daily quota exceeded"},
            headers={"X-RateLimit-Remaining": "0"},
            request=request,
        )

    client = make_client(handler)
    with pytest.raises(BudgetExhaustedError):
        client.request("GET", "https://api.invalid/x")
    with pytest.raises(BudgetExhaustedError):
        client.request("GET", "https://api.invalid/y")
    assert calls["n"] == 1  # the second call never reached the network
    client.close()


def test_zero_remaining_on_a_successful_response_pre_empts_the_next_call():
    client = make_client(
        responder((200, {"ok": True}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "60"}))
    )
    client.request("GET", "https://api.invalid/x")
    assert client.suspended_error is not None
    with pytest.raises(BudgetExhaustedError):
        client.request("GET", "https://api.invalid/y")
    client.close()


def test_local_daily_credit_ceiling_suspends_before_the_provider_budget():
    """SPEC_PATCH section 2: an optional local safety ceiling behaves identically."""
    client = make_client(
        responder((200, {"ok": True}, {"X-RateLimit-Credits-Used": "10"})),
        daily_credit_ceiling=25,
    )
    client.request("GET", "https://api.invalid/1")
    client.request("GET", "https://api.invalid/2")
    assert client.suspended_error is None
    client.request("GET", "https://api.invalid/3")  # 30 >= 25
    error = client.suspended_error
    assert error is not None
    assert error.details.get("local_ceiling") is True
    with pytest.raises(BudgetExhaustedError):
        client.request("GET", "https://api.invalid/4")
    client.close()


def test_budget_headers_are_tracked():
    client = make_client(
        responder(
            (
                200,
                {"ok": True},
                {
                    "X-RateLimit-Limit": "100000",
                    "X-RateLimit-Remaining": "99990",
                    "X-RateLimit-Credits-Used": "10",
                    "X-RateLimit-Reset": "1800",
                },
            )
        )
    )
    client.request("GET", "https://api.invalid/x")
    assert client.budget.limit == 100000
    assert client.budget.remaining == 99990
    assert client.budget.used_by_this_process == 10
    assert client.budget.reset_in_seconds == 1800
    client.close()
