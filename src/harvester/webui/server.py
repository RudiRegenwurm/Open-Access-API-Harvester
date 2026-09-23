# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""The local HTTP server.

Standard-library :class:`http.server.ThreadingHTTPServer` with a small dispatch
table. A framework would add several dependencies for roughly twenty local,
single-user routes; the repository's rule is that every dependency needs a concrete
purpose, and this one would not have earned its place.

The server binds to the loopback interface by default. There is no authentication —
that is a deliberate V1 non-goal — so binding anywhere else is opt-in and warned about.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__
from ..errors import HarvesterError
from ..util import to_json
from .api import Api, ApiError, AppContext

LOGGER = logging.getLogger("harvester.webui.server")

STATIC_ROOT = Path(__file__).resolve().parent / "static"

#: Largest request body accepted. The UI only ever posts small JSON forms.
MAX_BODY_BYTES = 1 * 1024 * 1024

#: Files streamed rather than buffered, in chunks of this size.
_CHUNK = 64 * 1024


class Route:
    __slots__ = ("method", "pattern", "handler", "kind")

    def __init__(self, method: str, pattern: str, handler: str, kind: str) -> None:
        self.method = method
        self.pattern = re.compile(f"^{pattern}$")
        self.handler = handler
        self.kind = kind  # "query" | "body" | "file"


#: ``{id}`` matches a single path segment.
def _p(path: str) -> str:
    return path.replace("{id}", r"([^/]+)")


ROUTES: list[Route] = [
    Route("GET", _p("/api/dashboard"), "dashboard", "query"),
    Route("GET", _p("/api/activity"), "activity", "query"),
    Route("GET", _p("/api/providers"), "providers", "query"),
    Route("GET", _p("/api/vocabulary"), "vocabulary", "query"),
    Route("POST", _p("/api/harvest"), "start_harvest", "body"),
    Route("POST", _p("/api/search/advice"), "search_advice", "body"),
    Route("POST", _p("/api/search/preview"), "search_preview", "body"),
    Route("POST", _p("/api/search/similar-runs"), "similar_runs", "body"),
    Route("GET", _p("/api/runs"), "list_runs", "query"),
    Route("GET", _p("/api/runs/{id}"), "run_detail", "query"),
    Route("GET", _p("/api/runs/{id}/report"), "run_report", "query"),
    Route("POST", _p("/api/runs/{id}/resume"), "resume_run", "body"),
    Route("GET", _p("/api/corpus"), "corpus", "query"),
    Route("GET", _p("/api/corpus/facets"), "corpus_facets", "query"),
    Route("GET", _p("/api/corpus/{id}"), "corpus_detail", "query"),
    Route("POST", _p("/api/verify"), "start_verify", "body"),
    Route("GET", _p("/api/verify/history"), "verify_history", "query"),
    Route("GET", _p("/api/failures"), "failures", "query"),
    Route("POST", _p("/api/failures/retry"), "retry_failures", "body"),
    Route("GET", _p("/api/settings"), "get_settings", "query"),
    Route("PUT", _p("/api/settings"), "put_settings", "body"),
]

_FILE_ROUTE = re.compile(r"^/api/corpus/([^/]+)/file/(pdf|xml|json)$")


