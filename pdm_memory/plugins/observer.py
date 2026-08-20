# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# AUTHORIZED EXTENSION POINT (Westfield OS) — implementing this interface
# (e.g. BasePDMPlugin) is PERMITTED. Core software modification remains
# prohibited without a commercial license from Westfield Innovations LLC.

"""
Observer plugin — proactive alerts on high-pressure ingestion.

``post_save`` evaluates registered :class:`ObserverRule` objects and dispatches
console / webhook alerts on a background worker so ``Memory.save`` does not wait
on I/O.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, TextIO
from urllib.parse import urlparse

from pdm_memory.core.signature import SignatureRecord
from pdm_memory.plugins.base import PLUGIN_DRAWER_PREFIX, BasePDMPlugin

logger = logging.getLogger(__name__)

DEFAULT_MIN_THRESHOLD = 90.0
DEFAULT_HOT_TAGS: tuple[str, ...] = ("danger", "critical", "deadline")
_WEBHOOK_TIMEOUT_S = 5.0
_FLUSH_TIMEOUT_S = 5.0
_QUEUE_MAX = 256
_ANSI_RED_BOLD = "\033[1;31m"
_ANSI_RESET = "\033[0m"


def _normalize_tags(tags: Sequence[str] | None) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags or ():
        token = str(raw).strip().lower()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return tuple(out)


def _validate_webhook_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            f"webhook_url must be an absolute http(s) URL, got {url!r}"
        )
    return url


@dataclass(frozen=True, slots=True)
class ObserverRule:
    """Match-any rule: pressure OR hot tags OR drawer."""

    name: str
    min_threshold: float = DEFAULT_MIN_THRESHOLD
    hot_tags: tuple[str, ...] = DEFAULT_HOT_TAGS
    drawer: str | None = None
    webhook_url: str | None = None

    def matches(self, sig: SignatureRecord) -> tuple[str, ...]:
        """Return reason tokens if the signature trips this rule."""
        reasons: list[str] = []
        if float(sig.p_magnitude) >= float(self.min_threshold):
            reasons.append("pressure")
        sig_tags = {str(t).strip().lower() for t in (sig.intent_tags or ()) if t}
        if self.hot_tags and sig_tags.intersection(self.hot_tags):
            reasons.append("tags")
        drawer = (self.drawer or "").strip()
        if drawer and (sig.drawer_domain or "").strip() == drawer:
            reasons.append("drawer")
        return tuple(reasons)


@dataclass(frozen=True, slots=True)
class ObserverAlert:
    """Immutable snapshot queued for background dispatch."""

    rule: ObserverRule
    memory_id: str
    text: str
    p_magnitude: float
    tags: tuple[str, ...]
    drawer: str
    domain: str
    reasons: tuple[str, ...]
    user: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)

    def webhook_payload(self) -> dict[str, Any]:
        return {
            "event": "pdm.observer.alert",
            "rule": self.rule.name,
            "memory_id": self.memory_id,
            "user": self.user,
            "p_magnitude": self.p_magnitude,
            "tags": list(self.tags),
            "drawer": self.drawer,
            "domain": self.domain,
            "text": self.text,
            "matched": list(self.reasons),
            "webhook_url": self.rule.webhook_url,
        }


class ObserverPlugin(BasePDMPlugin):
    """Builtin proactive monitor. Autoloaded as ``mem.observer``."""

    name = "observer"
    version = "1.0.0"
    autoload = True
    priority = 80

    def __init__(self) -> None:
        super().__init__()
        self.hooks = {"post_save": self._on_post_save}
        self._rules: dict[str, ObserverRule] = {}
        self._rules_lock = threading.Lock()
        self._queue: queue.Queue[ObserverAlert | None] = queue.Queue(
            maxsize=_QUEUE_MAX
        )
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._http_client: Any = None
        self._stream: TextIO = sys.stdout
        #: Test seam — last alerts dispatched (after worker runs).
        self.fired: list[ObserverAlert] = []
        self._fired_lock = threading.Lock()

    def on_install(self) -> None:
        self._stop.clear()
        worker = threading.Thread(
            target=self._run_worker,
            name="pdm-observer-dispatch",
            daemon=True,
        )
        self._worker = worker
        worker.start()
        logger.info("[PDM] observer worker started")

    def on_uninstall(self) -> None:
        self.flush(timeout=_FLUSH_TIMEOUT_S)
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            logger.warning("[PDM] observer queue full on shutdown; dropping sentinel")
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=_FLUSH_TIMEOUT_S)
        self._worker = None
        self._close_http_client()

    def add_rule(
        self,
        name: str,
        threshold: float = DEFAULT_MIN_THRESHOLD,
        tags: Sequence[str] | None = DEFAULT_HOT_TAGS,
        webhook_url: str | None = None,
        *,
        drawer: str | None = None,
    ) -> ObserverRule:
        """
        Register an active rule.

        A signature matches if **any** of: ``p_magnitude >= threshold``,
        any hot tag overlap, or ``drawer`` equals ``sig.drawer_domain``.
        """
        key = str(name).strip()
        if not key:
            raise ValueError("Observer rule name cannot be empty")
        try:
            min_threshold = float(threshold)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"threshold must be a number, got {threshold!r}") from exc
        if min_threshold != min_threshold:  # NaN
            raise ValueError("threshold cannot be NaN")

        hot_tags = _normalize_tags(
            DEFAULT_HOT_TAGS if tags is None else tags
        )
        url = str(webhook_url).strip() if webhook_url else None
        if url:
            url = _validate_webhook_url(url)
        drawer_key = str(drawer).strip() or None if drawer else None

        rule = ObserverRule(
            name=key,
            min_threshold=min_threshold,
            hot_tags=hot_tags,
            drawer=drawer_key or None,
            webhook_url=url,
        )
        with self._rules_lock:
            if key in self._rules:
                raise ValueError(f"Observer rule already registered: {key!r}")
            self._rules[key] = rule
        logger.info(
            "[PDM] observer rule added name=%s threshold=%.1f tags=%s drawer=%s webhook=%s",
            key,
            min_threshold,
            list(hot_tags),
            drawer_key,
            bool(url),
        )
        return rule

    def remove_rule(self, name: str) -> bool:
        key = str(name).strip()
        with self._rules_lock:
            return self._rules.pop(key, None) is not None

    def list_rules(self) -> tuple[ObserverRule, ...]:
        with self._rules_lock:
            return tuple(self._rules.values())

    def flush(self, timeout: float = _FLUSH_TIMEOUT_S) -> None:
        """Block until queued alerts are dispatched (tests / shutdown)."""
        done = threading.Event()

        def _wait() -> None:
            self._queue.join()
            done.set()

        waiter = threading.Thread(
            target=_wait, name="pdm-observer-flush", daemon=True
        )
        waiter.start()
        if not done.wait(timeout):
            raise TimeoutError(f"observer.flush timed out after {timeout}s")

    def dispatch_alert(self, sig: SignatureRecord, rule: ObserverRule) -> None:
        """Enqueue a matched alert. Never blocks on webhook I/O."""
        reasons = rule.matches(sig)
        if not reasons:
            return
        alert = ObserverAlert(
            rule=rule,
            memory_id=sig.id,
            text=sig.compressed_fact or "",
            p_magnitude=float(sig.p_magnitude),
            tags=tuple(sig.intent_tags or ()),
            drawer=sig.drawer_domain or "general",
            domain=sig.domain or "",
            reasons=reasons,
            user=sig.user or "default",
            metadata=dict(sig.metadata or {}),
        )
        try:
            self._queue.put_nowait(alert)
        except queue.Full:
            logger.error(
                "[PDM] observer queue full; dropping alert rule=%s memory_id=%s",
                rule.name,
                sig.id,
            )

    def _on_post_save(self, sig: SignatureRecord, memory_id: str) -> None:
        drawer = sig.drawer_domain or ""
        if drawer.startswith(PLUGIN_DRAWER_PREFIX):
            return
        with self._rules_lock:
            rules = tuple(self._rules.values())
        if not rules:
            return
        for rule in rules:
            if rule.matches(sig):
                self.dispatch_alert(sig, rule)

    def _run_worker(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                if item is None:
                    return
                self._deliver(item)
            except Exception:
                logger.exception("[PDM] observer dispatch failed")
            finally:
                self._queue.task_done()

    def _deliver(self, alert: ObserverAlert) -> None:
        with self._fired_lock:
            self.fired.append(alert)
        self._console_dispatch(alert)
        if alert.rule.webhook_url:
            self._webhook_dispatch(alert)

    def _console_dispatch(self, alert: ObserverAlert) -> None:
        stream = self._stream
        color = bool(getattr(stream, "isatty", lambda: False)())
        prefix = _ANSI_RED_BOLD if color else ""
        reset = _ANSI_RESET if color else ""
        tags = ", ".join(alert.tags) or "—"
        reasons = ", ".join(alert.reasons)
        lines = (
            f"{prefix}╔══════════════════════════════════════════════════════════╗{reset}",
            f"{prefix}║  PDM OBSERVER ALERT  rule={alert.rule.name:<28} ║{reset}",
            f"{prefix}╠══════════════════════════════════════════════════════════╣{reset}",
            f"{prefix}║  P={alert.p_magnitude:5.1f}  matched={reasons:<36} ║{reset}",
            f"{prefix}║  id={alert.memory_id:<48} ║{reset}",
            f"{prefix}║  tags={tags:<46} ║{reset}",
            f"{prefix}║  drawer={alert.drawer:<43} ║{reset}",
            f"{prefix}║  {alert.text[:50]:<50} ║{reset}",
            f"{prefix}╚══════════════════════════════════════════════════════════╝{reset}",
        )
        stream.write("\n".join(lines) + "\n")
        stream.flush()

    def _webhook_dispatch(self, alert: ObserverAlert) -> None:
        url = alert.rule.webhook_url
        if not url:
            return
        try:
            import httpx
        except ImportError:
            logger.error("[PDM] observer webhook skipped — httpx is not installed")
            return
        client = self._http_client
        if client is None:
            client = httpx.Client(timeout=_WEBHOOK_TIMEOUT_S)
            self._http_client = client
        try:
            response = client.post(
                url,
                json=alert.webhook_payload(),
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            logger.exception(
                "[PDM] observer webhook failed rule=%s url=%s",
                alert.rule.name,
                url,
            )

    def _close_http_client(self) -> None:
        client = self._http_client
        self._http_client = None
        if client is None:
            return
        try:
            client.close()
        except Exception:
            logger.debug("[PDM] observer http client close failed", exc_info=True)
