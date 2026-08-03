# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Memory — the main PDM SDK entry point.

Everything a developer touches goes through this class.

Quick start (local mode):
    from pdm_memory import Memory

    mem = Memory(store="./my_app.db")
    mem.save("User prefers metric units", source="chat", tags=["units", "formatting"])
    hits = mem.recall("how should I format the response?", k=5)

    for h in hits:
        print(h.text, h.pressure, h.last_reinforced)

Cloud mode:
    mem = Memory(store="cloud", token="eyJ...", cloud_url="https://api.azus.ai")

Hybrid:
    mem = Memory(store="./local.db")      # local SQLite
    mem.sync(direction="push")            # push to cloud (requires cloud configured)

Wrapper (zero-config LLM integration):
    from pdm_memory.integrations import wrap_openai
    client = wrap_openai(api_key="sk-...", memory=mem)
    reply = client.chat("What units should I use?")
"""

from __future__ import annotations

import builtins
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typing_extensions import Self

from pdm_memory.core.retrieval import DEFAULT_DIVERSITY_BIAS, RetrievalEngine
from pdm_memory.core.signature import (
    DrawerInfo,
    ExplainReport,
    MemoryHit,
    SignatureRecord,
)
from pdm_memory.models import AlignmentReport, MemoryListPage, SurfaceReport, TorsionReport
from pdm_memory.storage.base import BaseStorage
from pdm_memory.types import (
    HookEvent,
    PostRecallHook,
    PostSaveHook,
    PreSaveHook,
    RecallHook,
    TorsionJudge,
)

logger = logging.getLogger(__name__)


class Memory:
    """
    PDM Memory — persistent, pressure-driven memory for AI applications.

    Args:
        store:       SQLite path/URL, PostgreSQL DSN, ``"cloud"``, or a custom URL
                     registered via :func:`pdm_memory.storage.register_storage`.
        user:        User identifier to scope all memories (default "default").
        token:       JWT access token (required when store="cloud").
        refresh_token: JWT refresh token for automatic renewal (cloud only).
        cloud_url:   AZUS Companion API base URL (cloud only).
        store_raw:   If False, only SHA-256 hashes of text are stored locally.
                     True by default for usability; set False for maximum privacy.
        engine:      Custom RetrievalEngine instance (override for testing).
        storage:     Pre-built :class:`BaseStorage` instance (bypasses ``store`` URL).
        torsion_judge: Optional callback to flag torsion pairs rules-only detection misses.
    """

    def __init__(
        self,
        store: str = "./pdm_memory.db",
        user: str = "default",
        token: str | None = None,
        refresh_token: str | None = None,
        cloud_url: str = "https://api.azus.ai",
        store_raw: bool = True,
        engine: RetrievalEngine | None = None,
        storage: BaseStorage | None = None,
        torsion_judge: TorsionJudge | None = None,
    ) -> None:
        self._user = user
        self._engine = engine or RetrievalEngine()
        self._torsion_judge = torsion_judge
        self._pre_save_hooks: list[PreSaveHook] = []
        self._post_save_hooks: list[PostSaveHook] = []
        self._post_recall_hooks: list[PostRecallHook] = []
        self._token = token
        self._refresh_token = refresh_token
        self._cloud_url = cloud_url
        self._cloud_driver: Any | None = None  # lazy init
        if storage is not None:
            if not isinstance(storage, BaseStorage):
                raise TypeError(
                    f"storage must be a BaseStorage instance, got {type(storage).__name__}"
                )
            self._storage = storage
        else:
            self._storage = self._init_storage(store, token, refresh_token, cloud_url, store_raw)
        logger.debug("[PDM] Memory initialised | user=%s store=%s", user, store)

    # ------------------------------------------------------------------
    # Internal hooks (Lean Core / Middleware)
    # ------------------------------------------------------------------

    def add_hook(self, event: HookEvent, hook: Any) -> None:
        """
        Register an internal hook for Memory middleware.

        Events:
            - pre_save:  (SignatureRecord) -> SignatureRecord | None
            - post_save: (SignatureRecord, memory_id) -> None
            - post_recall:(ctx dict) -> None
        """
        match event:
            case "pre_save":
                self._pre_save_hooks.append(hook)
            case "post_save":
                self._post_save_hooks.append(hook)
            case "post_recall":
                self._post_recall_hooks.append(hook)
            case _:
                raise ValueError(f"Unknown hook event: {event!r}")

    def _run_pre_save_hooks(self, sig: SignatureRecord) -> SignatureRecord:
        for hook in self._pre_save_hooks:
            updated = hook(sig)
            if updated is not None:
                sig = updated
        return sig

    def _run_post_save_hooks(self, sig: SignatureRecord, memory_id: str) -> None:
        for hook in self._post_save_hooks:
            hook(sig, memory_id)

    def _run_post_recall_hooks(self, ctx: dict[str, Any]) -> None:
        for hook in self._post_recall_hooks:
            hook(ctx)

    @classmethod
    def from_env(cls, *, prefix: str = "PDM", **kwargs: Any) -> Memory:
        """
        Construct Memory from environment variables (fail fast if missing).

        Reads:
            ``{prefix}_STORE`` (required)
            ``{prefix}_USER`` (default: ``default``)
            ``{prefix}_TOKEN`` (required when store is ``cloud``)
            ``{prefix}_REFRESH_TOKEN`` (optional)
            ``{prefix}_CLOUD_URL`` (default: ``https://api.azus.ai``)

        Example:
            export PDM_STORE=postgresql://localhost/pdm
            export PDM_USER=alice
            mem = Memory.from_env()
        """
        store = os.environ.get(f"{prefix}_STORE")
        if not store:
            raise ValueError(
                f"{prefix}_STORE environment variable is required for Memory.from_env()"
            )
        user = os.environ.get(f"{prefix}_USER", "default")
        token = os.environ.get(f"{prefix}_TOKEN")
        refresh_token = os.environ.get(f"{prefix}_REFRESH_TOKEN")
        cloud_url = os.environ.get(f"{prefix}_CLOUD_URL", "https://api.azus.ai")
        return cls(
            store=store,
            user=user,
            token=token,
            refresh_token=refresh_token,
            cloud_url=cloud_url,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def save(
        self,
        text: str,
        source: str = "chat",
        tags: builtins.list[str] | None = None,
        p_magnitude: float = 50.0,
        t_persistence: float = 30.0,
        drawer: str = "general",
        regime: str = "neutral",
        phase_privilege: float = 1.0,
        deadline: datetime | None = None,
        event_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        dedupe: bool = True,
        dedupe_reinforce: bool = False,
        idempotency_key: str | None = None,
    ) -> str:
        """Store a new memory. See ``memory_ops.write.save_memory``."""
        from pdm_memory.memory_ops.write import save_memory

        return save_memory(
            self,
            text,
            source=source,
            tags=tags,
            p_magnitude=p_magnitude,
            t_persistence=t_persistence,
            drawer=drawer,
            regime=regime,
            phase_privilege=phase_privilege,
            deadline=deadline,
            event_at=event_at,
            metadata=metadata,
            dedupe=dedupe,
            dedupe_reinforce=dedupe_reinforce,
            idempotency_key=idempotency_key,
        )

    def save_many(
        self,
        items: builtins.list[dict[str, Any]],
        *,
        dedupe: bool = True,
        dedupe_reinforce: bool = False,
    ) -> dict[str, int]:
        """Batch-save multiple memories. See ``memory_ops.write.save_many_memories``."""
        from pdm_memory.memory_ops.write import save_many_memories

        return save_many_memories(
            self, items, dedupe=dedupe, dedupe_reinforce=dedupe_reinforce
        )

    def export_json(
        self,
        path: str | Path,
        *,
        limit: int = 100_000,
    ) -> int:
        """Export all user signatures to JSON. Returns count written."""
        from pdm_memory.io.json_transfer import export_signatures_json

        count = export_signatures_json(
            self._storage,
            path,
            user=self._user,
            limit=limit,
        )
        logger.info("[PDM] export_json → %s (%d signatures)", path, count)
        return count

    def export_csv(
        self,
        path: str | Path,
        *,
        limit: int = 100_000,
    ) -> int:
        """Export all user signatures to a CSV file."""
        from pdm_memory.io.csv_transfer import export_signatures_csv

        count = export_signatures_csv(
            self._storage,
            path,
            user=self._user,
            limit=limit,
        )
        logger.info("[PDM] export_csv → %s (%d signatures)", path, count)
        return count

    def import_json(
        self,
        path: str | Path,
        *,
        skip_duplicates: bool = True,
    ) -> dict[str, int]:
        """Import signatures from JSON export. Returns saved/skipped/errors counts."""
        from pdm_memory.io.json_transfer import import_signatures_json

        counts = import_signatures_json(
            self._storage,
            path,
            user=self._user,
            skip_duplicates=skip_duplicates,
        )
        logger.info("[PDM] import_json ← %s %s", path, counts)
        return counts

    def get(self, memory_id: str) -> MemoryHit | None:
        """
        Fetch a single memory by ID with live pressure metrics.

        Returns:
            MemoryHit with current P_effective, or None if not found.
        """
        rec = self._storage.get(memory_id, user=self._user)
        if rec is None:
            return None
        return self._record_to_hit(rec)

    def update(
        self,
        memory_id: str,
        *,
        text: str | None = None,
        tags: builtins.list[str] | None = None,
        p_magnitude: float | None = None,
        t_persistence: float | None = None,
        drawer: str | None = None,
        regime: str | None = None,
        source: str | None = None,
        metadata: dict[str, Any] | None = None,
        deadline: datetime | None = None,
        event_at: datetime | None = None,
    ) -> MemoryHit:
        """Update whitelisted fields on an existing memory."""
        from pdm_memory.memory_ops.mutate import update_memory

        return update_memory(
            self,
            memory_id,
            text=text,
            tags=tags,
            p_magnitude=p_magnitude,
            t_persistence=t_persistence,
            drawer=drawer,
            regime=regime,
            source=source,
            metadata=metadata,
            deadline=deadline,
            event_at=event_at,
        )

    def recall(
        self,
        query: str,
        k: int = 5,
        min_pressure: float = 0.0,
        search_cost: float = 0.5,
        drawer: str | None = None,
        reinforce: bool = True,
        *,
        candidate_limit: int = 10_000,
        page_size: int = 500,
        diversity_bias: float | None = DEFAULT_DIVERSITY_BIAS,
        on_recall: RecallHook | None = None,
    ) -> builtins.list[MemoryHit]:
        """
        Retrieve the top-k most relevant memories for a query.

        Retrieval works via Threshold-Adjustment Search (TAS):
        pressure thresholds are lowered based on search_cost, then
        coupling scores rank memories by tag/domain/regime resonance.

        Decay is applied at recall time via the canonical domain half-life
        law (P_effective = P × … × (1 - decay_factor)). Stored p_magnitude
        is not rewritten on read — no second power-law decay.

        Args:
            query:       The recall query / current context.
            k:           Maximum number of memories to return.
            min_pressure: Only consider memories with p_magnitude >= this.
            search_cost: 0.0 = strict (high threshold), 1.0 = loose (low threshold).
            drawer:      Optional: filter by drawer name.
            reinforce:   If True, increment retrieval_count and update last_retrieved.
            candidate_limit: Max signatures loaded from storage for ranking (default 10_000).
            page_size:       Keyset page size when loading candidates (default 500).
            diversity_bias:  Max fraction of top-k from one drawer (default ``0.4``).
                             Pass ``None`` for pure score order.
            on_recall:       Optional callback invoked for each returned hit before reinforce.

        Returns:
            List[MemoryHit] ranked by relevance, length ≤ k.
        """
        from pdm_memory.memory_ops.recall import run_recall

        return run_recall(
            self,
            query,
            k=k,
            min_pressure=min_pressure,
            search_cost=search_cost,
            drawer=drawer,
            reinforce=reinforce,
            candidate_limit=candidate_limit,
            page_size=page_size,
            diversity_bias=diversity_bias,
            on_recall=on_recall,
        )

    def surface(
        self,
        query: str,
        k: int = 5,
        *,
        search_cost: float = 0.65,
        torsion_threshold: float = 0.70,
        min_goal_pressure: float = 60.0,
        reinforce: bool = False,
    ) -> SurfaceReport:
        """
        Lite agent loop — recall, torsion scan, and alignment gate in one call.

        Args:
            query:              Context / intent to evaluate.
            k:                  Max recall hits.
            search_cost:        TAS threshold looseness for recall.
            torsion_threshold:  Minimum torsion_score to count.
            min_goal_pressure:  Goal-anchor pressure floor for alignment.
            reinforce:          If True, reinforce recall hits (default False).

        Returns:
            SurfaceReport with hits, torsion_count, and alignment status.
        """
        hits = self.recall(
            query,
            k=k,
            search_cost=search_cost,
            reinforce=reinforce,
        )
        torsion_reports = self.detect_torsion(threshold=torsion_threshold)
        alignment = self.verify_alignment(
            query,
            min_pressure=min_goal_pressure,
            torsion_threshold=torsion_threshold,
        )
        return SurfaceReport(
            hits=hits,
            torsion_count=len(torsion_reports),
            alignment=alignment.status,
            alignment_score=alignment.score,
            torsion_reports=torsion_reports,
        )

    def reinforce(self, memory_id: str, coupling_score: float = 0.5) -> None:
        """Manually reinforce a memory (raise its pressure)."""
        from pdm_memory.memory_ops.mutate import reinforce_memory

        reinforce_memory(self, memory_id, coupling_score=coupling_score)

    def penalize(self, memory_id: str, coupling_score: float = 0.5) -> None:
        """Penalize a memory (lower pressure / validation miss)."""
        from pdm_memory.memory_ops.mutate import penalize_memory

        penalize_memory(self, memory_id, coupling_score=coupling_score)

    def delete(self, memory_id: str) -> bool:

        """
        Soft-delete a signature (``is_deleted=True`` where supported).

        Returns:
            True if deleted, False if not found.
        """
        rec = self._storage.get(memory_id, user=self._user)
        if rec is None:
            logger.warning("[PDM] delete(%s): not found", memory_id)
            return False
        self._storage.delete(memory_id, user=self._user)
        logger.debug("[PDM] delete(%s)", memory_id)
        return True

    def reconcile_torsion(
        self,
        signature_a_id: str,
        signature_b_id: str,
        reconciled_text: str,
    ) -> str:
        """Replace a torsion pair with one authoritative signature."""
        from pdm_memory.memory_ops.mutate import reconcile_torsion_pair

        return reconcile_torsion_pair(
            self, signature_a_id, signature_b_id, reconciled_text
        )

    def audit_and_heal(
        self,
        *,
        torsion_threshold: float = 0.70,
        auto_reconcile_threshold: float = 0.85,
        run_decay: bool = True,
        dry_run: bool = False,
        drawer: str | None = None,
        limit: int = 10_000,
    ) -> dict[str, Any]:
        """
        Full-store self-maintenance: torsion scan, auto-reconcile, decay purge.

        1. ``detect_torsion`` across the store (optional drawer filter).
        2. Auto-reconcile pairs with ``torsion_score > auto_reconcile_threshold``
           (default ``0.85``), highest score first; overlapping IDs skipped.
        3. Optional ``decay()`` purge of low ``P_effective`` signatures.

        Returns:
            Dict with ``scanned_pairs``, ``reconciled``, ``skipped``,
            ``reconciled_ids``, ``decay``, ``narrative``, ``dry_run``.
        """
        from pdm_memory.memory_ops.heal import audit_and_heal as run_heal

        return run_heal(
            self,
            torsion_threshold=torsion_threshold,
            auto_reconcile_threshold=auto_reconcile_threshold,
            run_decay=run_decay,
            dry_run=dry_run,
            drawer=drawer,
            limit=limit,
        )

    @staticmethod
    def _heal_narrative(
        *,
        reconciled: int,
        drawers: list[str],
        kinds: list[str],
        decay: dict[str, int] | None,
    ) -> str:
        """Human-readable heal summary for agents / CLI / ops dashboards."""
        from pdm_memory.memory_ops.heal import heal_narrative

        return heal_narrative(
            reconciled=reconciled,
            drawers=drawers,
            kinds=kinds,
            decay=decay,
        )

    def verify_alignment(
        self,
        intent_text: str,
        *,
        min_pressure: float = 60.0,
        k_goals: int = 8,
        torsion_threshold: float = 0.70,
    ) -> AlignmentReport:
        """
        Goal-Anchor Alignment — final integrity gate before an agent ACT.

        Retrieves high-pressure Goal Signatures from stewardship / foundational
        drawers (ranked by IAW), then scores Resonance vs Torsion against
        ``intent_text``.

        Args:
            intent_text: Proposed action / intent to validate.
            min_pressure: Only consider goal anchors at/above this P.
            k_goals: Max anchors to evaluate.
            torsion_threshold: Peak torsion that escalates status to TORSION.

        Returns:
            AlignmentReport — use ``is_safe_to_act`` or ``status == "ALIGNED"``
            before triggering ACT.
        """
        records = self._storage.list(user=self._user, limit=10_000)
        report = self._engine.verify_alignment(
            records,
            intent_text,
            min_pressure=min_pressure,
            k_goals=k_goals,
            torsion_threshold=torsion_threshold,
        )
        logger.debug(
            "[PDM] verify_alignment status=%s score=%.3f torsion=%.3f",
            report.status,
            report.score,
            report.torsion,
        )
        return report

    def decay(self, dry_run: bool = False) -> dict[str, int]:
        """
        Purge memories whose live ``P_effective`` is below the delete threshold.

        Uses the SAME half-life law as ``recall()`` / ``explain()``. Does not
        rewrite ``p_magnitude`` with a separate power-law (that caused double
        decay). ``decayed`` stays in the return dict for API compat and is
        always 0.

        Args:
            dry_run: If True, compute what would be deleted but make no writes.

        Returns:
            Dict with keys: decayed, deleted, skipped.
        """
        from pdm_memory.memory_ops.decay import run_decay

        return run_decay(
            self._storage,
            user=self._user,
            days_since=self._days_since,
            dry_run=dry_run,
        )

    @staticmethod
    def _days_since(dt: datetime | None, now: datetime) -> float:
        if dt is None:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)

    def _record_to_hit(self, rec: SignatureRecord) -> MemoryHit:
        """Build a MemoryHit with live decay / P_effective (no query coupling)."""
        from pdm_memory.memory_ops.explain import record_to_hit

        return record_to_hit(self, rec)

    def detect_torsion(
        self,
        drawer: str | None = None,
        threshold: float = 0.7,
        *,
        apply_v_penalty: bool = False,
        limit: int = 10_000,
    ) -> builtins.list[TorsionReport]:
        """
        Detect Reverse Resonance — high topic similarity with opposing facts/pressure.

        Compares signatures within ``cluster_id`` buckets, ``drawer|domain``
        fallbacks, and auto-discovered virtual clusters (topic similarity > 0.85
        when ``cluster_id`` is absent). Optional ``apply_v_penalty`` records a
        validation miss on each involved signature so future ``P_effective``
        drops via Laplace V.

        Args:
            drawer:    Limit to one drawer (``drawer_domain``). None = all drawers.
            threshold: Minimum ``torsion_score`` to report (default 0.7).
            apply_v_penalty: If True, write V penalty to storage for conflicting IDs.
            limit:     Max signatures to load from storage.

        Returns:
            List of ``TorsionReport``, highest torsion first.
        """
        records = self._storage.list(
            user=self._user,
            limit=limit,
            drawer=drawer,
        )
        reports = self._engine.detect_torsion(
            records,
            threshold=threshold,
            judge=self._torsion_judge,
        )
        if apply_v_penalty and reports:
            self._apply_torsion_v_penalty(reports)
        return reports

    def explain(self, memory_id: str, query: str | None = None) -> ExplainReport:
        """Return a detailed explanation of why a memory has its current pressure."""
        from pdm_memory.memory_ops.explain import explain_memory

        return explain_memory(self, memory_id, query=query)

    def sync(
        self,
        direction: str = "push",
        cloud_url: str | None = None,
        token: str | None = None,
    ) -> Any:
        """
        Sync memories between a local storage backend and the AZUS cloud.

        Task 2.3.

        Args:
            direction: "push" | "pull" | "bidirectional"
            cloud_url: Override cloud URL (if not already configured).
            token:     Override JWT token (if not already configured).

        Returns:
            SyncReport with pushed, pulled, conflicts counts.

        Raises:
            RuntimeError: If cloud driver is not configured or storage is cloud-only.
        """
        from pdm_memory.storage.cloud_driver import CloudDriver
        from pdm_memory.sync import MemorySync

        cloud = self._get_cloud_driver(cloud_url, token)
        if cloud is None:
            raise RuntimeError(
                "sync() requires cloud configuration. "
                "Pass token= and cloud_url= either to Memory() or to sync()."
            )

        if isinstance(self._storage, CloudDriver):
            raise RuntimeError(  # noqa: TRY004
                "sync() requires a local storage backend (SQLite, PostgreSQL, etc.). "
                "Cloud-only Memory cannot sync to itself."
            )

        syncer = MemorySync(local=self._storage, cloud=cloud)
        return syncer.sync(user=self._user, direction=direction)

    # ------------------------------------------------------------------
    # Ingestion (see pdm_memory.ingest for full API)
    # ------------------------------------------------------------------

    def ingest(
        self,
        data_source: Any,
        mapping: dict[str, str] | None = None,
        llm_client: Any | None = None,
        batch_size: int = 50,
        on_progress: Any | None = None,
    ) -> dict[str, int]:
        """
        Ingest legacy data into PDM memory.

        Task 3.1 / 3.2 / 3.3.

        Args:
            data_source: list[dict], CSV file path (str), or list[str].
            mapping:     Maps source field names → PDM field names.
                         Example: {"text": "compressed_fact", "importance": "p_magnitude"}
                         Defaults auto-detect common field names.
            llm_client:  Optional LLM client for auto-signature generation (Task 3.2).
                         If None, raw text ingestion is used.
            batch_size:  Records per processing batch (Task 3.3).
            on_progress: Optional callback(processed: int, total: int).

        Returns:
            Dict with keys: saved, skipped, errors.
        """
        from pdm_memory.ingest.batch import BatchProcessor
        from pdm_memory.ingest.ingester import DataIngester

        ingester = DataIngester(
            storage=self._storage,
            user=self._user,
            mapping=mapping,
            llm_client=llm_client,
        )
        processor = BatchProcessor(batch_size=batch_size)
        return processor.process(data_source, ingester, on_progress=on_progress)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list(
        self,
        limit: int = 50,
        min_pressure: float = 0.0,
        drawer: str | None = None,
        cursor_id: str | None = None,
    ) -> MemoryListPage:
        """
        Keyset-paginated list of memories ordered by pressure (desc).

        Args:
            limit:      Page size (default 50).
            min_pressure: Minimum stored p_magnitude.
            drawer:     Optional drawer filter.
            cursor_id:  Last ID from the previous page (``next_cursor_id``).

        Returns:
            MemoryListPage with ``items`` and ``next_cursor_id`` when more pages exist.
        """
        records = self._storage.list(
            user=self._user,
            limit=limit,
            min_pressure=min_pressure,
            drawer=drawer,
            cursor_id=cursor_id,
        )
        hits = [self._record_to_hit(rec) for rec in records]
        next_cursor = records[-1].id if len(records) == limit else None
        return MemoryListPage(items=hits, next_cursor_id=next_cursor)

    def list_drawers(self) -> builtins.list[DrawerInfo]:
        """Return all drawer categories with signature counts and avg pressure."""
        return self._storage.list_drawers(user=self._user)

    def count(self) -> int:
        """Return total number of memories for this user."""
        return self._storage.count(user=self._user)

    def close(self) -> None:
        """Release storage connections. Call when done with the Memory instance."""
        self._storage.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_recall_candidates(
        self,
        *,
        min_pressure: float,
        drawer: str | None,
        candidate_limit: int,
        page_size: int,
    ) -> builtins.list[SignatureRecord]:
        """Load recall candidates via keyset pagination instead of one bulk query."""
        from pdm_memory.memory_ops.candidates import load_recall_candidates

        return load_recall_candidates(
            self._storage,
            user=self._user,
            min_pressure=min_pressure,
            drawer=drawer,
            candidate_limit=candidate_limit,
            page_size=page_size,
        )

    def _init_storage(
        self,
        store: str,
        token: str | None,
        refresh_token: str | None,
        cloud_url: str,
        store_raw: bool,
    ) -> BaseStorage:
        from pdm_memory.storage.factory import create_storage

        driver = create_storage(
            store,
            user=self._user,
            token=token,
            refresh_token=refresh_token,
            cloud_url=cloud_url,
            store_raw=store_raw,
        )
        if store.strip() == "cloud":
            self._cloud_driver = driver
        return driver

    def _get_cloud_driver(
        self,
        cloud_url: str | None,
        token: str | None,
    ) -> Any | None:
        if self._cloud_driver:
            return self._cloud_driver
        resolved_url = cloud_url or self._cloud_url
        resolved_token = token or self._token
        if resolved_token and resolved_url:
            from pdm_memory.auth.jwt_handler import JWTAuth
            from pdm_memory.storage.cloud_driver import CloudDriver
            from pdm_memory.storage.factory import companion_token_refresh_url

            auth = JWTAuth(
                token=resolved_token,
                refresh_token=self._refresh_token,
                refresh_url=companion_token_refresh_url(resolved_url),
            )
            return CloudDriver(auth=auth, base_url=resolved_url, user=self._user)
        return None

    def _apply_torsion_v_penalty(self, reports: builtins.list[TorsionReport]) -> None:
        """Record a validation miss on each signature involved in high torsion."""
        from pdm_memory.memory_ops.reinforcement import apply_torsion_v_penalty

        apply_torsion_v_penalty(self._storage, reports, user=self._user)

    def _apply_reinforcement(self, hits: builtins.list[MemoryHit]) -> None:
        """Write retrieval reinforcement back to storage for all hits.

        Also increments Validation Coefficient counters so repeated successful
        retrievals raise V and therefore P_effective over time.
        """
        from pdm_memory.memory_ops.reinforcement import apply_reinforcement

        apply_reinforcement(self._storage, self._engine, hits, user=self._user)
