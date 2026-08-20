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
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typing_extensions import Self

from pdm_memory.core.alignment import verify_records
from pdm_memory.core.math import (
    DECAY_DELETE_THRESHOLD,
    calculate_decay_factor,
    calculate_effective_spike,
    calculate_intent_weight,
    calculate_p_effective,
    calculate_v,
    infer_domain,
    resolve_half_life,
)
from pdm_memory.core.retrieval import DEFAULT_DIVERSITY_BIAS, RetrievalEngine
from pdm_memory.core.signature import (
    DrawerInfo,
    ExplainReport,
    MemoryHit,
    SignatureRecord,
)
from pdm_memory.models import (
    AlignmentReport,
    MemoryListPage,
    RelationshipChannelResolution,
    SurfaceReport,
    TorsionReport,
)
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
        """
        Store a new memory.

        The raw text is compressed into a SignatureRecord with PDM pressure fields.
        Only the text you supply is stored — no AI call is made in this method.

        Args:
            text:           The memory content (max 500 chars recommended).
            source:         Origin label: "chat", "manual", "csv", etc.
            tags:           Intent tags (3+ recommended for best retrieval).
            p_magnitude:    Initial pressure / importance (0–100).
            t_persistence:  Days this memory stays relevant before decaying.
            drawer:         Category drawer name (e.g. "preferences", "facts").
            regime:         Context regime: "neutral", "trading", "engineering", etc.
            phase_privilege: Nesting multiplier (usually 1.0).
            deadline:       Optional due datetime (PDM-T ``t_deadline`` — pressure cliff).
            event_at:       Optional event datetime (PDM-T ``t_event_at`` — when it
                            happened / will happen; powers "what was yesterday").
            metadata:       Arbitrary extra data attached to the memory.
            dedupe:         If True, return existing ID when fact hash already stored.
            dedupe_reinforce: When dedupe hits, call reinforce() on the existing memory.
            idempotency_key:  If set, repeated saves with the same key return the existing ID.

        Returns:
            The new memory ID (UUID string), or existing ID if dedupe matched.
        """
        if not text or not text.strip():
            raise ValueError("Memory text cannot be empty.")

        text = text.strip()[:500]

        if idempotency_key:
            key = idempotency_key.strip()
            if key:
                existing = self._storage.find_by_idempotency_key(key, user=self._user)
                if existing is not None:
                    logger.debug("[PDM] save() idempotency → %s", existing.id[:8])
                    return existing.id

        if dedupe:
            from pdm_memory.storage.schema import hash_fact_text

            existing = self._storage.find_by_hash(hash_fact_text(text), user=self._user)
            if existing is not None:
                if dedupe_reinforce:
                    self.reinforce(existing.id)
                logger.debug("[PDM] save() dedupe → %s", existing.id[:8])
                return existing.id

        sig = self._build_signature_record(
            text=text,
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
            idempotency_key=idempotency_key,
        )
        sig = self._run_pre_save_hooks(sig)
        memory_id = self._storage.save(sig)
        self._run_post_save_hooks(sig, memory_id)
        logger.debug("[PDM] save() → %s (P=%.1f)", memory_id, p_magnitude)
        return memory_id

    def save_many(
        self,
        items: builtins.list[dict[str, Any]],
        *,
        dedupe: bool = True,
        dedupe_reinforce: bool = False,
    ) -> dict[str, int]:
        """
        Batch-save multiple memories in one storage transaction when supported.

        Each item accepts the same keys as :meth:`save` (``text``, ``tags``,
        ``p_magnitude``, ``drawer``, ``source``, ``regime``, ``t_persistence``,
        ``metadata``, ``deadline``, ``event_at``).

        Returns:
            Dict with ``saved``, ``skipped``, ``errors`` counts.
        """
        from pdm_memory.storage.schema import hash_fact_text

        saved = 0
        skipped = 0
        errors = 0
        sigs_to_save: list[SignatureRecord] = []
        seen_idempotency_keys: set[str] = set()
        seen_hashes: set[str] = set()
        reinforce_ids: list[str] = []

        # Preload batch lookups once — avoids N× find_by_hash / idempotency RTTs.
        pending_keys: list[str] = []
        pending_hashes: list[str] = []
        for item in items:
            text = str(item.get("text") or item.get("compressed_fact") or "").strip()
            if not text:
                continue
            key = str(item.get("idempotency_key") or "").strip()
            if key:
                pending_keys.append(key)
            if dedupe:
                pending_hashes.append(hash_fact_text(text[:500]))

        existing_by_key = (
            self._storage.find_by_idempotency_keys(pending_keys, user=self._user)
            if pending_keys
            else {}
        )
        existing_by_hash = (
            self._storage.find_by_hashes(pending_hashes, user=self._user)
            if pending_hashes
            else {}
        )

        txn = getattr(self._storage, "transaction", None)
        ctx: AbstractContextManager[None] = txn() if callable(txn) else nullcontext()

        with ctx:
            for item in items:
                try:
                    text = str(item.get("text") or item.get("compressed_fact") or "").strip()
                    if not text:
                        errors += 1
                        continue

                    idempotency_key = str(item.get("idempotency_key") or "").strip()
                    if idempotency_key:
                        if idempotency_key in seen_idempotency_keys:
                            skipped += 1
                            continue
                        if idempotency_key in existing_by_key:
                            skipped += 1
                            continue
                        seen_idempotency_keys.add(idempotency_key)

                    if dedupe:
                        fact_hash = hash_fact_text(text[:500])
                        if fact_hash in seen_hashes:
                            skipped += 1
                            continue
                        existing = existing_by_hash.get(fact_hash)
                        if existing is not None:
                            if dedupe_reinforce:
                                reinforce_ids.append(existing.id)
                            skipped += 1
                            seen_hashes.add(fact_hash)
                            continue
                        seen_hashes.add(fact_hash)

                    sig = self._build_signature_record(
                        text,
                        source=str(item.get("source") or "batch"),
                        tags=item.get("tags") or item.get("intent_tags"),
                        p_magnitude=float(item.get("p_magnitude", 50.0)),
                        t_persistence=float(item.get("t_persistence", 30.0)),
                        drawer=str(item.get("drawer") or item.get("drawer_domain") or "general"),
                        regime=str(item.get("regime") or item.get("question_regime") or "neutral"),
                        deadline=item.get("deadline") or item.get("t_deadline"),
                        event_at=item.get("event_at") or item.get("t_event_at"),
                        metadata=item.get("metadata"),
                        phase_privilege=float(item.get("phase_privilege", 1.0)),
                        idempotency_key=idempotency_key or None,
                    )
                    sig = self._run_pre_save_hooks(sig)
                    sigs_to_save.append(sig)
                except Exception:
                    errors += 1

            if sigs_to_save:
                try:
                    save_results = self._storage.save_batch(sigs_to_save)
                except Exception as exc:
                    logger.warning("[PDM] save_many storage batch failed: %s", exc)
                    errors += len(sigs_to_save)
                else:
                    if len(save_results) != len(sigs_to_save):
                        logger.warning(
                            "[PDM] save_many result mismatch saved=%d returned=%d",
                            len(sigs_to_save),
                            len(save_results),
                        )
                    for sig, result in zip(sigs_to_save, save_results):
                        if result.error is None:
                            saved += 1
                            self._run_post_save_hooks(sig, result.id or sig.id)
                        else:
                            errors += 1
                    if len(save_results) < len(sigs_to_save):
                        errors += len(sigs_to_save) - len(save_results)

        for memory_id in reinforce_ids:
            try:
                self.reinforce(memory_id)
            except Exception as exc:
                logger.warning("[PDM] save_many dedupe reinforce failed for %s: %s", memory_id, exc)

        logger.info("[PDM] save_many saved=%d skipped=%d errors=%d", saved, skipped, errors)
        return {"saved": saved, "skipped": skipped, "errors": errors}

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
        """
        Update whitelisted fields on an existing memory.

        Args:
            memory_id:   Target signature UUID.
            text:        New fact text (max 500 chars).
            tags:        Replacement intent tags (recomputes domain).
            p_magnitude: New stored pressure (0–100).
            t_persistence: New persistence half-life in days.
            drawer:      New drawer / category name.
            regime:      New question regime.
            source:      New source label.
            metadata:    Shallow-merged into existing metadata dict.
            deadline:    New ``t_deadline`` (pass to clear with care — use storage).
            event_at:    New ``t_event_at`` event timestamp.

        Returns:
            Updated MemoryHit with live P_effective.

        Raises:
            ValueError: Memory not found or no fields to update.
        """
        rec = self._storage.get(memory_id, user=self._user)
        if rec is None:
            raise ValueError(f"Memory '{memory_id}' not found for user '{self._user}'")

        fields = self._build_update_fields(
            rec,
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
        if not fields:
            raise ValueError("At least one field must be provided to update()")

        self._storage.update(memory_id, user=self._user, **fields)
        updated = self._storage.get(memory_id, user=self._user)
        if updated is None:
            raise ValueError(f"Memory '{memory_id}' not found after update")
        logger.debug("[PDM] update(%s) fields=%s", memory_id[:8], sorted(fields))
        return self._record_to_hit(updated)

    def update_batch(
        self,
        updates: builtins.list[tuple[str, dict[str, Any]]],
    ) -> dict[str, int]:
        """
        Batch-update multiple memories in one storage batch when supported.

        Each tuple is ``(memory_id, fields)`` where ``fields`` may use the same
        public keys as :meth:`update` (``text``, ``tags``, ``drawer``, etc.) or
        storage-facing aliases such as ``compressed_fact`` / ``intent_tags``.

        Returns:
            Dict with ``updated``, ``skipped``, ``errors`` counts.
        """
        updated = 0
        skipped = 0
        errors = 0
        prepared_updates: list[tuple[str, dict[str, Any]]] = []

        ids = [memory_id for memory_id, _ in updates]
        records = self._storage.get_many(ids, user=self._user) if ids else {}

        for memory_id, raw_fields in updates:
            try:
                rec = records.get(memory_id)
                if rec is None:
                    errors += 1
                    continue

                fields = self._normalize_batch_update_fields(rec, raw_fields)
                if not fields:
                    skipped += 1
                    continue

                prepared_updates.append((memory_id, fields))
            except Exception:
                errors += 1

        if prepared_updates:
            results = self._storage.update_batch(prepared_updates, user=self._user)
            for res in results:
                if res.error is None:
                    updated += 1
                else:
                    errors += 1

        logger.info("[PDM] update_batch updated=%d skipped=%d errors=%d", updated, skipped, errors)
        return {"updated": updated, "skipped": skipped, "errors": errors}

    def update_many(self, items: builtins.list[dict[str, Any]]) -> dict[str, int]:
        """
        Convenience wrapper around :meth:`update_batch`.

        Each item must include ``id`` or ``memory_id`` plus any update fields.
        Returns:
            Dict with ``updated``, ``skipped``, ``errors`` counts.
        """
        updates: list[tuple[str, dict[str, Any]]] = []
        errors = 0

        for item in items:
            memory_id = str(item.get("memory_id") or item.get("id") or "").strip()
            if not memory_id:
                errors += 1
                continue
            fields = {k: v for k, v in item.items() if k not in {"id", "memory_id"}}
            updates.append((memory_id, fields))

        counts = self.update_batch(updates)
        counts["errors"] += errors
        return counts

    def recall(
        self,
        query: str,
        k: int = 5,
        min_pressure: float = 0.0,
        search_cost: float = 0.5,
        drawer: str | None = None,
        reinforce: bool = True,
        *,
        candidate_limit: int = 2_000,
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
            candidate_limit: Max signatures loaded from storage for ranking (default 2_000).
            page_size:       Keyset page size when loading candidates (default 500).
            diversity_bias:  Max fraction of top-k from one drawer (default ``0.4``).
                             Pass ``None`` for pure score order.
            on_recall:       Optional callback invoked for each returned hit before reinforce.

        Returns:
            List[MemoryHit] ranked by relevance, length ≤ k.
        """
        records = self._load_recall_candidates(
            query=query,
            min_pressure=min_pressure,
            drawer=drawer,
            candidate_limit=candidate_limit,
            page_size=page_size,
        )
        if not records:
            ctx: dict[str, Any] = {
                "query": query,
                "k": k,
                "hits": [],
                "reinforced": False,
                "min_pressure": min_pressure,
                "search_cost": search_cost,
                "drawer": drawer,
                "diversity_bias": diversity_bias,
            }
            self._run_post_recall_hooks(ctx)
            return []

        hits = self._engine.recall(
            records=records,
            query=query,
            k=k,
            search_cost=search_cost,
            diversity_bias=diversity_bias,
        )

        if on_recall:
            for hit in hits:
                on_recall(hit)

        reinforced = bool(reinforce and hits)
        if reinforced:
            self._apply_reinforcement(hits)

        ctx = {
            "query": query,
            "k": k,
            "hits": hits,
            "reinforced": reinforced,
            "min_pressure": min_pressure,
            "search_cost": search_cost,
            "drawer": drawer,
            "diversity_bias": diversity_bias,
        }
        self._run_post_recall_hooks(ctx)
        return hits

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
        # One store load — recall / torsion / alignment share the same snapshot.
        records = self._storage.list(user=self._user, limit=10_000)
        hits = self._engine.recall(
            records=records,
            query=query,
            k=k,
            search_cost=search_cost,
            diversity_bias=DEFAULT_DIVERSITY_BIAS,
        )
        reinforced = bool(reinforce and hits)
        if reinforced:
            self._apply_reinforcement(hits)
        self._run_post_recall_hooks(
            {
                "query": query,
                "k": k,
                "hits": hits,
                "reinforced": reinforced,
                "min_pressure": 0.0,
                "search_cost": search_cost,
                "drawer": None,
                "diversity_bias": None,
            }
        )
        torsion_reports = self._engine.detect_torsion(
            records,
            threshold=torsion_threshold,
            judge=self._torsion_judge,
        )
        alignment = self._engine.verify_alignment(
            records,
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
        """
        Manually reinforce a memory (raise its pressure) and record a correct
        prediction in the Validation Coefficient counters.

        recall() calls this automatically for all returned hits when
        ``reinforce=True`` (the default).  Call this explicitly to signal
        that a memory-driven prediction turned out to be *correct*.

        Each call increments both ``validation_prediction_total`` and
        ``validation_prediction_correct``, which raises V and therefore
        P_effective on the next retrieval.

        Args:
            memory_id:      The memory UUID to reinforce.
            coupling_score: Coupling strength (0–1), influences Δp magnitude.
        """
        now = datetime.now(tz=timezone.utc)
        atomic_reinforce = getattr(self._storage, "atomic_reinforce", None)
        if callable(atomic_reinforce):
            atomic_reinforce(
                memory_id,
                self._user,
                compute_delta=lambda p, rc: self._engine.compute_reinforcement_delta(
                    p, rc, coupling_score
                ),
                last_retrieved=now,
            )
            logger.debug("[PDM] reinforce(%s) atomic", memory_id)
            return

        rec = self._storage.get(memory_id, user=self._user)
        if rec is None:
            raise ValueError(f"Memory '{memory_id}' not found for user '{self._user}'")
        delta = self._engine.compute_reinforcement_delta(
            rec.p_magnitude, rec.retrieval_count, coupling_score
        )
        new_p = min(100.0, rec.p_magnitude + delta)
        new_spike = calculate_effective_spike(new_p, rec.t_persistence, rec.phase_privilege)
        new_total = (rec.validation_prediction_total or 0) + 1
        new_correct = (rec.validation_prediction_correct or 0) + 1
        self._storage.update(
            memory_id,
            user=self._user,
            p_magnitude=new_p,
            effective_spike=new_spike,
            retrieval_count=(rec.retrieval_count or 0) + 1,
            last_retrieved=now,
            validation_prediction_total=new_total,
            validation_prediction_correct=new_correct,
        )
        logger.debug(
            "[PDM] reinforce(%s) Δp=+%.2f → P=%.1f  V_total=%d V_correct=%d",
            memory_id, delta, new_p, new_total, new_correct,
        )

    def penalize(self, memory_id: str, coupling_score: float = 0.5) -> None:
        """
        Penalise a memory after a *wrong* prediction and lower its authority.

        This is the consequence signal **E*** described in the Correctability
        Benchmark spec.  Call it when the memory that was used as the basis
        for a prediction turned out to be **incorrect**.

        Mechanically:
        - Increments ``validation_prediction_total`` (prediction was made).
        - Does **not** increment ``validation_prediction_correct`` (it was wrong).
        - This lowers V = (correct+1)/(total+2), which lowers P_effective on
          the next retrieval — the wrong signature loses authority over time.
        - Also decrements ``p_magnitude`` by the same delta formula used in
          reinforcement, so the raw pressure drops too.

        Args:
            memory_id:      The memory UUID to penalise.
            coupling_score: Coupling strength (0–1), influences Δp magnitude.
        """
        rec = self._storage.get(memory_id, user=self._user)
        if rec is None:
            logger.warning("[PDM] penalize(%s): not found", memory_id)
            return
        delta = self._engine.compute_reinforcement_delta(
            rec.p_magnitude, rec.retrieval_count, coupling_score
        )
        new_p = max(0.0, rec.p_magnitude - delta)
        new_spike = calculate_effective_spike(new_p, rec.t_persistence, rec.phase_privilege)
        new_total = (rec.validation_prediction_total or 0) + 1
        # correct count stays the same — this was a wrong prediction
        self._storage.update(
            memory_id,
            user=self._user,
            p_magnitude=new_p,
            effective_spike=new_spike,
            retrieval_count=(rec.retrieval_count or 0) + 1,
            last_retrieved=datetime.now(tz=timezone.utc),
            validation_prediction_total=new_total,
        )
        logger.debug(
            "[PDM] penalize(%s) Δp=-%.2f → P=%.1f  V_total=%d V_correct=%d",
            memory_id, delta, new_p, new_total, rec.validation_prediction_correct or 0,
        )

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
        """
        Replace a torsion pair with one authoritative signature.

        Saves merged fact, then deletes both conflicting records.

        Returns:
            ID of the new reconciled signature.

        Raises:
            ValueError: Missing signatures or empty reconciled text.
        """
        rec_a = self._storage.get(signature_a_id, user=self._user)
        rec_b = self._storage.get(signature_b_id, user=self._user)
        if rec_a is None or rec_b is None:
            raise ValueError("One or both signatures not found")
        text = reconciled_text.strip()[:500]
        if not text:
            raise ValueError("reconciled_text cannot be empty")

        tags = sorted({t for t in rec_a.intent_tags + rec_b.intent_tags if t})
        drawer = rec_a.drawer_domain or rec_b.drawer_domain or "general"
        p_mag = min(100.0, max(rec_a.p_magnitude, rec_b.p_magnitude) + 8.0)

        txn = getattr(self._storage, "transaction", None)
        ctx: AbstractContextManager[None] = txn() if callable(txn) else nullcontext()
        with ctx:
            new_id = self.save(
                text,
                tags=tags,
                drawer=drawer,
                p_magnitude=p_mag,
                source="reconcile",
                dedupe=False,
            )
            self._storage.delete(signature_a_id, user=self._user)
            self._storage.delete(signature_b_id, user=self._user)
        logger.info(
            "[PDM] reconcile_torsion %s+%s → %s",
            signature_a_id[:8],
            signature_b_id[:8],
            new_id[:8],
        )
        return new_id

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
        reports = self.detect_torsion(
            drawer=drawer,
            threshold=torsion_threshold,
            limit=limit,
        )
        # Strictly greater than threshold (architectural claim: score > 0.85)
        candidates = sorted(
            (r for r in reports if float(r.torsion_score) > float(auto_reconcile_threshold)),
            key=lambda r: float(r.torsion_score),
            reverse=True,
        )

        reconciled = 0
        skipped = 0
        reconciled_ids: list[str] = []
        reconciled_drawers: list[str] = []
        reconciled_kinds: list[str] = []
        consumed: set[str] = set()

        # Prefetch all records needed by the reconcile loop in one bulk query
        # (2*N get() calls → 1 get_many() call regardless of candidate count).
        all_ids: set[str] = set()
        for report in candidates:
            all_ids.add(report.signature_a_id)
            all_ids.add(report.signature_b_id)
        records = self._storage.get_many(list(all_ids), user=self._user)

        for report in candidates:
            a_id = report.signature_a_id
            b_id = report.signature_b_id
            if a_id in consumed or b_id in consumed:
                skipped += 1
                continue
            rec_a = records.get(a_id)
            rec_b = records.get(b_id)
            if rec_a is None or rec_b is None:
                skipped += 1
                continue

            # Prefer the higher-pressure fact as the surviving narrative
            if float(rec_a.p_magnitude) >= float(rec_b.p_magnitude):
                text = (rec_a.compressed_fact or "").strip()
            else:
                text = (rec_b.compressed_fact or "").strip()
            if not text:
                skipped += 1
                continue

            if dry_run:
                reconciled += 1
                reconciled_ids.append(f"dry:{a_id[:8]}+{b_id[:8]}")
                reconciled_drawers.append(report.drawer or "general")
                reconciled_kinds.append(report.conflict_kind or "semantic")
                consumed.add(a_id)
                consumed.add(b_id)
                continue

            try:
                new_id = self.reconcile_torsion(a_id, b_id, text)
            except ValueError as exc:
                logger.warning(
                    "[PDM] audit_and_heal skip pair %s+%s: %s",
                    a_id[:8],
                    b_id[:8],
                    exc,
                )
                skipped += 1
                continue

            reconciled += 1
            reconciled_ids.append(new_id)
            reconciled_drawers.append(report.drawer or "general")
            reconciled_kinds.append(report.conflict_kind or "semantic")
            consumed.add(a_id)
            consumed.add(b_id)

        decay_counts: dict[str, int] | None = None
        if run_decay:
            decay_counts = self.decay(dry_run=dry_run)

        narrative = self._heal_narrative(
            reconciled=reconciled,
            drawers=reconciled_drawers,
            kinds=reconciled_kinds,
            decay=decay_counts,
        )
        summary = {
            "scanned_pairs": len(reports),
            "auto_reconcile_threshold": float(auto_reconcile_threshold),
            "reconciled": reconciled,
            "skipped": skipped,
            "reconciled_ids": reconciled_ids,
            "decay": decay_counts,
            "narrative": narrative,
            "dry_run": dry_run,
        }
        logger.info("[PDM] audit_and_heal %s", summary)
        return summary

    @staticmethod
    def _heal_narrative(
        *,
        reconciled: int,
        drawers: list[str],
        kinds: list[str],
        decay: dict[str, int] | None,
    ) -> str:
        """Human-readable heal summary for agents / CLI / ops dashboards."""
        parts: list[str] = []
        if reconciled > 0:
            drawer = drawers[0] if drawers else "general"
            # Prefer a single drawer label when all matches agree
            if drawers and len(set(drawers)) == 1:
                drawer = drawers[0]
            elif drawers and len(set(drawers)) > 1:
                drawer = ", ".join(sorted(set(drawers))[:3])
            kind = kinds[0] if kinds else "factual"
            if kinds and len(set(kinds)) > 1:
                kind = "mixed"
            noun = "contradiction" if reconciled == 1 else "contradictions"
            parts.append(f"Detected and resolved {reconciled} {kind} {noun} in '{drawer}'.")
        else:
            parts.append("No high-confidence torsion pairs required reconciliation.")

        purged = int((decay or {}).get("deleted", 0) or 0)
        if purged > 0:
            residue = "residue" if purged == 1 else "residues"
            parts.append(f"Purged {purged} low-pressure {residue}.")
        elif decay is not None:
            parts.append("No low-pressure residues required purge.")

        return " ".join(parts)

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
        report = verify_records(
            records,
            intent_text,
            engine=self._engine,
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
        records = self._storage.list(user=self._user, limit=10_000)
        now = datetime.now(tz=timezone.utc)
        counts = {"decayed": 0, "deleted": 0, "skipped": 0}
        to_delete: list[str] = []

        for rec in records:
            days_since_touch = self._days_since(rec.last_retrieved or rec.created_at, now)
            days_since_created = self._days_since(rec.created_at, now)
            domain = rec.domain or infer_domain(rec.intent_tags)
            half_life = resolve_half_life(domain)
            decay = calculate_decay_factor(
                days_since_touch,
                half_life,
                days_since_created=days_since_created,
                t_persistence=rec.t_persistence,
            )
            v = calculate_v(
                rec.validation_prediction_correct,
                rec.validation_prediction_total,
            )
            p_eff = calculate_p_effective(
                rec.p_magnitude, v, decay, intent_weight=1.0, quality=0.80
            )

            if p_eff < DECAY_DELETE_THRESHOLD:
                to_delete.append(rec.id)
                counts["deleted"] += 1
            else:
                counts["skipped"] += 1

        if to_delete and not dry_run:
            hard_delete = getattr(self._storage, "hard_delete", None)
            txn = getattr(self._storage, "transaction", None)
            ctx: AbstractContextManager[None] = txn() if callable(txn) else nullcontext()
            with ctx:
                for memory_id in to_delete:
                    if callable(hard_delete):
                        hard_delete(memory_id, user=self._user)
                    else:
                        self._storage.delete(memory_id, user=self._user)

        logger.info("[PDM] decay() %s | %s", "(dry_run)" if dry_run else "", counts)
        return counts

    @staticmethod
    def _days_since(dt: datetime | None, now: datetime) -> float:
        if dt is None:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)

    def _record_to_hit(self, rec: SignatureRecord) -> MemoryHit:
        """Build a MemoryHit with live decay / P_effective (no query coupling)."""
        now = datetime.now(tz=timezone.utc)
        days_since = self._days_since(rec.last_retrieved or rec.created_at, now)
        days_since_created = self._days_since(rec.created_at, now)
        domain = rec.domain or infer_domain(rec.intent_tags)
        half_life = resolve_half_life(domain)
        decay = calculate_decay_factor(
            days_since,
            half_life,
            days_since_created=days_since_created,
            t_persistence=rec.t_persistence,
        )
        v = calculate_v(rec.validation_prediction_correct, rec.validation_prediction_total)
        p_eff = calculate_p_effective(rec.p_magnitude, v, decay, 1.0, 0.80)
        e_temporal, is_urgent = self._engine._temporal_energy(rec, now)
        return MemoryHit.from_record(
            rec,
            p_eff,
            decay,
            1.0,
            v,
            e_temporal=e_temporal,
            is_urgent=is_urgent,
        )

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
        """
        Return a detailed explanation of why a memory has its current pressure.

        Task 5.1: The explain method.

        Shows every component of P_effective and (if query given) the TAS
        coupling scores that led to resonance.

        Args:
            memory_id: The UUID of the memory to explain.
            query:     Optional: show resonance components for this query.

        Returns:
            ExplainReport (call .render() for a human-readable string).

        Raises:
            KeyError: If the memory is not found.
        """
        rec = self._storage.get(memory_id, user=self._user)
        if rec is None:
            raise KeyError(f"Memory '{memory_id}' not found for user '{self._user}'.")

        now = datetime.now(tz=timezone.utc)
        days_since = self._days_since(rec.last_retrieved or rec.created_at, now)
        days_since_created = self._days_since(rec.created_at, now)

        domain = rec.domain or infer_domain(rec.intent_tags)
        half_life = resolve_half_life(domain)
        decay = calculate_decay_factor(
            days_since,
            half_life,
            days_since_created=days_since_created,
            t_persistence=rec.t_persistence,
        )
        v = calculate_v(rec.validation_prediction_correct, rec.validation_prediction_total)
        i_weight = calculate_intent_weight(rec.intent_tags, query) if query else None
        p_eff = calculate_p_effective(
            rec.p_magnitude,
            v,
            decay,
            i_weight if i_weight is not None else 1.0,
            0.80,
        )

        # TAS coupling for the query
        coupling_score = tag_overlap = domain_match = regime_match = press_prox = None
        if query:
            hits = self._engine.recall(records=[rec], query=query, k=1, search_cost=1.0)
            if hits:
                h = hits[0]
                coupling_score = h.coupling_score
                tag_overlap = h.tag_overlap
                domain_match = h.domain_match
                regime_match = h.regime_match
                press_prox = h.pressure_proximity

        return ExplainReport(
            memory_id=memory_id,
            compressed_fact=rec.compressed_fact,
            drawer=rec.drawer_domain,
            source=rec.source,
            p_magnitude=rec.p_magnitude,
            t_persistence=rec.t_persistence,
            effective_spike=rec.effective_spike or 0.0,
            created_at=rec.created_at,
            last_retrieved=rec.last_retrieved,
            retrieval_count=rec.retrieval_count,
            days_since_retrieved=round(days_since, 2),
            half_life_days=half_life,
            decay_factor=round(decay, 4),
            v_coefficient=round(v, 4),
            intent_weight=round(i_weight, 4) if i_weight is not None else None,
            quality=0.80,
            p_effective=round(p_eff, 2),
            coupling_score=coupling_score,
            tag_overlap=tag_overlap,
            domain_match=domain_match,
            regime_match=regime_match,
            pressure_proximity=press_prox,
            intent_tags=rec.intent_tags,
            domain=domain,
        )

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

    def current_resolution(
        self,
        observer: str = "principal",
        target: str = "operator",
        domain: str = "*",
    ) -> RelationshipChannelResolution:
        """
        Query the RelationshipChannel resolution matrix (ecosystem / cloud only).

        Thin client over Companion ``GET /api/v1/integrity/profile/`` with
        ``observer``, ``target``, and ``domain`` query params. Returns the
        multidomain channel vector — never a single flat percentage score.

        Requires ``store="cloud"`` or a JWT ``token`` (and optional ``cloud_url``)
        so the SDK can reach the Companion integrity API.
        """
        from pdm_memory.storage.cloud_driver import CloudDriver

        cloud: CloudDriver | None = None
        if isinstance(self._storage, CloudDriver):
            cloud = self._storage
        elif self._cloud_driver is not None and isinstance(
            self._cloud_driver, CloudDriver
        ):
            cloud = self._cloud_driver
        else:
            resolved = self._get_cloud_driver(None, None)
            if isinstance(resolved, CloudDriver):
                cloud = resolved
                self._cloud_driver = cloud

        if cloud is None:
            raise RuntimeError(
                "current_resolution requires ecosystem/cloud mode: "
                "Memory(store='cloud', token=...) or Memory(..., token=..., cloud_url=...)."
            )
        return cloud.current_resolution(
            observer=observer,
            target=target,
            domain=domain,
        )

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

    def _build_signature_record(
        self,
        text: str,
        *,
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
        idempotency_key: str | None = None,
    ) -> SignatureRecord:
        resolved_tags = tags or []
        domain = infer_domain(resolved_tags)
        eff_spike = calculate_effective_spike(p_magnitude, t_persistence, phase_privilege)

        # Companion parity: a future deadline without event_at still needs an
        # event timestamp for temporal-window recall.
        resolved_event = event_at
        if resolved_event is None and deadline is not None:
            resolved_event = deadline

        return SignatureRecord(
            user=self._user,
            compressed_fact=text,
            source=source,
            p_magnitude=p_magnitude,
            t_persistence=t_persistence,
            phase_privilege=phase_privilege,
            effective_spike=eff_spike,
            intent_tags=resolved_tags,
            question_regime=regime,
            domain=domain,
            drawer_domain=drawer,
            decay_rate=0.9,
            t_deadline=deadline,
            t_event_at=resolved_event,
            metadata=metadata or {},
            idempotency_key=idempotency_key.strip() if idempotency_key else None,
        )

    def _build_update_fields(
        self,
        rec: SignatureRecord,
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
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = dict(extra_fields or {})
        if text is not None:
            if not text.strip():
                raise ValueError("Memory text cannot be empty.")
            from pdm_memory.storage.schema import encode_compressed_fact

            trimmed = text.strip()[:500]
            store_raw = getattr(self._storage, "store_raw", True)
            stored, text_hash = encode_compressed_fact(trimmed, store_raw=store_raw)
            fields["compressed_fact"] = stored
            fields["compressed_fact_hash"] = text_hash
        if tags is not None:
            fields["intent_tags"] = tags
            fields["domain"] = infer_domain(tags)
        if p_magnitude is not None:
            if not 0.0 <= p_magnitude <= 100.0:
                raise ValueError("p_magnitude must be between 0 and 100")
            fields["p_magnitude"] = p_magnitude
        if t_persistence is not None:
            fields["t_persistence"] = t_persistence
        if drawer is not None:
            fields["drawer_domain"] = drawer
        if regime is not None:
            fields["question_regime"] = regime
        if source is not None:
            fields["source"] = source
        if metadata is not None:
            fields["metadata"] = {**(rec.metadata or {}), **metadata}
        if deadline is not None:
            fields["t_deadline"] = deadline
        if event_at is not None:
            fields["t_event_at"] = event_at
        elif deadline is not None and rec.t_event_at is None:
            # Same backfill rule as save(): deadline implies event when unset.
            fields["t_event_at"] = deadline

        if any(key in fields for key in ("p_magnitude", "t_persistence", "phase_privilege")):
            new_p = fields.get("p_magnitude", rec.p_magnitude)
            new_t = fields.get("t_persistence", rec.t_persistence)
            new_phase = fields.get("phase_privilege", rec.phase_privilege)
            fields["effective_spike"] = calculate_effective_spike(new_p, new_t, new_phase)
        return fields

    def _normalize_batch_update_fields(
        self,
        rec: SignatureRecord,
        raw_fields: dict[str, Any],
    ) -> dict[str, Any]:
        fields = dict(raw_fields)
        text = fields.pop("text", None)
        if text is None and "compressed_fact" in fields:
            text = fields.pop("compressed_fact")

        tags = fields.pop("tags", None)
        if tags is None and "intent_tags" in fields:
            tags = fields.pop("intent_tags")

        drawer = fields.pop("drawer", None)
        if drawer is None and "drawer_domain" in fields:
            drawer = fields.pop("drawer_domain")

        regime = fields.pop("regime", None)
        if regime is None and "question_regime" in fields:
            regime = fields.pop("question_regime")

        deadline = fields.pop("deadline", None)
        if deadline is None and "t_deadline" in fields:
            deadline = fields.pop("t_deadline")

        event_at = fields.pop("event_at", None)
        if event_at is None and "t_event_at" in fields:
            event_at = fields.pop("t_event_at")

        source = fields.pop("source", None) if "source" in fields else None
        metadata = fields.pop("metadata", None) if "metadata" in fields else None

        return self._build_update_fields(
            rec,
            text=text,
            tags=tags,
            p_magnitude=fields.pop("p_magnitude", None),
            t_persistence=fields.pop("t_persistence", None),
            drawer=drawer,
            regime=regime,
            source=source,
            metadata=metadata,
            deadline=deadline,
            event_at=event_at,
            extra_fields=fields,
        )

    def _load_recall_candidates(
        self,
        *,
        query: str | None = None,
        min_pressure: float,
        drawer: str | None,
        candidate_limit: int,
        page_size: int,
    ) -> builtins.list[SignatureRecord]:
        """Load recall candidates via keyset pagination instead of one bulk query.

        When ``query`` has tokens, first page with ``tag_any`` prefilter. If that
        yields too few rows, fill remainder from pressure-ordered unfiltered pages.
        """
        if candidate_limit <= 0:
            return []

        page_size = max(1, min(page_size, candidate_limit))
        query_tokens = RetrievalEngine._tokenize_query(query) if query else []

        def _paginate(tag_any: builtins.list[str] | None) -> builtins.list[SignatureRecord]:
            records: list[SignatureRecord] = []
            cursor_id: str | None = None
            while len(records) < candidate_limit:
                batch_limit = min(page_size, candidate_limit - len(records))
                batch = self._storage.list(
                    user=self._user,
                    limit=batch_limit,
                    min_pressure=min_pressure,
                    drawer=drawer,
                    cursor_id=cursor_id,
                    tag_any=tag_any,
                )
                if not batch:
                    break
                records.extend(batch)
                if len(batch) < batch_limit:
                    break
                cursor_id = batch[-1].id
            return records

        if not query_tokens:
            return _paginate(None)

        tagged = _paginate(query_tokens)
        # Enough tag hits — prefer them (cheaper + more relevant).
        min_fill = min(candidate_limit, max(50, page_size))
        if len(tagged) >= min_fill:
            return tagged

        # Fallthrough: merge pressure-ordered fill for sparse tag indexes.
        by_id = {rec.id: rec for rec in tagged}
        for rec in _paginate(None):
            if rec.id not in by_id:
                by_id[rec.id] = rec
            if len(by_id) >= candidate_limit:
                break
        merged = list(by_id.values())
        merged.sort(key=lambda r: (r.p_magnitude, r.id), reverse=True)
        return merged[:candidate_limit]

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
        affected: set[str] = set()
        for report in reports:
            affected.add(report.signature_a_id)
            affected.add(report.signature_b_id)
        if not affected:
            return

        records = self._storage.get_many(list(affected), user=self._user)
        batch_updates: list[tuple[str, dict]] = []
        for sig_id, rec in records.items():
            new_total = int(rec.validation_prediction_total or 0) + 1
            # correct unchanged → Laplace V decreases
            batch_updates.append(
                (
                    sig_id,
                    {"validation_prediction_total": new_total},
                )
            )
        if batch_updates:
            try:
                results = self._storage.update_batch(batch_updates, user=self._user)
                for res in results:
                    if res.error:
                        logger.warning("[PDM] torsion V penalty failed for %s: %s", res.id, res.error)
            except Exception as e:
                logger.warning("[PDM] torsion V penalty batch failed: %s", e)

    def _apply_reinforcement(self, hits: builtins.list[MemoryHit]) -> None:
        """Write retrieval reinforcement back to storage for all hits.

        Also increments Validation Coefficient counters so repeated successful
        retrievals raise V and therefore P_effective over time.
        """
        if not hits:
            return
        now = datetime.now(tz=timezone.utc)
        records = self._storage.get_many([hit.id for hit in hits], user=self._user)
        batch_updates: list[tuple[str, dict]] = []
        for hit in hits:
            rec = records.get(hit.id)
            if rec is None:
                continue
            try:
                delta = self._engine.compute_reinforcement_delta(
                    rec.p_magnitude, rec.retrieval_count, hit.coupling_score
                )
                new_p = min(100.0, rec.p_magnitude + delta)
                new_spike = calculate_effective_spike(new_p, rec.t_persistence, rec.phase_privilege)
                batch_updates.append(
                    (
                        hit.id,
                        {
                            "p_magnitude": new_p,
                            "effective_spike": new_spike,
                            "retrieval_count": (rec.retrieval_count or 0) + 1,
                            "last_retrieved": now,
                        },
                    )
                )
            except (TypeError, ValueError) as e:
                logger.warning("[PDM] reinforcement check failed for %s: %s", hit.id, e)

        if batch_updates:
            try:
                results = self._storage.update_batch(batch_updates, user=self._user)
                for res in results:
                    if res.error:
                        logger.warning("[PDM] reinforcement update failed for %s: %s", res.id, res.error)
            except Exception as e:
                logger.warning("[PDM] reinforcement batch update failed: %s", e)
