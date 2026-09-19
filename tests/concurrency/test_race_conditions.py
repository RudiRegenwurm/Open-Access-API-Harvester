"""Concurrency and race-condition tests (MASTER_SPEC section 49).

Covers AC-020: two workers cannot successfully acquire the same logical document.
"""

from __future__ import annotations

import threading
from collections import Counter
from pathlib import Path

from harvester.identity import document_id_for_doi, sha256_file
from harvester.models import DocumentStatus, Source, SourceRecord
from harvester.state import StateStore
from harvester.util import utc_now_iso
from mocks import MockProviders, make_pdf_bytes, openalex_work

from tests.unit.test_state import make_document  # noqa: F401  (re-used builder)


# ===================================================================== AC-020


def test_ac020_only_one_worker_can_claim_a_document(tmp_path: Path):
    """AC-020: concurrent claims of one document produce exactly one winner."""
    with StateStore(tmp_path / "race.sqlite3") as store:
        store.create_run("run-1", query={}, config={})
        document, record = make_document("10.1234/contested")
        store.upsert_discovered_document("run-1", document, record)

        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(16)

        def worker(index: int) -> None:
            barrier.wait()
            claimed = store.claim_document(document.document_id, f"worker-{index}")
            with lock:
                results.append(claimed)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(results) == 1, "exactly one worker may win the claim"
        assert len(results) == 16
        assert store.get_document_status(document.document_id) is DocumentStatus.ACQUIRING


def test_many_processes_can_open_one_fresh_database_simultaneously(tmp_path: Path):
    """Opening the state store must not fail when several harvesters start at once.

    Regression: switching a database into WAL mode needs an exclusive lock and SQLite
    refuses it immediately rather than honouring ``busy_timeout``, so concurrent
    openers used to die with "database is locked" before doing any work.
    """
    path = tmp_path / "concurrent-open.sqlite3"
    stores: list[StateStore] = []
    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(16, timeout=60)

    def opener(index: int) -> None:
        try:
            barrier.wait()
            store = StateStore(path)
            store.create_run(f"run-{index}", query={}, config={})
            with lock:
                stores.append(store)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=opener, args=(i,)) for i in range(16)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        assert errors == [], f"opening the store concurrently failed: {errors}"
        assert len(stores) == 16
        assert len(stores[0].list_runs(50)) == 16
    finally:
        for store in stores:
            store.close()


def test_ac020_claims_are_exclusive_across_separate_store_instances(tmp_path: Path):
    """Two *processes* (separate connections/stores) also cannot both claim."""
    path = tmp_path / "race-processes.sqlite3"
    with StateStore(path) as setup:
        setup.create_run("run-1", query={}, config={})
        document, record = make_document("10.1234/cross-process")
        setup.upsert_discovered_document("run-1", document, record)

    stores = [StateStore(path) for _ in range(6)]
    try:
        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(len(stores))

        def worker(index: int) -> None:
            barrier.wait()
            claimed = stores[index].claim_document(document.document_id, f"proc-{index}")
            with lock:
                results.append(claimed)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(stores))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(results) == 1
    finally:
        for store in stores:
            store.close()


def test_ac020_concurrent_harvest_downloads_each_document_exactly_once(
    config, store, harvester_factory, query
):
    """A bounded worker pool must not double-acquire any document."""
    config.downloads.concurrency = 8
    count = 12
    providers = MockProviders(
        works=[openalex_work(i) for i in range(1, count + 1)],
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, count + 1)},
    )
    result = harvester_factory(providers).harvest(query)

    assert result.stats.completed == count
    for index in range(1, count + 1):
        assert providers.count(f"file:/W{2000000 + index}.pdf") == 1

    root = Path(config.storage_root)
    assert len(list(root.glob("*.pdf"))) == count
    assert len(list(root.glob("*.json"))) == count
    assert list(root.glob("*.part")) == []


def test_concurrent_run_produces_the_same_corpus_as_a_serial_run(
    tmp_path, config, store, harvester_factory, query
):
    """Determinism: concurrency must not change the resulting corpus."""
    count = 8
    config.downloads.concurrency = 1
    serial_providers = MockProviders(
        works=[openalex_work(i) for i in range(1, count + 1)],
        files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, count + 1)},
    )
    harvester_factory(serial_providers).harvest(query)
    serial = {p.name: sha256_file(p) for p in sorted(Path(config.storage_root).glob("*.pdf"))}

    # Second, independent corpus and state, harvested concurrently.
    parallel_config = config
    parallel_config.storage_root = Path(tmp_path) / "corpus-parallel"
    parallel_config.state_db = Path(tmp_path) / "state-parallel.sqlite3"
    parallel_config.downloads.concurrency = 8
    with StateStore(parallel_config.state_db) as parallel_store:
        from harvester.http import ClientPool
        from harvester.orchestrator import Harvester

        providers = MockProviders(
            works=[openalex_work(i) for i in range(1, count + 1)],
            files={f"/W{2000000 + i}.pdf": make_pdf_bytes() for i in range(1, count + 1)},
        )
        with ClientPool(
            parallel_config, transport=providers.transport, sleeper=lambda _s: None
        ) as clients:
            Harvester(parallel_config, parallel_store, clients).harvest(query)

    parallel = {
        p.name: sha256_file(p) for p in sorted(Path(parallel_config.storage_root).glob("*.pdf"))
    }
    assert serial == parallel


def test_concurrent_state_writes_stay_consistent(config, store, harvester_factory, query):
    """Many threads writing attempts/failures must not corrupt the database."""
    from harvester.errors import ErrorCategory, Failure

    store.create_run("run-stress", query={}, config={})
    errors: list[BaseException] = []

    def writer(index: int) -> None:
        try:
            for step in range(25):
                store.record_attempt(
                    run_id="run-stress",
                    document_id=f"doc-{index}",
                    operation="acquire.pdf",
                    attempt_no=step + 1,
                    ok=step % 2 == 0,
                )
                store.record_failure(
                    "run-stress",
                    Failure(
                        category=ErrorCategory.NETWORK_ERROR,
                        message=f"boom {index}-{step}",
                        operation="acquire.pdf",
                        document_id=f"doc-{index}",
                    ),
                )
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(store.failures_for_run("run-stress")) == 8 * 25
    counts = Counter(
        row["document_id"]
        for row in store.connection.execute(
            "SELECT document_id FROM attempts WHERE run_id = 'run-stress'"
        ).fetchall()
    )
    assert set(counts.values()) == {25}


def test_worker_that_loses_the_claim_does_no_work(config, store, harvester_factory, query):
    """The loser of a claim race must not download or write anything."""
    providers = MockProviders(
        works=[openalex_work(1)], files={"/W2000001.pdf": make_pdf_bytes()}
    )
    harvester = harvester_factory(providers)
    document_id = document_id_for_doi("10.1234/mock.0001")

    # Discover first, then pre-claim the document as if another process held it.
    harvester.harvest(query, dry_run=True)
    assert store.claim_document(document_id, "other-process") is True

    result = harvester_factory(providers).harvest(query)
    assert result.stats.attempted == 0
    assert providers.count("file:/W2000001.pdf") == 0
    assert list(Path(config.storage_root).glob("*.pdf")) == []
