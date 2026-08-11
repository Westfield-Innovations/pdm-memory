# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

from __future__ import annotations

import logging

import pytest

from pdm_memory import Memory, PostRecallContext


@pytest.fixture
def mem(tmp_path):
    m = Memory(store=str(tmp_path / "test_hooks.db"), user="test_user")
    yield m
    m.close()


class TestInternalHooks:
    def test_pre_and_post_save_hooks(self, mem: Memory) -> None:
        pre_calls: list[str] = []
        post_calls: list[str] = []

        def pre_save(sig):
            pre_calls.append(sig.id[:8])
            sig.p_magnitude = 42.0
            sig.metadata = {"hooked": True}
            return sig

        def post_save(sig, memory_id: str) -> None:
            post_calls.append(memory_id)
            assert sig.p_magnitude == 42.0

        mem.add_hook("pre_save", pre_save)
        mem.add_hook("post_save", post_save)

        mid = mem.save("alpha fact", tags=["alpha", "beta", "gamma"], p_magnitude=10.0)
        assert pre_calls
        assert post_calls == [mid]

        rec = mem._storage.get(mid, user="test_user")
        assert rec is not None
        assert rec.p_magnitude == pytest.approx(42.0)
        assert rec.metadata.get("hooked") is True

    def test_post_recall_hook_runs_after_reinforce(self, mem: Memory) -> None:
        hooks: list[PostRecallContext] = []

        id_a = mem.save(
            "alpha beta gamma fact",
            tags=["alpha", "beta", "gamma"],
            drawer="work",
            p_magnitude=80.0,
        )
        id_b = mem.save(
            "delta epsilon zeta fact",
            tags=["delta", "epsilon", "zeta"],
            drawer="other",
            p_magnitude=70.0,
        )

        before_a = mem._storage.get(id_a, user="test_user").retrieval_count
        before_b = mem._storage.get(id_b, user="test_user").retrieval_count

        def post_recall(ctx: PostRecallContext) -> None:
            hooks.append(ctx)
            assert isinstance(ctx, PostRecallContext)
            assert ctx.query == "alpha beta gamma"
            assert ctx["query"] == "alpha beta gamma"
            assert ctx.reinforced is True
            assert ctx.source == "recall"
            assert len(ctx.hits) == 1

            hit_id = ctx.hits[0].id
            rec = mem._storage.get(hit_id, user="test_user")
            assert rec is not None

            expected_before = before_a if hit_id == id_a else before_b
            assert rec.retrieval_count == expected_before + 1

        mem.add_hook("post_recall", post_recall)

        hits = mem.recall(
            "alpha beta gamma",
            k=1,
            reinforce=True,
            search_cost=1.0,
        )
        assert len(hits) == 1
        assert hits[0].id == id_a

        assert len(hooks) == 1
        assert hooks[0].k == 1
        assert hooks[0]["k"] == 1

    def test_post_recall_hook_called_on_empty_db(self, mem: Memory) -> None:
        hooks: list[PostRecallContext] = []

        def post_recall(ctx: PostRecallContext) -> None:
            hooks.append(ctx)

        mem.add_hook("post_recall", post_recall)

        hits = mem.recall(
            "anything",
            k=3,
            reinforce=True,
            search_cost=1.0,
        )
        assert hits == []
        assert len(hooks) == 1
        assert hooks[0].hits == ()
        assert hooks[0].reinforced is False
        assert hooks[0].source == "recall"

    def test_post_save_exception_isolated(self, mem: Memory, caplog) -> None:
        seen: list[str] = []

        def boom(_sig, _memory_id: str) -> None:
            raise RuntimeError("post_save exploded")

        def ok(_sig, memory_id: str) -> None:
            seen.append(memory_id)

        mem.add_hook("post_save", boom, priority=10)
        mem.add_hook("post_save", ok, priority=20)

        with caplog.at_level(logging.ERROR):
            mid = mem.save("survives post hook", tags=["a", "b", "c"])

        assert mid
        assert seen == [mid]
        assert mem.get(mid) is not None
        assert any("post_save hook failed" in r.message for r in caplog.records)

    def test_post_recall_exception_isolated(self, mem: Memory, caplog) -> None:
        seen: list[int] = []

        mem.save(
            "alpha beta gamma recall isolation",
            tags=["alpha", "beta", "gamma"],
            p_magnitude=90.0,
        )

        def boom(_ctx: PostRecallContext) -> None:
            raise RuntimeError("post_recall exploded")

        def ok(ctx: PostRecallContext) -> None:
            seen.append(len(ctx.hits))

        mem.add_hook("post_recall", boom, priority=10)
        mem.add_hook("post_recall", ok, priority=20)

        with caplog.at_level(logging.ERROR):
            hits = mem.recall("alpha beta gamma", k=3, reinforce=False, search_cost=1.0)

        assert hits
        assert seen == [len(hits)]
        assert any("post_recall hook failed" in r.message for r in caplog.records)

    def test_pre_save_none_vetoes_save(self, mem: Memory) -> None:
        from pdm_memory import IntegrityBlock

        def veto(_sig):
            return None

        mem.add_hook("pre_save", veto)
        with pytest.raises(IntegrityBlock, match="vetoed"):
            mem.save("should not persist", tags=["a", "b", "c"])
        assert mem.count() == 0

    def test_pre_save_false_vetoes_save(self, mem: Memory) -> None:
        from pdm_memory import IntegrityBlock

        def veto(_sig):
            return False

        mem.add_hook("pre_save", veto)
        with pytest.raises(IntegrityBlock, match="vetoed"):
            mem.save("blocked by false", tags=["a", "b", "c"])
        assert mem.count() == 0

    def test_pre_save_raises_integrity_block(self, mem: Memory) -> None:
        from pdm_memory import IntegrityBlock

        def guard(sig):
            if "malware" in (sig.compressed_fact or "").lower():
                raise IntegrityBlock("GuardDog blocked harmful content")
            return sig

        mem.add_hook("pre_save", guard)
        with pytest.raises(IntegrityBlock, match="GuardDog"):
            mem.save("inject malware payload", tags=["a", "b", "c"])
        assert mem.count() == 0

        ok = mem.save("clean fact about widgets", tags=["a", "b", "c"])
        assert ok
