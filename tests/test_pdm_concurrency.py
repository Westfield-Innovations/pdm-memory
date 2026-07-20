"""
Postgres storage seam — concurrent reinforce() must not lose retrieval_count updates.

10 workers × 50 reinforces = exactly 500 retrieval_count, or we have lost updates.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

from pdm_memory import Memory

WORKERS = 10
REINFORCES_PER_WORKER = 50
EXPECTED_COUNT = WORKERS * REINFORCES_PER_WORKER


def _postgres_url() -> str | None:
    return os.environ.get("PDM_TEST_POSTGRES_URL")


@pytest.fixture(scope="module")
def postgres_url() -> str:
    url = _postgres_url()
    if not url:
        pytest.skip(
            "PDM_TEST_POSTGRES_URL is not set — skipping Postgres concurrency stress test. "
            "Example: export PDM_TEST_POSTGRES_URL=postgresql://admin@localhost:5432/pdm_smoke"
        )
    psycopg = pytest.importorskip("psycopg")
    del psycopg  # only verify driver is installed
    return url


@pytest.fixture
def stress_user(postgres_url: str) -> str:
    return f"concurrency_{uuid.uuid4().hex[:12]}"


@pytest.fixture(scope="module")
def postgres_driver(postgres_url: str):
    """Single driver — schema/migrations run once, thread-local connections per worker."""
    from pdm_memory.storage.postgres_driver import PostgresDriver

    driver = PostgresDriver(dsn=postgres_url)
    yield driver
    driver.close()


def _worker(
    driver,
    user: str,
    target_id: str,
    barrier: threading.Barrier,
    errors: list[BaseException],
) -> None:
    mem = Memory(storage=driver, user=user)
    try:
        barrier.wait(timeout=30)
        for _ in range(REINFORCES_PER_WORKER):
            mem.reinforce(target_id, coupling_score=0.5)
    except BaseException as exc:
        errors.append(exc)
    finally:
        mem.close()


class TestPostgresConcurrentReinforce:
    def test_ten_workers_fifty_reinforces_each(
        self,
        postgres_driver,
        stress_user: str,
    ) -> None:
        setup = Memory(storage=postgres_driver, user=stress_user)
        try:
            target_id = setup.save(
                "Concurrent reinforce stress target",
                tags=["concurrency", "postgres", "stress"],
                p_magnitude=40.0,
            )
            initial = setup._storage.get(target_id, user=stress_user)
            assert initial is not None
            assert initial.retrieval_count == 0
        finally:
            setup.close()

        barrier = threading.Barrier(WORKERS + 1)
        errors: list[BaseException] = []
        threads = [
            threading.Thread(
                target=_worker,
                args=(postgres_driver, stress_user, target_id, barrier, errors),
                name=f"reinforce-worker-{i}",
            )
            for i in range(WORKERS)
        ]

        for t in threads:
            t.start()

        barrier.wait(timeout=30)

        for t in threads:
            t.join(timeout=120)

        assert not errors, f"worker failures: {errors!r}"
        assert all(not t.is_alive() for t in threads)

        verify = Memory(storage=postgres_driver, user=stress_user)
        try:
            rec = verify._storage.get(target_id, user=stress_user)
            assert rec is not None, "stress target missing after concurrent reinforces"
            assert rec.retrieval_count == EXPECTED_COUNT, (
                f"Lost updates detected: retrieval_count={rec.retrieval_count}, "
                f"expected={EXPECTED_COUNT}. "
                "PostgresDriver must use SELECT FOR UPDATE or atomic "
                "retrieval_count = retrieval_count + 1."
            )
        finally:
            verify.close()
