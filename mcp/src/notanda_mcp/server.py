# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""Local-only MCP server with durable, independently verifiable evidence records.

This module is adapted from the isolated Notanda MCP experiment at commit
366bc2d4827aa915e2e58a82cd567163ed63d4be. It intentionally performs no corpus
writes and no full-text downloads.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path
import re
from typing import Any
import uuid

import httpx

from harvester.config import Config, RetryConfig
from harvester.http import ProviderClient, redact_url
from harvester.providers.openalex import OpenAlexAdapter, OpenAlexQuery
from harvester.util import utc_now_iso

SOURCE_EXPERIMENT_COMMIT = "366bc2d4827aa915e2e58a82cd567163ed63d4be"
PAYLOADS = ("request.json", "provider.json", "response.json")
MAX_BODY = 4 * 1024 * 1024


def encode(value: Any) -> bytes:
    """Return a deterministic UTF-8 JSON representation."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def publish(path: Path, value: Any) -> None:
    """Atomically publish one evidence payload."""

    temporary = path.with_suffix(".pending")
    with temporary.open("xb") as stream:
        stream.write(encode(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class EvidenceStore:
    """Filesystem store for one immutable evidence directory per request."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def begin(self, request: dict) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        folder = self.root / request["evidence_id"]
        folder.mkdir()
        publish(folder / "request.json", request)
        return folder

    def finish(self, folder: Path, provider: dict, response: dict) -> None:
        publish(folder / "provider.json", provider)
        publish(folder / "response.json", response)
        files = {}
        for name in PAYLOADS:
            data = (folder / name).read_bytes()
            files[name] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        publish(
            folder / "manifest.json",
            {
                "schema_version": 1,
                "status": "complete",
                "outcome": response["status"],
                "files": files,
            },
        )

    def get(self, evidence_id: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{32}", evidence_id):
            return {"status": "error", "error": "invalid_evidence_id"}
        folder = self.root / evidence_id
        if not folder.is_dir():
            return {"status": "error", "error": "not_found"}
        if not (folder / "manifest.json").exists():
            return {"status": "incomplete", "evidence_id": evidence_id}
        try:
            manifest = json.loads((folder / "manifest.json").read_bytes())
            if manifest["schema_version"] != 1 or set(manifest["files"]) != set(PAYLOADS):
                raise ValueError("schema")
            data = {}
            for name in PAYLOADS:
                raw = (folder / name).read_bytes()
                expected = manifest["files"][name]
                if (
                    len(raw) != expected["bytes"]
                    or hashlib.sha256(raw).hexdigest() != expected["sha256"]
                ):
                    raise ValueError("hash")
                data[name] = json.loads(raw)
            if (
                data["request.json"]["evidence_id"] != evidence_id
                or data["response.json"]["evidence_id"] != evidence_id
            ):
                raise ValueError("identity")
            return data["response.json"]
        except (OSError, ValueError, KeyError, TypeError):
            return {
                "status": "error",
                "error": "integrity_failure",
                "evidence_id": evidence_id,
            }


class RecordingTransport(httpx.BaseTransport):
    """Capture the actual OpenAlex exchange after removing configured secrets."""

    def __init__(self, inner: httpx.BaseTransport, provider: dict, scrub):
        self.inner = inner
        self.provider = provider
        self.scrub = scrub

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        attempt = {
            "started_at": utc_now_iso(),
            "method": request.method,
            "url": self.scrub(redact_url(str(request.url))),
        }
        self.provider["attempts"].append(attempt)
        try:
            # No redirected destination or arbitrary endpoint, even from local config.
            if (
                request.url.host != "api.openalex.org"
                or request.url.path != "/works"
                or request.url.scheme != "https"
            ):
                raise ValueError("unexpected_provider_endpoint")
            response = self.inner.handle_request(request)
            attempt["http_status"] = response.status_code
            try:
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BODY:
                        raise ValueError("provider_response_too_large")
                    chunks.append(chunk)
                body = b"".join(chunks)
            finally:
                response.close()
            attempt["body_bytes"] = len(body)
            try:
                self.provider["observed_json"] = self.scrub(json.loads(body))
                self.provider["capture"] = (
                    "parsed_json_with_secret_redaction_not_wire_bytes"
                )
            except (ValueError, UnicodeDecodeError):
                self.provider["capture"] = "non_json_body_not_retained"
            return httpx.Response(
                response.status_code,
                headers={"Content-Type": "application/json"},
                content=body,
                request=request,
            )
        except Exception as exc:
            attempt["error_type"] = type(exc).__name__
            raise
        finally:
            attempt["finished_at"] = utc_now_iso()

    def close(self) -> None:
        self.inner.close()


class Experiment:
    """Implement the two deliberately narrow MCP operations."""

    def __init__(self, root: Path, config: Config, transport_factory=None):
        self.store = EvidenceStore(root)
        self.config = config
        self.transport_factory = transport_factory or (
            lambda: httpx.HTTPTransport(retries=0)
        )
        self.secrets = [
            secret
            for secret in (
                config.openalex.api_key,
                config.contact_email,
                config.advisor.api_key,
            )
            if secret
        ]

    def scrub(self, value):
        if isinstance(value, dict):
            return {
                key: (
                    "REDACTED"
                    if key.lower()
                    in {
                        "api_key",
                        "apikey",
                        "authorization",
                        "token",
                        "contact_email",
                    }
                    else self.scrub(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "REDACTED")
            if value.startswith(("http://", "https://")):
                value = redact_url(value)
        return value

    def search(self, query: Any = None, limit: Any = 5) -> dict:
        evidence_id = uuid.uuid4().hex
        valid = (
            isinstance(query, str)
            and 0 < len(query.strip()) <= 1000
            and type(limit) is int
            and 1 <= limit <= 10
        )
        effective = OpenAlexQuery(search=query.strip()).to_dict() if valid else None
        request = self.scrub(
            {
                "schema_version": 1,
                "evidence_id": evidence_id,
                "requested_at": utc_now_iso(),
                "tool": "search_literature",
                "provider": "openalex",
                "supplied": {"query": query, "limit": limit},
                "effective": effective,
                "limit": limit if valid else None,
                "source_experiment_commit": SOURCE_EXPERIMENT_COMMIT,
                "notanda_core_version": version("notanda"),
                "server_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            }
        )
        try:
            folder = self.store.begin(request)
        except OSError:
            return {
                "status": "error",
                "error": "evidence_storage_unavailable",
                "persisted": False,
            }
        provider = {
            "attempts": [],
            "capture": "not_attempted",
            "pagination": False,
            "max_attempts": 1,
        }
        response = {
            "evidence_id": evidence_id,
            "status": "error",
            "error": "invalid_arguments",
        }
        if valid:
            try:
                cfg = replace(
                    self.config.openalex,
                    base_url="https://api.openalex.org",
                    per_page=limit,
                    timeout_seconds=20,
                    connect_timeout_seconds=10,
                )
                if not cfg.api_key and not cfg.allow_keyless:
                    raise ValueError("missing_openalex_key")
                with ProviderClient(
                    "openalex",
                    cfg,
                    RetryConfig(max_attempts=1),
                    user_agent="Notanda-MCP-Experiment/1",
                    max_redirects=0,
                    transport=RecordingTransport(
                        self.transport_factory(), provider, self.scrub
                    ),
                ) as client:
                    page = OpenAlexAdapter(client, cfg).discover_page(
                        OpenAlexQuery(search=query.strip())
                    )
                results = [
                    {
                        "document_id": document.document_id,
                        "title": document.title,
                        "doi": document.doi,
                        "year": document.publication_year,
                        "source_id": source.source_id,
                    }
                    for document, source in page.records[:limit]
                ]
                provider["raw_record_count"] = page.raw_count
                provider["normalized_record_count"] = len(page.records)
                response = {
                    "evidence_id": evidence_id,
                    "status": "ok",
                    "results": results,
                    "limit": limit,
                    "total_count": page.total_count,
                    "pagination": False,
                }
            except Exception as exc:
                # Never persist exception messages: providers can echo credentials.
                response["error"] = (
                    "configuration_error"
                    if isinstance(exc, ValueError)
                    and str(exc) == "missing_openalex_key"
                    else "provider_error"
                )
                response["error_type"] = type(exc).__name__
        response = self.scrub(response)
        try:
            self.store.finish(folder, self.scrub(provider), response)
        except OSError:
            return {
                "status": "error",
                "error": "evidence_storage_unavailable",
                "evidence_id": evidence_id,
                "persisted": False,
            }
        return response


def main() -> None:
    """Run the local stdio MCP server."""

    from mcp.server.fastmcp import FastMCP

    parser = argparse.ArgumentParser(description="Notanda local MCP experiment (stdio only)")
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)  # No raw provider exceptions or URLs on stderr.
    try:
        experiment = Experiment(args.evidence_dir, Config.load())
    except Exception:
        parser.exit(
            2,
            "Notanda: invalid local configuration; inspect your configuration file.\n",
        )
    server = FastMCP("Notanda local evidence experiment")

    @server.tool()
    def search_literature(query: Any = None, limit: Any = 5) -> dict[str, Any]:
        """Search OpenAlex OA works with DOI and persist a local evidence record."""

        return experiment.search(query, limit)

    @server.tool()
    def get_evidence(evidence_id: str) -> dict[str, Any]:
        """Verify saved hashes and retrieve the original result without a new search."""

        return experiment.store.get(evidence_id)

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
