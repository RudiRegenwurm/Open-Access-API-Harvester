"""Shared fixtures. Every test runs offline against the deterministic mocks."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from harvester.config import Config, env_variable_names  # noqa: E402
from harvester.http import ClientPool  # noqa: E402
from harvester.orchestrator import Harvester  # noqa: E402
from harvester.providers.openalex import OpenAlexQuery  # noqa: E402
from harvester.state import StateStore  # noqa: E402

from mocks import EPMC_BASE, OPENALEX_BASE, UNPAYWALL_BASE, MockProviders  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_environment(request, monkeypatch) -> None:
    """Run every offline test against a machine with no harvester settings on it.

    ``Config.load`` reads ``os.environ`` by default, so a developer who exports a real
    ``HARVESTER_OPENALEX_API_KEY`` or ``HARVESTER_CONTACT_EMAIL`` silently changes what
    the suite is testing: a case that asserts "an unconfigured harvest is refused"
    passes on a bare CI box and fails on the machine of the person who actually uses
    the tool. The variables are cleared for the duration of each test only —
    ``monkeypatch`` restores the real process environment afterwards, and the
    operator's own shell and configuration file are never touched.

    Live tests are exempt: they exist to contact the real providers and need the real
    credentials. They read them at import time in any case.
    """
    if request.node.get_closest_marker("live"):
        return
    for name in env_variable_names():
        monkeypatch.delenv(name, raising=False)


class RecordingSleeper:
    """Replaces ``time.sleep`` so backoff is observable and tests stay fast."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.calls)


@pytest.fixture
def sleeper() -> RecordingSleeper:
    return RecordingSleeper()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A fully local configuration pointed at the mock provider base URLs."""
    cfg = Config.load(
        overrides={
            "storage_root": str(tmp_path / "corpus"),
            "state_db": str(tmp_path / "state" / "harvester.sqlite3"),
            "reports_dir": str(tmp_path / "reports"),
            "contact_email": "operator@example.org",
            "openalex.api_key": "test-key-do-not-log",
            "openalex.base_url": OPENALEX_BASE,
            "openalex.per_page": 25,
            "openalex.requests_per_second": 1000,
            "europe_pmc.base_url": EPMC_BASE,
            "europe_pmc.requests_per_second": 1000,
            "unpaywall.base_url": UNPAYWALL_BASE,
            "unpaywall.requests_per_second": 1000,
            "downloads.requests_per_second": 1000,
            "downloads.concurrency": 1,
            "downloads.min_pdf_size_bytes": 512,
            "downloads.min_xml_size_bytes": 64,
            "retry.max_attempts": 3,
            "retry.backoff_initial_seconds": 0.01,
            "retry.backoff_max_seconds": 0.05,
            "retry.jitter_ratio": 0.0,
            "log_level": "WARNING",
        }
    )
    return cfg


@pytest.fixture
def store(config: Config) -> Iterator[StateStore]:
    with StateStore(config.state_db) as state_store:
        yield state_store


@pytest.fixture
def providers() -> MockProviders:
    from mocks import build_default_providers

    return build_default_providers(3)


def make_clients(
    config: Config, providers: MockProviders, sleeper: Any | None = None
) -> ClientPool:
    return ClientPool(config, transport=providers.transport, sleeper=sleeper or (lambda _s: None))


def make_harvester(
    config: Config, store: StateStore, providers: MockProviders, sleeper: Any | None = None
) -> tuple[Harvester, ClientPool]:
    clients = make_clients(config, providers, sleeper)
    return Harvester(config, store, clients), clients


@pytest.fixture
def harvester_factory(config: Config, store: StateStore):
    """Build a harvester bound to a given mock provider set."""
    created: list[ClientPool] = []

    def _factory(providers: MockProviders, *, sleeper: Any | None = None, cfg: Config | None = None):
        harvester, clients = make_harvester(cfg or config, store, providers, sleeper)
        created.append(clients)
        return harvester

    yield _factory
    for clients in created:
        clients.close()


@pytest.fixture
def query() -> OpenAlexQuery:
    return OpenAlexQuery(topic_id="T10159")
