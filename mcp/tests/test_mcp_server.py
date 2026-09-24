import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from harvester.config import Config
from notanda_mcp.server import EvidenceStore, Experiment


def make(tmp_path, handler):
    config = Config()
    config.openalex.api_key = "test-secret-never-persist"
    return Experiment(tmp_path, config, lambda: httpx.MockTransport(handler))


def work():
    return {
        "id": "https://openalex.org/W1",
        "doi": "https://doi.org/10.1234/example",
        "title": "Fixture only",
        "publication_year": 2024,
        "open_access": {"is_oa": True},
    }


def test_success_restart_and_corruption(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.params["per-page"] == "2"
        return httpx.Response(200, json={"results": [work()], "meta": {"count": 1}})

    result = make(tmp_path, handler).search("science", 2)
    assert result["status"] == "ok", result
    assert len(result["results"]) == 1
    assert len(calls) == 1
    folder = tmp_path / result["evidence_id"]
    request = json.loads((folder / "request.json").read_bytes())
    assert request["source_experiment_commit"] == (
        "366bc2d4827aa915e2e58a82cd567163ed63d4be"
    )
    assert request["notanda_core_version"] == "1.3.1"
    assert EvidenceStore(tmp_path).get(result["evidence_id"]) == result
    assert "test-secret-never-persist" not in "".join(
        path.read_text() for path in folder.glob("*.json")
    )
    (folder / "response.json").write_text("{}")
    assert EvidenceStore(tmp_path).get(result["evidence_id"])["error"] == (
        "integrity_failure"
    )


@pytest.mark.parametrize(
    "query,limit",
    [("", 1), ("x", 0), ("x", 11), ("x", True), (None, 5), ("x", "5")],
)
def test_invalid_persisted_without_provider(tmp_path, query, limit):
    def forbidden(request):
        pytest.fail("provider must not run")

    experiment = make(tmp_path, forbidden)
    result = experiment.search(query, limit)
    assert result["error"] == "invalid_arguments"
    assert experiment.store.get(result["evidence_id"]) == result


@pytest.mark.parametrize("status", [401, 429, 500])
def test_provider_error_once_and_redaction(tmp_path, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "test-secret-never-persist"})

    experiment = make(tmp_path, handler)
    result = experiment.search("science")
    assert result["error"] == "provider_error"
    assert len(calls) == 1
    assert experiment.store.get(result["evidence_id"]) == result
    assert "test-secret-never-persist" not in "".join(
        path.read_text() for path in tmp_path.rglob("*.json")
    )


def test_timeout(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("test-secret-never-persist")

    experiment = make(tmp_path, handler)
    result = experiment.search("science")
    assert result["error"] == "provider_error"
    provider = json.loads(
        (tmp_path / result["evidence_id"] / "provider.json").read_bytes()
    )
    assert provider["attempts"][0]["error_type"] == "ReadTimeout"


def test_empty_is_success(tmp_path):
    result = make(
        tmp_path,
        lambda request: httpx.Response(
            200, json={"results": [], "meta": {"count": 0}}
        ),
    ).search("x")
    assert result["status"] == "ok" and result["results"] == []


def test_storage_failure_before_network(tmp_path):
    root = tmp_path / "file"
    root.write_text("not a directory")

    def forbidden(request):
        pytest.fail("provider must not run")

    result = make(root, forbidden).search("x")
    assert result["error"] == "evidence_storage_unavailable"


def test_completion_write_failure_never_returns_success(tmp_path, monkeypatch):
    experiment = make(
        tmp_path,
        lambda request: httpx.Response(
            200, json={"results": [], "meta": {"count": 0}}
        ),
    )

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(experiment.store, "finish", fail)
    result = experiment.search("science")
    assert result["error"] == "evidence_storage_unavailable"
    assert experiment.store.get(result["evidence_id"])["status"] == "incomplete"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "http://localhost/private"}),
        httpx.Response(200, content=b"not JSON"),
        httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1)),
    ],
)
def test_bad_responses_fail_closed(tmp_path, response):
    calls = []

    def handler(request):
        calls.append(request)
        return response

    result = make(tmp_path, handler).search("science")
    assert result["error"] == "provider_error"
    assert len(calls) == 1


def test_independent_verifier_detects_tampering(tmp_path):
    import shutil

    result = make(
        tmp_path / "source",
        lambda request: httpx.Response(
            200, json={"results": [], "meta": {"count": 0}}
        ),
    ).search("x")
    folder = tmp_path / "copy" / result["evidence_id"]
    shutil.copytree(tmp_path / "source" / result["evidence_id"], folder)
    verifier = Path(__file__).resolve().parents[1] / "tools" / "verify_mcp_evidence.py"
    assert (
        subprocess.run(
            [sys.executable, "-S", str(verifier), str(folder)], capture_output=True
        ).returncode
        == 0
    )
    (folder / "provider.json").write_text("{}")
    assert (
        subprocess.run(
            [sys.executable, "-S", str(verifier), str(folder)], capture_output=True
        ).returncode
        == 1
    )


def test_process_death_is_incomplete(tmp_path):
    script = """
import os,sys,httpx
from pathlib import Path
from harvester.config import Config
from notanda_mcp.server import Experiment
c=Config();c.openalex.allow_keyless=True
Experiment(Path(sys.argv[1]),c,lambda:httpx.MockTransport(lambda r:os._exit(23))).search('x')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True
    )
    assert result.returncode == 23
    (folder,) = tmp_path.iterdir()
    assert (folder / "request.json").exists()
    assert EvidenceStore(tmp_path).get(folder.name)["status"] == "incomplete"


def test_stdio_real_client_two_processes(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # Dependency injection is confined to this test launcher, never a production switch.
    script = """
import httpx
import notanda_mcp.server as m
Original=m.Experiment
class Fixture(Original):
 def __init__(self, root, config):
  config.openalex.allow_keyless=True
  super().__init__(root,config,lambda:httpx.MockTransport(lambda r:httpx.Response(200,json={'results':[],'meta':{'count':0}})))
m.Experiment=Fixture
m.main()
"""

    async def call(arguments, tool, fixture):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=(
                ["-c", script] if fixture else ["-m", "notanda_mcp.server"]
            )
            + ["--evidence-dir", str(tmp_path)],
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("HARVESTER_")
            },
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert {item.name for item in (await session.list_tools()).tools} == {
                    "get_evidence",
                    "search_literature",
                }
                result = await session.call_tool(tool, arguments)
                assert not result.isError, result
                return result.structuredContent

    first = asyncio.run(call({"query": "fixture"}, "search_literature", True))
    assert first["status"] == "ok"
    second = asyncio.run(
        call({"evidence_id": first["evidence_id"]}, "get_evidence", False)
    )
    assert first == second == json.loads(
        (tmp_path / first["evidence_id"] / "response.json").read_bytes()
    )
    invalid = asyncio.run(
        call({"query": "fixture", "limit": 11}, "search_literature", True)
    )
    assert invalid["error"] == "invalid_arguments"
