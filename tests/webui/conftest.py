"""Fixtures for the web UI tests.

The tests drive the real HTTP server over a real socket, so routing, JSON encoding,
static serving and file streaming are all exercised. Provider traffic is served by
the same deterministic mocks the core suite uses — no network, no credentials.

Every ``HARVESTER_*`` variable (and the advisor vendor's ``ANTHROPIC_API_KEY``) is
cleared for the duration of each test by the ``isolated_environment`` fixture in the
parent ``conftest``. An environment value beats this fixture's config file, so without
that these tests would assert against the developer's real credentials rather than the
fake ones below.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from harvester.advisor import build_advisor
from harvester.http import ClientPool
from harvester.webui.server import create_server
from mocks import ADVISOR_BASE, EPMC_BASE, OPENALEX_BASE, UNPAYWALL_BASE, MockProviders


class Client:
    """Minimal HTTP client for the server under test."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def request(
        self, method: str, path: str, payload: Any = None, *, raw: bool = False
    ) -> Any:
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read()
                self.status = response.status
                self.headers = dict(response.headers)
                if raw:
                    return body
                return json.loads(body) if body else None
        except HTTPError as error:
            body = error.read()
            self.status = error.code
            self.headers = dict(error.headers)
            if raw:
                return body
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {"error": body.decode("utf-8", "replace")}

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, payload: Any = None) -> Any:
        return self.request("POST", path, payload if payload is not None else {})

    def put(self, path: str, payload: Any = None) -> Any:
        return self.request("PUT", path, payload if payload is not None else {})


@pytest.fixture
def ui_providers() -> MockProviders:
    from mocks import make_pdf_bytes, openalex_work

    return MockProviders(
        works=[openalex_work(i) for i in range(1, 6)],
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, 6)},
    )


@pytest.fixture
def ui_config_file(tmp_path: Path) -> Path:
    """A config file pointing every provider at the in-process mocks."""
    path = tmp_path / "harvester.json"
    path.write_text(
        json.dumps(
            {
                "storage_root": str(tmp_path / "corpus"),
                "state_db": str(tmp_path / "state" / "harvester.sqlite3"),
                "reports_dir": str(tmp_path / "reports"),
                "contact_email": "operator@example.org",
                "log_level": "ERROR",
                "openalex": {
                    "api_key": "ui-test-key",
                    "base_url": OPENALEX_BASE,
                    "per_page": 25,
                    "requests_per_second": 1000,
                },
                "europe_pmc": {"base_url": EPMC_BASE, "requests_per_second": 1000},
                "unpaywall": {"base_url": UNPAYWALL_BASE, "requests_per_second": 1000},
                "advisor": {
                    "api_key": "ui-advisor-key",
                    "base_url": ADVISOR_BASE,
                    "requests_per_second": 1000,
                },
                "downloads": {
                    "concurrency": 1,
                    "requests_per_second": 1000,
                    "min_pdf_size_bytes": 512,
                },
                "retry": {
                    "max_attempts": 2,
                    "backoff_initial_seconds": 0.01,
                    "backoff_max_seconds": 0.02,
                    "jitter_ratio": 0.0,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def ui(ui_config_file: Path, ui_providers: MockProviders, monkeypatch) -> Iterator[Client]:
    """A running server whose harvests use the mock transport."""
    import harvester.webui.api as api_module
    import harvester.webui.runmanager as runmanager

    original = ClientPool

    def pooled(config, **kwargs):
        kwargs.setdefault("transport", ui_providers.transport)
        kwargs.setdefault("sleeper", lambda _s: None)
        return original(config, **kwargs)

    monkeypatch.setattr(runmanager, "ClientPool", pooled)
    # The discovery preview opens its own pool: it must reach the same mocks.
    monkeypatch.setattr(api_module, "ClientPool", pooled)

    server = create_server(config_path=ui_config_file, host="127.0.0.1", port=0)
    # The advisor talks to the same in-process mock transport as every other
    # provider: deterministic, offline, no credentials.
    server.context.advisor_factory = lambda config: build_advisor(
        config, transport=ui_providers.transport, sleeper=lambda _s: None
    )
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    client = Client(f"http://{host}:{port}")
    client.server = server  # type: ignore[attr-defined]
    client.providers = ui_providers  # type: ignore[attr-defined]
    client.config_path = ui_config_file  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def wait_for_idle(ui: Client, timeout: float = 60.0) -> dict[str, Any]:
    """Block until the background operation finishes, then return it."""
    ui.server.context.runs.wait(timeout)  # type: ignore[attr-defined]
    for _ in range(int(timeout * 10)):
        data = ui.get("/api/activity")
        if not data["busy"] and data["operation"] and data["operation"]["finished"]:
            return data["operation"]
        threading.Event().wait(0.1)
    raise AssertionError("the background operation did not finish in time")
