# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
PDM Explorer — local FastAPI dashboard for visualizing memory physics.

    uvicorn pdm_memory.tools.server:create_app --factory
    # or via CLI: pdm-cli ui --store ./local.db --port 8080
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pdm_memory.core.signature import SignatureRecord
from pdm_memory.memory import Memory
from pdm_memory.models import TorsionReport
from pdm_memory.tools.explorer_actions import (
    generate_reconciliation,
    record_to_node,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
RESONANCE_LINK_THRESHOLD = 0.35
MAX_LINKS = 400


class ReinforceBody(BaseModel):
    coupling_score: float = Field(default=0.65, ge=0.0, le=1.0)


class ResolveTorsionBody(BaseModel):
    signature_a_id: str
    signature_b_id: str
    use_ai: bool = True
    reconciled_text: str | None = Field(default=None, max_length=500)


def _tag_jaccard(a: list[str], b: list[str]) -> float:
    sa = {t.lower() for t in a if t}
    sb = {t.lower() for t in b if t}
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def _torsion_to_dict(report: TorsionReport) -> dict[str, Any]:
    return {
        "signature_a_id": report.signature_a_id,
        "signature_b_id": report.signature_b_id,
        "signature_a_text": report.signature_a_text,
        "signature_b_text": report.signature_b_text,
        "drawer": report.drawer,
        "domain": report.domain,
        "torsion_score": report.torsion_score,
        "topic_similarity": report.topic_similarity,
        "contradiction_strength": report.contradiction_strength,
        "explanation": report.explanation,
        "conflict_kind": report.conflict_kind,
        "cluster_key": report.cluster_key,
    }


def _build_resonance_links(
    records: list[SignatureRecord],
    *,
    threshold: float = RESONANCE_LINK_THRESHOLD,
    max_links: int = MAX_LINKS,
) -> list[dict[str, Any]]:
    scored: list[tuple[float, str, str]] = []
    n = len(records)
    for i in range(n):
        for j in range(i + 1, n):
            res = _tag_jaccard(records[i].intent_tags, records[j].intent_tags)
            if res >= threshold:
                scored.append((res, records[i].id, records[j].id))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [
        {"source": a, "target": b, "resonance": round(res, 4)}
        for res, a, b in scored[:max_links]
    ]


def _node_payload(
    mem: Memory,
    memory_id: str,
    now: datetime,
    *,
    compute_torsion: bool = True,
) -> dict[str, Any]:
    rec = mem._storage.get(memory_id, user=mem._user)
    if rec is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    torsion_status = "clear"
    if compute_torsion:
        torsion_ids: set[str] = set()
        for report in mem.detect_torsion(threshold=0.7):
            torsion_ids.add(report.signature_a_id)
            torsion_ids.add(report.signature_b_id)
        torsion_status = "torsion" if rec.id in torsion_ids else "clear"
    return record_to_node(rec, now, torsion_status=torsion_status)


def create_app(
    store: str = "./pdm_memory.db",
    user: str = "default",
) -> FastAPI:
    """
    Build the Explorer FastAPI app bound to a local (or cloud) store.

    Args:
        store: Path to SQLite .db or ``"cloud"``.
        user:  User scope for all reads.
    """
    app = FastAPI(
        title="PDM Explorer",
        description="Interactive Memory Console for Pressure-Driven Memory",
        version="0.1.9",
    )
    app.state.store = store
    app.state.user = user

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_model=None)
    def index() -> FileResponse | HTMLResponse:
        index_path = STATIC_DIR / "index.html"
        if index_path.is_file():
            return FileResponse(
                index_path,
                media_type="text/html",
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate",
                    "Pragma": "no-cache",
                },
            )
        return HTMLResponse(
            "<h1>PDM Explorer</h1><p>index.html missing from package static/</p>",
            status_code=500,
        )

    @app.get("/api/v1/memory-map")
    def memory_map(
        limit: int = Query(default=2000, ge=1, le=10_000),
        torsion_threshold: float = Query(default=0.7, ge=0.0, le=1.0),
        link_threshold: float = Query(default=RESONANCE_LINK_THRESHOLD, ge=0.0, le=1.0),
        projected_days: float = Query(default=0.0, ge=0.0, le=3650.0),
    ) -> dict[str, Any]:
        """All signatures with live/projected pressure + torsion flags + resonance edges."""
        with Memory(store=app.state.store, user=app.state.user) as mem:
            records = mem._storage.list(user=app.state.user, limit=limit)
            torsion_reports = mem.detect_torsion(threshold=torsion_threshold)

        torsion_ids: set[str] = set()
        for report in torsion_reports:
            torsion_ids.add(report.signature_a_id)
            torsion_ids.add(report.signature_b_id)

        now = datetime.now(tz=timezone.utc)
        nodes: list[dict[str, Any]] = []
        for rec in records:
            nodes.append(
                record_to_node(
                    rec,
                    now,
                    torsion_status="torsion" if rec.id in torsion_ids else "clear",
                    extra_days=projected_days,
                )
            )

        links = _build_resonance_links(records, threshold=link_threshold)
        return {
            "store": app.state.store,
            "user": app.state.user,
            "count": len(nodes),
            "torsion_count": len(torsion_ids),
            "projected_days": projected_days,
            "nodes": nodes,
            "links": links,
        }

    @app.get("/api/v1/torsion")
    def torsion(
        threshold: float = Query(default=0.7, ge=0.0, le=1.0),
        drawer: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Return torsion reports; ``latest`` is the highest-scoring pair."""
        with Memory(store=app.state.store, user=app.state.user) as mem:
            reports = mem.detect_torsion(drawer=drawer, threshold=threshold)

        payload = [_torsion_to_dict(r) for r in reports]
        return {
            "count": len(payload),
            "latest": payload[0] if payload else None,
            "reports": payload,
        }

    @app.get("/api/v1/search")
    def search(
        q: str = Query(..., min_length=1, max_length=500),
        k: int = Query(default=25, ge=1, le=100),
        search_cost: float = Query(default=0.65, ge=0.0, le=1.0),
    ) -> dict[str, Any]:
        """Semantic recall search — returns matching signature IDs (no reinforce)."""
        with Memory(store=app.state.store, user=app.state.user) as mem:
            hits = mem.recall(q, k=k, reinforce=False, search_cost=search_cost)

        return {
            "query": q,
            "search_cost": search_cost,
            "count": len(hits),
            "hits": [
                {
                    "id": h.id,
                    "text": h.text,
                    "p_effective": round(float(h.pressure), 2),
                    "coupling_score": round(float(h.coupling_score), 4),
                    "drawer": h.drawer,
                }
                for h in hits
            ],
        }

    @app.post("/api/v1/memories/{memory_id}/reinforce")
    def reinforce_memory(memory_id: str, body: ReinforceBody) -> dict[str, Any]:
        """Manually reinforce a signature (raises P_magnitude in store)."""
        with Memory(store=app.state.store, user=app.state.user) as mem:
            rec_before = mem._storage.get(memory_id, user=app.state.user)
            if rec_before is None:
                raise HTTPException(status_code=404, detail="Memory not found")
            mem.reinforce(memory_id, coupling_score=body.coupling_score)
            now = datetime.now(tz=timezone.utc)
            node = _node_payload(mem, memory_id, now, compute_torsion=False)
        return {
            "ok": True,
            "id": memory_id,
            "p_magnitude_before": round(float(rec_before.p_magnitude), 2),
            "node": node,
        }

    @app.delete("/api/v1/memories/{memory_id}")
    def delete_memory(memory_id: str) -> dict[str, Any]:
        """Hard-delete a signature from the local store."""
        with Memory(store=app.state.store, user=app.state.user) as mem:
            deleted = mem.delete(memory_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Memory not found")
        return {"ok": True, "deleted": True, "id": memory_id}

    @app.post("/api/v1/torsion/resolve")
    def resolve_torsion(body: ResolveTorsionBody) -> dict[str, Any]:
        """Reconcile a torsion pair — optional AI draft, persistent merge in store."""
        with Memory(store=app.state.store, user=app.state.user) as mem:
            rec_a = mem._storage.get(body.signature_a_id, user=app.state.user)
            rec_b = mem._storage.get(body.signature_b_id, user=app.state.user)
            if rec_a is None or rec_b is None:
                raise HTTPException(status_code=404, detail="Torsion pair not found")

            reports = mem.detect_torsion(threshold=0.5)
            report = next(
                (
                    r
                    for r in reports
                    if {r.signature_a_id, r.signature_b_id}
                    == {body.signature_a_id, body.signature_b_id}
                ),
                None,
            )
            explanation = report.explanation if report else ""
            conflict_kind = report.conflict_kind if report else "semantic"

            if body.reconciled_text and body.reconciled_text.strip():
                reconciled = body.reconciled_text.strip()[:500]
                method = "manual"
            else:
                reconciled, method = generate_reconciliation(
                    rec_a.compressed_fact,
                    rec_b.compressed_fact,
                    explanation=explanation,
                    conflict_kind=conflict_kind,
                    use_ai=body.use_ai,
                )

            new_id = mem.reconcile_torsion(
                body.signature_a_id,
                body.signature_b_id,
                reconciled,
            )
            now = datetime.now(tz=timezone.utc)
            new_node = _node_payload(mem, new_id, now)

        return {
            "ok": True,
            "method": method,
            "reconciled_text": reconciled,
            "new_memory_id": new_id,
            "deleted_ids": [body.signature_a_id, body.signature_b_id],
            "node": new_node,
        }

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        storage_ok = False
        try:
            with Memory(store=app.state.store, user=app.state.user) as mem:
                storage_ok = mem._storage.ping()
        except Exception as exc:
            logger.warning("[PDM Explorer] health storage check failed: %s", exc)
        return {
            "status": "ok",
            "store": app.state.store,
            "user": app.state.user,
            "storage_ok": storage_ok,
        }

    return app


def run_server(
    store: str = "./pdm_memory.db",
    user: str = "default",
    host: str = "127.0.0.1",
    port: int = 8080,
    open_browser: bool = True,
) -> None:
    """Start uvicorn and optionally open the system browser."""
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "PDM Explorer requires FastAPI + uvicorn.\n"
            'Install with:  pip install "pdm-memory[ui]"'
        ) from exc

    import threading
    import time
    import webbrowser

    app = create_app(store=store, user=user)
    url = f"http://{host}:{port}"

    if open_browser:
        def _open() -> None:
            time.sleep(0.8)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    logger.info("[PDM Explorer] Serving %s  store=%s user=%s", url, store, user)
    print(f"PDM Explorer → {url}")
    print(f"  store={store}  user={user}")
    print("  Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="info")