class Handler(BaseHTTPRequestHandler):
    """One request. The server instance carries the shared :class:`Api`."""

    server_version = f"OAHarvesterUI/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------------

    @property
    def api(self) -> Api:
        return self.server.api  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Local-only tool: refuse to be embedded or sniffed.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, to_json(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str, detail: Any = None) -> None:
        self._json(status, {"error": message, "detail": detail, "status": status})

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "Request body too large.")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, f"Request body is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ApiError(400, "Request body must be a JSON object.")
        return payload

    # -- verbs ---------------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    # -- dispatch ------------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        # Routes are matched against the *encoded* path and each captured segment is
        # decoded afterwards. Decoding first would turn an encoded slash in a DOI
        # ("10.1234%2Fx") into a real separator and break segment matching.
        raw_path = parsed.path
        path = unquote(raw_path)
        try:
            if not raw_path.startswith("/api/"):
                if method != "GET":
                    self._error(405, "Method not allowed.")
                    return
                self._serve_static(path)
                return

            file_match = _FILE_ROUTE.match(raw_path)
            if file_match and method == "GET":
                self._serve_artifact(unquote(file_match.group(1)), file_match.group(2))
                return

            for route in ROUTES:
                match = route.pattern.match(raw_path)
                if not match:
                    continue
                if route.method != method:
                    continue
                handler: Callable[..., Any] = getattr(self.api, route.handler)
                args = [unquote(group) for group in match.groups()]
                if route.kind == "body":
                    result = handler(*args, self._read_body())
                else:
                    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                    result = handler(*args, params)
                self._json(200, result)
                return

            # Path exists but under a different verb -> 405 is more useful than 404.
            if any(r.pattern.match(raw_path) for r in ROUTES):
                self._error(405, "Method not allowed.")
            else:
                self._error(404, "Unknown API endpoint.")
        except ApiError as exc:
            self._error(exc.status, exc.message, exc.detail)
        except HarvesterError as exc:
            LOGGER.warning("core error serving %s: %s", path, exc)
            self._error(400, str(exc), {"category": exc.category.value})
        except BrokenPipeError:  # pragma: no cover - client navigated away
            pass
        except Exception as exc:  # noqa: BLE001 - the UI must never see a bare traceback
            LOGGER.exception("unhandled error serving %s", path)
            self._error(500, f"Unexpected server error: {exc}")

    # -- static and files ----------------------------------------------------

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        candidate = (STATIC_ROOT / relative).resolve()
        try:
            candidate.relative_to(STATIC_ROOT)
        except ValueError:
            self._error(403, "Forbidden.")
            return
        if not candidate.is_file():
            # Unknown non-API path: hand the SPA its shell so deep links work.
            candidate = STATIC_ROOT / "index.html"
            if not candidate.is_file():
                self._error(404, "UI assets are missing from the installation.")
                return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript",
            "application/json",
        ):
            content_type += "; charset=utf-8"
        self._send(200, candidate.read_bytes(), content_type)

    def _serve_artifact(self, document_id: str, kind: str) -> None:
        try:
            path, content_type = self.api.artifact_file(document_id, kind)
        except ApiError as exc:
            self._error(exc.status, exc.message, exc.detail)
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        # Inline so a PDF opens in the browser's viewer instead of downloading.
        self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as handle:
            while chunk := handle.read(_CHUNK):
                self.wfile.write(chunk)


class ControlCenterServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], context: AppContext) -> None:
        super().__init__(address, Handler)
        self.context = context
        self.api = Api(context)

    def server_close(self) -> None:  # pragma: no cover - teardown
        try:
            super().server_close()
        finally:
            self.context.close()


def create_server(
    *, config_path: Path, host: str = "127.0.0.1", port: int = 8765,
    config_overrides: dict[str, Any] | None = None,
) -> ControlCenterServer:
    """Build a server bound to *host*:*port* (port 0 picks a free one)."""
    context = AppContext(Path(config_path), config_overrides=config_overrides)
    return ControlCenterServer((host, port), context)


def serve(
    *,
    config_path: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    config_overrides: dict[str, Any] | None = None,
) -> int:
    """Run the control center until interrupted. Returns a process exit code."""
    try:
        server = create_server(
            config_path=config_path, host=host, port=port,
            config_overrides=config_overrides,
        )
    except OSError as exc:
        LOGGER.error("cannot bind %s:%s — %s", host, port, exc)
        print(f"Could not start the UI on {host}:{port}: {exc}")
        if isinstance(exc, OSError) and exc.errno in (48, 98, 10048):
            print("That port is already in use. Try: harvester serve --port 8766")
        return 1

    bound_host, bound_port = server.server_address[:2]
    url = f"http://{'localhost' if bound_host in ('127.0.0.1', '::1') else bound_host}:{bound_port}/"

    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "WARNING: the control center has no authentication and is now reachable "
            f"from the network on {bound_host}. Use 127.0.0.1 unless you understand "
            "the consequences."
        )

    print(f"Notanda control center running at {url}")
    print(f"  configuration : {config_path}")
    print(f"  corpus        : {server.context.config.storage_root}")
    print(f"  state database: {server.context.config.state_db}")
    print("Press Ctrl+C to stop.")

    if open_browser:
        threading.Timer(0.4, lambda: _open_browser(url)).start()

    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        server.shutdown()
        server.server_close()
    return 0


def _open_browser(url: str) -> None:  # pragma: no cover - environment dependent
    try:
        webbrowser.open(url)
    except Exception:
        LOGGER.debug("could not open a browser automatically")


def find_free_port() -> int:  # pragma: no cover - helper for tests and scripts
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


__all__ = ["ControlCenterServer", "create_server", "find_free_port", "serve"]
