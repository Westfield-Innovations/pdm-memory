# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest

from pdm_memory import Memory
from pdm_memory.plugins.observer import ObserverPlugin, ObserverRule


@pytest.fixture
def mem(tmp_path: Path) -> Memory:
    m = Memory(
        store=str(tmp_path / "observer.db"),
        user="test_user",
        autoload_plugins=True,
    )
    yield m
    m.close()


class TestObserverAutoload:
    def test_autoload_binds_mem_observer(self, mem: Memory) -> None:
        assert "observer" in mem.plugins
        assert isinstance(mem.observer, ObserverPlugin)
        assert mem.observer.bound_hooks == ["post_save"]


class TestObserverRules:
    def test_add_rule_fail_fast(self, mem: Memory) -> None:
        with pytest.raises(ValueError, match="cannot be empty"):
            mem.observer.add_rule("  ", threshold=90.0)
        mem.observer.add_rule("tank", threshold=95.0, tags=["danger"])
        with pytest.raises(ValueError, match="already registered"):
            mem.observer.add_rule("tank", threshold=90.0)
        with pytest.raises(ValueError, match="http"):
            mem.observer.add_rule(
                "bad-url",
                threshold=90.0,
                webhook_url="javascript:alert(1)",
            )

    def test_low_pressure_does_not_fire(self, mem: Memory) -> None:
        mem.observer.add_rule("high-p", threshold=95.0, tags=["danger"])
        mem.save("routine inspection complete", tags=["ops", "log", "daily"], p_magnitude=50.0)
        mem.observer.flush()
        assert mem.observer.fired == []

    def test_high_pressure_fires_immediately(self, mem: Memory) -> None:
        buf = io.StringIO()
        mem.observer._stream = buf
        mem.observer.add_rule("tank-critical", threshold=95.0, tags=["danger"])
        mid = mem.save(
            "CRITICAL: Pressure leak in Tank #5",
            tags=["danger", "tank", "leak"],
            p_magnitude=98.0,
        )
        mem.observer.flush()
        assert len(mem.observer.fired) == 1
        alert = mem.observer.fired[0]
        assert alert.memory_id == mid
        assert alert.rule.name == "tank-critical"
        assert "pressure" in alert.reasons
        assert "tags" in alert.reasons
        assert "PDM OBSERVER ALERT" in buf.getvalue()

    def test_hot_tag_fires_below_threshold(self, mem: Memory) -> None:
        mem.observer.add_rule("tag-only", threshold=99.0, tags=["deadline"])
        mem.save("submit report tonight", tags=["deadline", "ops", "report"], p_magnitude=40.0)
        mem.observer.flush()
        assert len(mem.observer.fired) == 1
        assert mem.observer.fired[0].reasons == ("tags",)

    def test_drawer_match(self, mem: Memory) -> None:
        mem.observer.add_rule(
            "steward",
            threshold=101.0,
            tags=(),
            drawer="stewardship",
        )
        mem.save("quiet fact", tags=["a", "b", "c"], p_magnitude=10.0, drawer="general")
        mem.save(
            "stewardship watch",
            tags=["a", "b", "c"],
            p_magnitude=10.0,
            drawer="stewardship",
        )
        mem.observer.flush()
        assert len(mem.observer.fired) == 1
        assert mem.observer.fired[0].reasons == ("drawer",)
        assert mem.observer.fired[0].drawer == "stewardship"

    def test_skips_plugin_drawer(self, mem: Memory) -> None:
        mem.observer.add_rule("any", threshold=0.0, tags=())
        mem.observer.plugin_save("internal config", tags=["plugin", "observer", "cfg"])
        mem.observer.flush()
        assert mem.observer.fired == []

    def test_remove_rule(self, mem: Memory) -> None:
        mem.observer.add_rule("gone", threshold=10.0, tags=())
        assert mem.observer.remove_rule("gone") is True
        mem.save("still quiet", tags=["a", "b", "c"], p_magnitude=80.0)
        mem.observer.flush()
        assert mem.observer.fired == []


class TestObserverWebhook:
    def test_webhook_posts_json(self, mem: Memory) -> None:
        class _Resp:
            def raise_for_status(self) -> None:
                return None

        class _Client:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            def post(self, url: str, json=None, headers=None):
                self.calls.append((url, dict(json or {})))
                return _Resp()

            def close(self) -> None:
                return None

        client = _Client()
        mem.observer._http_client = client
        mem.observer.add_rule(
            "hooked",
            threshold=90.0,
            tags=["danger"],
            webhook_url="https://hooks.example.test/pdm",
        )
        mem.save(
            "CRITICAL: Pressure leak in Tank #5",
            tags=["danger", "x", "y"],
            p_magnitude=98.0,
        )
        mem.observer.flush()
        assert len(client.calls) == 1
        url, payload = client.calls[0]
        assert url == "https://hooks.example.test/pdm"
        assert payload["event"] == "pdm.observer.alert"
        assert payload["rule"] == "hooked"
        assert "CRITICAL" in payload["text"]

    def test_dispatch_does_not_block_save(self, mem: Memory) -> None:
        class _Resp:
            def raise_for_status(self) -> None:
                return None

        class _SlowClient:
            def post(self, url: str, json=None, headers=None):
                time.sleep(0.25)
                return _Resp()

            def close(self) -> None:
                return None

        mem.observer._http_client = _SlowClient()
        mem.observer.add_rule(
            "slow-hook",
            threshold=90.0,
            tags=["danger"],
            webhook_url="https://hooks.example.test/slow",
        )
        t0 = time.perf_counter()
        mem.save(
            "CRITICAL: Pressure leak in Tank #5",
            tags=["danger", "x", "y"],
            p_magnitude=98.0,
        )
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.15
        mem.observer.flush(timeout=2.0)
        assert mem.observer.fired


class TestObserverRuleUnit:
    def test_matches_or_semantics(self) -> None:
        from pdm_memory.core.signature import SignatureRecord

        rule = ObserverRule(
            name="r",
            min_threshold=90.0,
            hot_tags=("danger",),
            drawer="stewardship",
        )
        low = SignatureRecord(
            compressed_fact="x",
            p_magnitude=10.0,
            intent_tags=["ok"],
            drawer_domain="general",
        )
        assert rule.matches(low) == ()
        hot = SignatureRecord(
            compressed_fact="x",
            p_magnitude=10.0,
            intent_tags=["DANGER"],
            drawer_domain="general",
        )
        assert rule.matches(hot) == ("tags",)
