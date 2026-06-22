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

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pdm_memory.core.math import (
    DECAY_DELETE_THRESHOLD,
    calculate_effective_spike,
    calculate_incremental_decay,
    infer_domain,
)
from pdm_memory.core.retrieval import RetrievalEngine
from pdm_memory.core.signature import (
    DrawerInfo,
    ExplainReport,
    MemoryHit,
    SignatureRecord,
)
from pdm_memory.storage.base import BaseStorage

logger = logging.getLogger(__name__)


class Memory:
    """
    PDM Memory — persistent, pressure-driven memory for AI applications.

    Args:
        store:       Path to a local SQLite .db file, or "cloud" for cloud mode.
        user:        User identifier to scope all memories (default "default").
        token:       JWT access token (required when store="cloud").
        refresh_token: JWT refresh token for automatic renewal (cloud only).
        cloud_url:   AZUS Companion API base URL (cloud only).
        store_raw:   If False, only SHA-256 hashes of text are stored locally.
                     True by default for usability; set False for maximum privacy.
        engine:      Custom RetrievalEngine instance (override for testing).
    """

    def __init__(
        self,
        store: str = "./pdm_memory.db",
        user: str = "default",
        token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        cloud_url: str = "https://api.azus.ai",
        store_raw: bool = True,
        engine: Optional[RetrievalEngine] = None,
    ) -> None:
        self._user = user
        self._engine = engine or RetrievalEngine()
        self._cloud_driver: Optional[Any] = None   # lazy init
        self._storage: BaseStorage = self._init_storage(
            store, token, refresh_token, cloud_url, store_raw
        )
        logger.debug("[PDM] Memory initialised | user=%s store=%s", user, store)

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def save(
        self,
        text: str,
        source: str = "chat",
        tags: Optional[List[str]] = None,
        p_magnitude: float = 50.0,
        t_persistence: float = 30.0,
        drawer: str = "general",
        regime: str = "neutral",
        phase_privilege: float = 1.0,
        deadline: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
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
            deadline:       Optional datetime for time-sensitive memories (PDM-T).
            metadata:       Arbitrary extra data attached to the memory.

        Returns:
            The new memory ID (UUID string).
        """
        if not text or not text.strip():
            raise ValueError("Memory text cannot be empty.")

        text = text[:500]  # Enforce 500-char limit
        resolved_tags = tags or []
        domain = infer_domain(resolved_tags)
        eff_spike = calculate_effective_spike(p_magnitude, t_persistence, phase_privilege)

        sig = SignatureRecord(
            user=self._user,
            compressed_fact=text.strip(),
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
            metadata=metadata or {},
        )
        memory_id = self._storage.save(sig)
        logger.debug("[PDM] save() → %s (P=%.1f)", memory_id, p_magnitude)
        return memory_id

    def recall(
        self,
        query: str,
        k: int = 5,
        min_pressure: float = 0.0,
        search_cost: float = 0.5,
        drawer: Optional[str] = None,
        reinforce: bool = True,
    ) -> List[MemoryHit]:
        """
        Retrieve the top-k most relevant memories for a query.

        Retrieval works via Threshold-Adjustment Search (TAS):
        pressure thresholds are lowered based on search_cost, then
        coupling scores rank memories by tag/domain/regime resonance.

        Decay is applied incrementally at recall time — no scheduler needed.

        Args:
            query:       The recall query / current context.
            k:           Maximum number of memories to return.
            min_pressure: Only consider memories with p_magnitude >= this.
            search_cost: 0.0 = strict (high threshold), 1.0 = loose (low threshold).
            drawer:      Optional: filter by drawer name.
            reinforce:   If True, increment retrieval_count and update last_retrieved.

        Returns:
            List[MemoryHit] ranked by relevance, length ≤ k.
        """
        records = self._storage.list(
            user=self._user,
            limit=500,
            min_pressure=min_pressure,
            drawer=drawer,
        )
        if not records:
            return []

        hits = self._engine.recall(
            records=records,
            query=query,
            k=k,
            search_cost=search_cost,
        )

        if reinforce and hits:
            self._apply_reinforcement(hits)

        return hits

    def reinforce(self, memory_id: str, coupling_score: float = 0.5) -> None:
        """
        Manually reinforce a memory (raise its pressure).

        recall() does this automatically for all returned hits.
        Use this method to manually signal that a memory was useful.

        Args:
            memory_id:     The memory UUID to reinforce.
            coupling_score: Coupling strength (0–1), influences Δp magnitude.
        """
        rec = self._storage.get(memory_id, user=self._user)
        if rec is None:
            logger.warning("[PDM] reinforce(%s): not found", memory_id)
            return
        delta = self._engine.compute_reinforcement_delta(
            rec.p_magnitude, rec.retrieval_count, coupling_score
        )
        new_p = min(100.0, rec.p_magnitude + delta)
        new_spike = calculate_effective_spike(new_p, rec.t_persistence, rec.phase_privilege)
        self._storage.update(
            memory_id,
            p_magnitude=new_p,
            effective_spike=new_spike,
            retrieval_count=(rec.retrieval_count or 0) + 1,
            last_retrieved=datetime.now(tz=timezone.utc),
        )
        logger.debug("[PDM] reinforce(%s) Δp=+%.2f → P=%.1f", memory_id, delta, new_p)

    def decay(self, dry_run: bool = False) -> Dict[str, int]:
        """
        Manually trigger a decay pass over all memories.

        In normal usage this is NOT necessary — decay runs automatically
        at each recall() call.  Use this method to explicitly purge stale memories
        or to run maintenance on a schedule without Celery.

        Args:
            dry_run: If True, compute what would be decayed but make no writes.

        Returns:
            Dict with keys: decayed, deleted, skipped.
        """
        records = self._storage.list(user=self._user, limit=10_000)
        now = datetime.now(tz=timezone.utc)
        counts = {"decayed": 0, "deleted": 0, "skipped": 0}

        for rec in records:
            created = rec.created_at or now
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            days_elapsed = (now - created).total_seconds() / 86400.0

            new_p, new_spike = calculate_incremental_decay(
                rec.p_magnitude,
                days_elapsed,
                rec.t_persistence,
                rec.phase_privilege,
                rec.decay_rate,
            )

            if new_p == rec.p_magnitude:
                counts["skipped"] += 1
                continue

            if new_p < DECAY_DELETE_THRESHOLD:
                if not dry_run:
                    self._storage.delete(rec.id, user=self._user)
                counts["deleted"] += 1
            else:
                if not dry_run:
                    self._storage.update(
                        rec.id,
                        p_magnitude=new_p,
                        effective_spike=new_spike,
                    )
                counts["decayed"] += 1

        logger.info("[PDM] decay() %s | %s", "(dry_run)" if dry_run else "", counts)
        return counts

    def explain(self, memory_id: str, query: Optional[str] = None) -> ExplainReport:
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
        from pdm_memory.core.math import (
            DOMAIN_HALF_LIVES, DEFAULT_HALF_LIFE,
            calculate_decay_factor, calculate_intent_weight,
            calculate_p_effective, calculate_v,
        )

        rec = self._storage.get(memory_id, user=self._user)
        if rec is None:
            raise KeyError(f"Memory '{memory_id}' not found for user '{self._user}'.")

        now = datetime.now(tz=timezone.utc)
        last_r = rec.last_retrieved or rec.created_at
        if last_r and last_r.tzinfo is None:
            last_r = last_r.replace(tzinfo=timezone.utc)
        days_since = (now - last_r).total_seconds() / 86400.0 if last_r else 0.0

        from pdm_memory.core.math import infer_domain
        domain = rec.domain or infer_domain(rec.intent_tags)
        half_life = DOMAIN_HALF_LIVES.get(domain, DEFAULT_HALF_LIFE)
        decay = calculate_decay_factor(days_since, half_life)
        v = calculate_v(rec.validation_prediction_correct, rec.validation_prediction_total)
        i_weight = calculate_intent_weight(rec.intent_tags, query) if query else None
        p_eff = calculate_p_effective(
            rec.p_magnitude, v, decay,
            i_weight if i_weight is not None else 1.0,
            0.80,
        )

        # TAS coupling for the query
        coupling_score = tag_overlap = domain_match = regime_match = press_prox = None
        if query:
            hits = self._engine.recall(
                records=[rec], query=query, k=1, search_cost=1.0
            )
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
        cloud_url: Optional[str] = None,
        token: Optional[str] = None,
    ) -> Any:
        """
        Sync memories between local SQLite and the AZUS cloud.

        Task 2.3.

        Args:
            direction: "push" | "pull" | "bidirectional"
            cloud_url: Override cloud URL (if not already configured).
            token:     Override JWT token (if not already configured).

        Returns:
            SyncReport with pushed, pulled, conflicts counts.

        Raises:
            RuntimeError: If cloud driver is not configured.
        """
        from pdm_memory.sync import MemorySync

        cloud = self._get_cloud_driver(cloud_url, token)
        if cloud is None:
            raise RuntimeError(
                "sync() requires cloud configuration. "
                "Pass token= and cloud_url= either to Memory() or to sync()."
            )

        if not isinstance(self._storage, __import__(
            "pdm_memory.storage.sqlite_driver", fromlist=["SQLiteDriver"]
        ).SQLiteDriver):
            raise RuntimeError(
                "sync() requires the local storage to be a SQLiteDriver. "
                "Currently using cloud mode — use sync() from a local Memory instance."
            )

        syncer = MemorySync(local=self._storage, cloud=cloud)
        return syncer.sync(user=self._user, direction=direction)

    # ------------------------------------------------------------------
    # Ingestion (see pdm_memory.ingest for full API)
    # ------------------------------------------------------------------

    def ingest(
        self,
        data_source: Any,
        mapping: Optional[Dict[str, str]] = None,
        llm_client: Optional[Any] = None,
        batch_size: int = 50,
        on_progress: Optional[Any] = None,
    ) -> Dict[str, int]:
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
        from pdm_memory.ingest.ingester import DataIngester
        from pdm_memory.ingest.batch import BatchProcessor

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

    def list_drawers(self) -> List[DrawerInfo]:
        """Return all drawer categories with signature counts and avg pressure."""
        return self._storage.list_drawers(user=self._user)

    def count(self) -> int:
        """Return total number of memories for this user."""
        return self._storage.count(user=self._user)

    def close(self) -> None:
        """Release storage connections. Call when done with the Memory instance."""
        self._storage.close()

    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _init_storage(
        self,
        store: str,
        token: Optional[str],
        refresh_token: Optional[str],
        cloud_url: str,
        store_raw: bool,
    ) -> BaseStorage:
        if store == "cloud":
            if not token:
                raise ValueError(
                    "Cloud mode requires a JWT token. "
                    "Pass token='eyJ...' to Memory()."
                )
            from pdm_memory.auth.jwt_handler import JWTAuth
            from pdm_memory.storage.cloud_driver import CloudDriver
            auth = JWTAuth(token=token, refresh_token=refresh_token)
            driver = CloudDriver(auth=auth, base_url=cloud_url, user=self._user)
            # Keep reference for sync()
            self._cloud_driver = driver
            return driver
        else:
            from pdm_memory.storage.sqlite_driver import SQLiteDriver
            return SQLiteDriver(db_path=store, store_raw=store_raw)

    def _get_cloud_driver(
        self,
        cloud_url: Optional[str],
        token: Optional[str],
    ) -> Optional[Any]:
        if self._cloud_driver:
            return self._cloud_driver
        if token and cloud_url:
            from pdm_memory.auth.jwt_handler import JWTAuth
            from pdm_memory.storage.cloud_driver import CloudDriver
            auth = JWTAuth(token=token)
            return CloudDriver(auth=auth, base_url=cloud_url, user=self._user)
        return None

    def _apply_reinforcement(self, hits: List[MemoryHit]) -> None:
        """Write retrieval reinforcement back to storage for all hits."""
        now = datetime.now(tz=timezone.utc)
        batch_updates: List[tuple[str, dict]] = []
        for hit in hits:
            try:
                rec = self._storage.get(hit.id, user=self._user)
                if rec is None:
                    continue
                delta = self._engine.compute_reinforcement_delta(
                    rec.p_magnitude, rec.retrieval_count, hit.coupling_score
                )
                new_p = min(100.0, rec.p_magnitude + delta)
                new_spike = calculate_effective_spike(
                    new_p, rec.t_persistence, rec.phase_privilege
                )
                batch_updates.append((
                    hit.id,
                    {
                        "p_magnitude": new_p,
                        "effective_spike": new_spike,
                        "retrieval_count": (rec.retrieval_count or 0) + 1,
                        "last_retrieved": now,
                    }
                ))
            except Exception as e:
                logger.warning("[PDM] reinforcement check failed for %s: %s", hit.id, e)

        if batch_updates:
            try:
                self._storage.update_batch(batch_updates)
            except Exception as e:
                logger.warning("[PDM] reinforcement batch update failed: %s", e)
