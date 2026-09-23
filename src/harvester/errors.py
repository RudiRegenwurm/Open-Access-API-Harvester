# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""Structured error model.

MASTER_SPEC section 29: failures are data. Every failure carries a category, the
operation, the source, the attempt number, an HTTP status where applicable, a concise
message and an explicit retryability flag. Secrets never enter these objects — callers
pass URLs through :func:`harvester.http.redact_url` first.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class ErrorCategory(str, enum.Enum):
    """Categories from MASTER_SPEC section 29."""

    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NETWORK_ERROR = "NETWORK_ERROR"
    TIMEOUT = "TIMEOUT"
    HTTP_ERROR = "HTTP_ERROR"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    NOT_FOUND = "NOT_FOUND"
    INVALID_METADATA = "INVALID_METADATA"
    DOWNLOAD_ERROR = "DOWNLOAD_ERROR"
    INVALID_PDF = "INVALID_PDF"
    INVALID_XML = "INVALID_XML"
    #: The provider answered with an automated-access challenge instead of the file.
    #: Distinct from INVALID_PDF because nothing is wrong with the artifact, and
    #: distinct from AUTHENTICATION_ERROR because no credential would help. The
    #: challenge is reported, never solved: the harvester circumvents no access
    #: control, so this is a dead end to be named honestly, not an obstacle to clear.
    BOT_CHALLENGE = "BOT_CHALLENGE"
    CHECKSUM_ERROR = "CHECKSUM_ERROR"
    STORAGE_ERROR = "STORAGE_ERROR"
    STATE_ERROR = "STATE_ERROR"
    SIZE_LIMIT_EXCEEDED = "SIZE_LIMIT_EXCEEDED"
    UNSAFE_URL = "UNSAFE_URL"
    NO_OA_LOCATION = "NO_OA_LOCATION"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


#: Categories that are never worth retrying, regardless of attempt budget.
PERMANENT_CATEGORIES = frozenset(
    {
        ErrorCategory.CONFIGURATION_ERROR,
        ErrorCategory.AUTHENTICATION_ERROR,
        ErrorCategory.NOT_FOUND,
        ErrorCategory.INVALID_METADATA,
        ErrorCategory.INVALID_PDF,
        ErrorCategory.INVALID_XML,
        # Retrying a challenge returns the same challenge, and solving it is out of
        # the question, so there is nothing left to attempt on this location.
        ErrorCategory.BOT_CHALLENGE,
        ErrorCategory.CHECKSUM_ERROR,
        ErrorCategory.SIZE_LIMIT_EXCEEDED,
        ErrorCategory.UNSAFE_URL,
        ErrorCategory.NO_OA_LOCATION,
    }
)


@dataclass(slots=True)
class Failure:
    """A structured failure record."""

    category: ErrorCategory
    message: str
    operation: str
    source: str | None = None
    document_id: str | None = None
    attempt: int = 1
    http_status: int | None = None
    url: str | None = None
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "message": self.message,
            "operation": self.operation,
            "source": self.source,
            "document_id": self.document_id,
            "attempt": self.attempt,
            "http_status": self.http_status,
            "url": self.url,
            "retryable": self.retryable,
            "details": self.details,
        }


class HarvesterError(Exception):
    """Base class for all harvester errors carrying a structured category."""

    category: ErrorCategory = ErrorCategory.UNKNOWN_ERROR
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory | None = None,
        retryable: bool | None = None,
        http_status: int | None = None,
        url: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if category is not None:
            self.category = category
        if retryable is not None:
            self.retryable = retryable
        self.http_status = http_status
        self.url = url
        self.details = details or {}

    def as_failure(
        self,
        operation: str,
        *,
        source: str | None = None,
        document_id: str | None = None,
        attempt: int = 1,
    ) -> Failure:
        return Failure(
            category=self.category,
            message=self.message,
            operation=operation,
            source=source,
            document_id=document_id,
            attempt=attempt,
            http_status=self.http_status,
            url=self.url,
            retryable=self.retryable,
            details=dict(self.details),
        )


class ConfigurationError(HarvesterError):
    category = ErrorCategory.CONFIGURATION_ERROR


class AuthenticationError(HarvesterError):
    category = ErrorCategory.AUTHENTICATION_ERROR


class RateLimitedError(HarvesterError):
    """Transient throttling. Retried with configured backoff."""

    category = ErrorCategory.RATE_LIMITED
    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class BudgetExhaustedError(HarvesterError):
    """Provider daily budget (or the local safety ceiling) is spent.

    SPEC_PATCH section 2/7: this is *not* an ordinary retryable failure. It must
    checkpoint state and enter a clean resumable suspension instead of retrying.
    """

    category = ErrorCategory.BUDGET_EXHAUSTED
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        reset_at: str | None = None,
        reset_in_seconds: float | None = None,
        limit: int | None = None,
        remaining: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.provider = provider
        self.reset_at = reset_at
        self.reset_in_seconds = reset_in_seconds
        self.limit = limit
        self.remaining = remaining

    def suspension_details(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "reset_at": self.reset_at,
            "reset_in_seconds": self.reset_in_seconds,
            "limit": self.limit,
            "remaining": self.remaining,
            "http_status": self.http_status,
        }


class NetworkError(HarvesterError):
    category = ErrorCategory.NETWORK_ERROR
    retryable = True


class TimeoutError_(HarvesterError):
    category = ErrorCategory.TIMEOUT
    retryable = True


class HttpError(HarvesterError):
    category = ErrorCategory.HTTP_ERROR


class ProviderError(HarvesterError):
    category = ErrorCategory.PROVIDER_ERROR


class NotFoundError(HarvesterError):
    category = ErrorCategory.NOT_FOUND


class InvalidMetadataError(HarvesterError):
    category = ErrorCategory.INVALID_METADATA


class DownloadError(HarvesterError):
    category = ErrorCategory.DOWNLOAD_ERROR
    retryable = True


class InvalidPdfError(HarvesterError):
    category = ErrorCategory.INVALID_PDF


class BotChallengeError(HarvesterError):
    """The provider served an automated-access challenge instead of the artifact."""

    category = ErrorCategory.BOT_CHALLENGE


class InvalidXmlError(HarvesterError):
    category = ErrorCategory.INVALID_XML


class SizeLimitExceededError(HarvesterError):
    category = ErrorCategory.SIZE_LIMIT_EXCEEDED


class UnsafeUrlError(HarvesterError):
    category = ErrorCategory.UNSAFE_URL


class StorageError(HarvesterError):
    category = ErrorCategory.STORAGE_ERROR


class StateError(HarvesterError):
    category = ErrorCategory.STATE_ERROR


#: Exit codes for the CLI (MASTER_SPEC section 33).
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 2
EXIT_PARTIAL_FAILURE = 3
EXIT_SUSPENDED = 4
EXIT_INTERRUPTED = 130
