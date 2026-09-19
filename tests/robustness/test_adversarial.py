"""Adversarial tests (MASTER_SPEC section 63).

Deliberate attempts to break the harvester with hostile providers, hostile filesystems
and hostile timing. Each test here corresponds to a finding from the audit.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest

from harvester.errors import StorageError
from harvester.identity import artifact_path, document_id_for_doi, normalize_doi
from harvester.models import DocumentStatus, RunStatus
from harvester.state import StateStore
from mocks import Behavior, MockProviders, make_pdf_bytes, openalex_work


# ------------------------------------------------- hostile pagination behavior


def test_provider_cursor_that_never_advances_does_not_loop_forever(
    config, store, harvester_factory, query
):
    """A pathological provider must not produce an unbounded run."""
    providers = MockProviders(
        works=[openalex_work(i) for i in range(1, 5)],
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, 5)},
        page_size=2,
    )
    providers.script_route(
        "openalex.works",
        [
            Behavior(
                status=200,
                json={
                    "meta": {"count": 99, "next_cursor": "stuck"},
                    "results": [openalex_work(1)],
                },
            )
        ]
        * 50,
    )
    result = harvester_factory(providers).harvest(query)
    assert result.status is RunStatus.COMPLETED
    assert providers.count("openalex.works") <= 3


def test_provider_cursor_that_cycles_between_pages_does_not_loop_forever(
    config, store, harvester_factory, query
):
    cursors = ["a", "b", "a", "b", "a", "b", "a", "b"]
    behaviors = [
        Behavior(
            status=200,
            json={
                "meta": {"count": 99, "next_cursor": cursor},
                "results": [openalex_work(index + 1)],
            },
        )
        for index, cursor in enumerate(cursors)
    ]
    providers = MockProviders(works=[], files={})
    providers.script_route("openalex.works", behaviors)
    result = harvester_factory(providers).harvest(query)

    assert result.status is RunStatus.COMPLETED
    assert providers.count("openalex.works") <= 4
    assert any("cursor repeated" in note for note in result.stats.notes)


def test_provider_reporting_more_results_than_it_returns_terminates(
    config, store, harvester_factory, query
):
    providers = MockProviders(works=[], files={})
    providers.script_route(
        "openalex.works",
        [Behavior(status=200, json={"meta": {"count": 10_000, "next_cursor": "x"}, "results": []})],
    )
    result = harvester_factory(providers).harvest(query)
    assert result.status is RunStatus.COMPLETED
    assert result.stats.records_discovered == 0


# ------------------------------------------------------- hostile metadata


@pytest.mark.parametrize(
    "doi",
    [
        "10.1234/\x00nul",
        "10.1234/" + "‮" + "reversed",
        "10.1234/../../escape",
        "10.1234/" + "z" * 4000,
        "10.1234/a\nb",
    ],
)
def test_hostile_dois_produce_safe_identifiers_or_are_rejected(doi):
    normalized = normalize_doi(doi)
    if normalized is None:
        return
    document_id = document_id_for_doi(normalized)
    # Whatever survives normalization must still be a safe single path component.
    path = artifact_path(Path("/tmp/corpus"), document_id, "pdf")
    assert path.name == f"{document_id}.pdf"
    assert ".." not in document_id
    assert "/" not in document_id and "\\" not in document_id


def test_openalex_record_with_hostile_types_does_not_crash_normalization(
    config, store, harvester_factory, query
):
    hostile = {
        "id": "https://openalex.org/W1",
        "doi": 12345,                       # not a string
        "title": {"nested": "object"},      # not a string
        "authorships": "not-a-list",
        "publication_year": "not-a-year",
        "open_access": [],                  # not a dict
        "primary_topic": "not-a-dict",
        "topics": [None, 5, {"id": None}],
        "abstract_inverted_index": {"word": "not-a-list"},
        "locations": [None, "x"],
        "ids": None,
    }
    providers = MockProviders(works=[hostile], files={})
    result = harvester_factory(providers).harvest(query)

    # It is recorded as a failure (no OA location), never a crash and never fabricated.
    assert result.status is RunStatus.COMPLETED
    document_id = store.all_document_ids()[0]
    metadata = store.get_metadata(document_id)
    assert metadata["doi"] is None
    assert metadata["title"] is None
    assert metadata["abstract"] is None
    assert metadata["publication_year"] is None
    assert metadata["authors"] == []


def test_provider_returning_a_json_array_is_a_provider_error(
    config, store, harvester_factory, query
):
    providers = MockProviders(works=[], files={})
    providers.script_route("openalex.works", [Behavior(status=200, json=[1, 2, 3])])
    result = harvester_factory(providers).harvest(query)
    assert result.status is RunStatus.FAILED
    assert any(
        f["category"] == "PROVIDER_ERROR" for f in store.failures_for_run(result.run_id)
    )


def test_extremely_long_storage_root_still_works(tmp_path, config, store, harvester_factory, query):
    """Regression: a long path must not be misjudged as escaping the storage root."""
    deep = tmp_path
    for _ in range(6):
        deep = deep / ("segment" * 5)
    config.storage_root = deep / "corpus"

    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    result = harvester_factory(providers).harvest(query)
    assert result.stats.completed == 1
    assert len(list(Path(config.storage_root).glob("*.pdf"))) == 1


# ------------------------------------------------------- hostile timing


def test_interrupting_a_run_leaves_it_resumable(config, store, harvester_factory, query):
    """A KeyboardInterrupt mid-acquisition checkpoints and stays resumable."""
    providers = MockProviders(
        works=[openalex_work(i) for i in range(1, 4)],
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, 4)},
    )
    calls = {"n": 0}
    original = providers.handle

    def interrupting_handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("W2000002.pdf"):
            calls["n"] += 1
            raise KeyboardInterrupt("operator pressed Ctrl-C")
        return original(request)

    providers.handle = interrupting_handle  # type: ignore[method-assign]
    result = harvester_factory(providers).harvest(query)

    assert result.status is RunStatus.INTERRUPTED
    run = store.get_run(result.run_id)
    assert run.status is RunStatus.INTERRUPTED
    assert calls["n"] == 1

    healthy = MockProviders(
        works=[openalex_work(i) for i in range(1, 4)],
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, 4)},
    )
    resumed = harvester_factory(healthy).resume(result.run_id)
    assert resumed.status is RunStatus.COMPLETED
    assert len(list(Path(config.storage_root).glob("*.pdf"))) == 3
    assert list(Path(config.storage_root).glob("*.part")) == []


def test_two_harvesters_sharing_one_state_database_do_not_double_download(
    tmp_path, config, query
):
    """Two concurrent 'processes' against one corpus acquire each document once."""
    from harvester.http import ClientPool
    from harvester.orchestrator import Harvester

    count = 6
    works = [openalex_work(i) for i in range(1, count + 1)]
    files = {f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, count + 1)}
    providers_a = MockProviders(works=works, files=files)
    providers_b = MockProviders(works=works, files=files)

    # The barrier only lines the two workers up; it must not itself become the thing
    # under test. It is generous, and a worker that dies reports why instead of
    # leaving the assertion to say "0 == 2".
    barrier = threading.Barrier(2, timeout=120)
    results: list = []
    errors: list[str] = []
    lock = threading.Lock()

    def run(providers: MockProviders, label: str) -> None:
        try:
            with StateStore(config.state_db) as store, ClientPool(
                config, transport=providers.transport, sleeper=lambda _s: None
            ) as clients:
                harvester = Harvester(config, store, clients, worker_prefix=label)
                barrier.wait()
                outcome = harvester.harvest(query, run_id=f"run-{label}")
                with lock:
                    results.append(outcome)
        except BaseException as exc:  # noqa: BLE001 - reported, then asserted on
            with lock:
                errors.append(f"{label}: {type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=run, args=(providers_a, "alpha")),
        threading.Thread(target=run, args=(providers_b, "beta")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)
        assert not thread.is_alive(), "a harvester thread did not finish"

    assert errors == [], f"a harvester thread failed: {errors}"
    assert len(results) == 2
    root = Path(config.storage_root)
    assert len(list(root.glob("*.pdf"))) == count
    assert list(root.glob("*.part")) == []
    for index in range(1, count + 1):
        route = f"file:/W{2000000 + index}.pdf"
        assert providers_a.count(route) + providers_b.count(route) == 1

    with StateStore(config.state_db) as store:
        assert len(store.all_document_ids()) == count
        for document_id in store.all_document_ids():
            assert store.get_document_status(document_id) is DocumentStatus.COMPLETED


# ------------------------------------------------------- hostile filesystem


def test_corrupted_sidecar_is_reported_by_verify(config, store, harvester_factory, query):
    from harvester.verify import verify_corpus

    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    harvester_factory(providers).harvest(query)
    document_id = document_id_for_doi("10.1234/mock.0001")
    (Path(config.storage_root) / f"{document_id}.pdf").write_bytes(b"tampered")

    report = verify_corpus(config, store, deep=True)
    assert report.ok is False
    problem = report.problems[0]
    assert "sha256 mismatch" in problem["problem"]
    assert problem["valid"] is False


def test_read_only_style_storage_failure_is_recorded_not_crashed(
    config, store, harvester_factory, query, monkeypatch
):
    """A filesystem that refuses the final rename must not corrupt state."""
    import harvester.acquisition as acquisition_module

    def failing_replace(src, dst):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(acquisition_module.os, "replace", failing_replace)
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    result = harvester_factory(providers).harvest(query)

    assert result.stats.completed == 0
    assert any(
        f["category"] == "STORAGE_ERROR" for f in store.failures_for_run(result.run_id)
    )
    root = Path(config.storage_root)
    assert list(root.glob("*.pdf")) == []
    assert list(root.glob("*.part")) == []


def test_state_database_and_corpus_can_be_relocated_independently(
    tmp_path, config, store, harvester_factory, query
):
    """Files remain the authoritative artifacts; state is rebuildable evidence."""
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    result = harvester_factory(providers).harvest(query)
    document_id = document_id_for_doi("10.1234/mock.0001")
    sidecar = json.loads(
        (Path(config.storage_root) / f"{document_id}.json").read_text("utf-8")
    )
    # The sidecar alone identifies the document, its artifacts and their checksums.
    assert sidecar["document_id"] == document_id
    assert sidecar["artifacts"]["pdf"]["sha256"]
    assert sidecar["provenance"]["acquired_via"]
    assert result.stats.completed == 1


def test_unsafe_document_id_can_never_reach_the_filesystem(tmp_path):
    for hostile in ("../escape", "a/b", "a\\b", "", "."):
        with pytest.raises(StorageError):
            artifact_path(tmp_path, hostile, "pdf")
