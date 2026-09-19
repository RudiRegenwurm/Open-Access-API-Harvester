"""Artifact acquisition: bounded streaming download, validation, atomic publication.

MASTER_SPEC sections 14, 17, 37, 38, 39.

The invariant enforced here: **a final artifact filename never exists unless the bytes
behind it were fully downloaded, validated and hashed.** Everything is written to a
``.part`` file first and published with a single atomic rename.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .errors import (
    BudgetExhaustedError,
    DownloadError,
    HarvesterError,
    SizeLimitExceededError,
    StorageError,
)
from .http import ProviderClient, redact_url
from .identity import artifact_path, validate_remote_url
from .models import ArtifactKind, ArtifactRecord, FulltextCandidate
from .util import coerce_int, utc_now_iso
from .validation import ValidationResult, content_type_is_plausible, validate_pdf, validate_xml

LOGGER = logging.getLogger("harvester.acquisition")

PART_SUFFIX = ".part"
_CHUNK_SIZE = 64 * 1024


@dataclass(slots=True)
class AcquisitionOutcome:
    artifact: ArtifactRecord
    validation: ValidationResult
    #: How many HTTP attempts this artifact needed (1 = no retry).
    attempts: int = 1


class Acquirer:
    """Downloads and publishes one artifact at a time."""

    def __init__(self, config: Config, client: ProviderClient) -> None:
        self._config = config
        self._client = client
        self._storage_root = Path(config.storage_root)

    def acquire(
        self,
        *,
        document_id: str,
        candidate: FulltextCandidate,
        on_attempt: Callable[[int, HarvesterError | None, int | None], None] | None = None,
    ) -> AcquisitionOutcome:
        """Download, validate and atomically publish one candidate.

        Retryable transport conditions (timeout, reset, 429, 5xx) are retried with the
        configured backoff; a rejected *artifact* is not retried, because the same URL
        will keep returning the same bytes. Raises a :class:`HarvesterError` subclass
        on final failure. The temporary file is always removed, so an interrupted or
        rejected download leaves nothing behind.
        """
        url = validate_remote_url(
            candidate.url, allow_private_hosts=self._config.downloads.allow_private_hosts
        )
        final_path = artifact_path(self._storage_root, document_id, candidate.kind.value)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        max_attempts = max(1, self._config.retry.max_attempts)

        for attempt in range(1, max_attempts + 1):
            part_path = _new_part_path(final_path)
            try:
                resolved_url, status, content_type, _size = self._download(url, part_path)
                validation = self._validate(candidate.kind, part_path)
                if not content_type_is_plausible(candidate.kind.value, content_type):
                    LOGGER.info(
                        "artifact %s: content-type %r disagrees with validated %s content",
                        document_id,
                        content_type,
                        candidate.kind.value,
                    )
                _publish(part_path, final_path)
            except HarvesterError as exc:
                _discard(part_path)
                if on_attempt is not None:
                    on_attempt(attempt, exc, exc.http_status)
                if (
                    isinstance(exc, BudgetExhaustedError)
                    or not exc.retryable
                    or attempt >= max_attempts
                ):
                    raise
                self._client.sleep_before_retry(attempt, exc)
                continue
            except BaseException:
                _discard(part_path)
                raise

            if on_attempt is not None:
                on_attempt(attempt, None, status)
            return AcquisitionOutcome(
                artifact=ArtifactRecord(
                    kind=candidate.kind,
                    filename=final_path.name,
                    sha256=validation.sha256,
                    size_bytes=validation.size_bytes,
                    retrieved_at=utc_now_iso(),
                    source=candidate.source,
                    original_url=redact_url(candidate.url),
                    resolved_url=redact_url(resolved_url),
                    http_status=status,
                    content_type=content_type,
                ),
                validation=validation,
                attempts=attempt,
            )
        raise AssertionError("unreachable: the retry loop always returns or raises")

    # ------------------------------------------------------------------ internals

    def _download(self, url: str, part_path: Path) -> tuple[str, int, str | None, int]:
        limit = self._config.downloads.max_download_size_bytes
        with self._client.stream("GET", url) as response:
            status = response.status_code
            content_type = response.headers.get("content-type")
            resolved_url = str(response.url)

            # Reject before transferring anything when the server announces a size we
            # will not accept (MASTER_SPEC section 39).
            declared = coerce_int(response.headers.get("content-length"))
            if declared is not None and declared > limit:
                raise SizeLimitExceededError(
                    f"server declared {declared} bytes, above the configured limit of {limit}",
                    http_status=status,
                    url=redact_url(url),
                )

            written = 0
            try:
                with open(part_path, "wb") as handle:
                    for chunk in response.iter_bytes(_CHUNK_SIZE):
                        if not chunk:
                            continue
                        written += len(chunk)
                        # The limit is enforced during transfer too: Content-Length may
                        # be absent, wrong, or the response may be chunked.
                        if written > limit:
                            raise SizeLimitExceededError(
                                f"download exceeded the configured limit of {limit} bytes",
                                http_status=status,
                                url=redact_url(url),
                            )
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            except SizeLimitExceededError:
                raise
            except HarvesterError:
                raise
            except OSError as exc:
                raise StorageError(
                    f"could not write {part_path.name}: {exc}", url=redact_url(url)
                ) from exc
            except Exception as exc:  # transport failure part-way through the stream
                raise DownloadError(
                    f"download interrupted after {written} bytes: {exc}",
                    http_status=status,
                    url=redact_url(url),
                ) from exc

        if written == 0:
            raise DownloadError(
                "server returned an empty body", http_status=status, url=redact_url(url)
            )
        return resolved_url, status, content_type, written

    def _validate(self, kind: ArtifactKind, path: Path) -> ValidationResult:
        if kind is ArtifactKind.PDF:
            return validate_pdf(path, min_size_bytes=self._config.downloads.min_pdf_size_bytes)
        return validate_xml(path, min_size_bytes=self._config.downloads.min_xml_size_bytes)


def _new_part_path(final_path: Path) -> Path:
    """A unique temporary path beside the final artifact.

    Uniqueness matters: two workers must never share a ``.part`` file, and a stale
    ``.part`` left by a killed process must never be appended to or mistaken for
    live work.
    """
    handle, name = tempfile.mkstemp(
        prefix=f"{final_path.name}.", suffix=PART_SUFFIX, dir=str(final_path.parent)
    )
    os.close(handle)
    return Path(name)


def _publish(part_path: Path, final_path: Path) -> None:
    """Atomically move the validated temporary file into place."""
    try:
        os.replace(part_path, final_path)
    except OSError as exc:
        raise StorageError(f"could not publish {final_path.name}: {exc}") from exc


def _discard(part_path: Path) -> None:
    try:
        if part_path.exists():
            part_path.unlink()
    except OSError:  # pragma: no cover - best effort cleanup
        LOGGER.warning("could not remove temporary file %s", part_path)


def sweep_stale_parts(storage_root: Path, *, min_age_seconds: float = 300.0) -> list[str]:
    """Remove ``.part`` files left behind by a terminated process.

    MASTER_SPEC section 17: stale temporary files must be safely detectable and
    recoverable. They carry no validated content, so removing them costs nothing —
    the affected documents are re-acquired from their recorded state.

    Only files untouched for *min_age_seconds* are removed. A second harvester
    process working against the same corpus may have a download in flight, and its
    temporary file must not be deleted underneath it.
    """
    root = Path(storage_root)
    if not root.exists():
        return []
    cutoff = time.time() - max(0.0, min_age_seconds)
    removed: list[str] = []
    for path in sorted(root.glob(f"*{PART_SUFFIX}")):
        try:
            if path.stat().st_mtime > cutoff:
                continue
            path.unlink()
            removed.append(path.name)
        except OSError:  # pragma: no cover - concurrent sweep
            LOGGER.warning("could not remove stale temporary file %s", path)
    return removed
