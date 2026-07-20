# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Identity Health Dashboard — one-glance substrate diagnostics for pdm-cli status."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pdm_memory.core.math import calculate_v

if TYPE_CHECKING:
    from pdm_memory.memory import Memory


@dataclass(slots=True)
class HealthReport:
    store: str
    user: str
    integrity_pct: float
    memory_density: str
    torsion_level: str
    storage_ok: bool
    total_memories: int
    torsion_count: int
    peak_torsion: float


def _ascii_bar(pct: float, width: int = 10) -> str:
    clamped = max(0.0, min(100.0, pct))
    filled = round(width * clamped / 100.0)
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def _memory_density_label(total: int, avg_pressure: float) -> str:
    if total == 0:
        return "Empty"
    score = total + (avg_pressure / 10.0)
    if score >= 60:
        return "Very High"
    if score >= 30:
        return "High"
    if score >= 10:
        return "Medium"
    return "Low"


def _torsion_level_label(count: int, peak: float) -> str:
    if count == 0:
        return "Zero"
    if peak >= 0.90 or count >= 5:
        return "Critical"
    if peak >= 0.75 or count >= 3:
        return "Moderate"
    return "Low"


def _compute_integrity_pct(
    *,
    total: int,
    avg_v: float,
    torsion_count: int,
    peak_torsion: float,
    storage_ok: bool,
    avg_pressure: float,
) -> float:
    if not storage_ok:
        return max(0.0, 40.0 - torsion_count * 5.0)

    if total == 0:
        return 100.0

    v_component = avg_v * 100.0
    torsion_penalty = min(55.0, torsion_count * 12.0 + peak_torsion * 25.0)
    torsion_component = max(0.0, 100.0 - torsion_penalty)
    vitality = min(100.0, (avg_pressure / 100.0) * 100.0)

    integrity = (
        0.40 * v_component
        + 0.35 * torsion_component
        + 0.15 * vitality
        + 0.10 * 100.0
    )
    return round(max(0.0, min(100.0, integrity)), 1)


def _storage_ping(mem: Memory, user: str) -> bool:
    ping = getattr(mem._storage, "ping", None)
    if callable(ping):
        return bool(ping())
    try:
        mem._storage.list(user=user, limit=1)
        return True
    except Exception:
        return False


def build_health_report(mem: Memory, *, store: str, user: str) -> HealthReport:
    """Collect substrate health metrics from an open Memory instance."""
    storage_ok = _storage_ping(mem, user)
    total = mem.count()
    records = mem._storage.list(user=user, limit=10_000)

    if records:
        avg_pressure = sum(r.p_magnitude for r in records) / len(records)
        v_values = [
            calculate_v(r.validation_prediction_correct, r.validation_prediction_total)
            for r in records
        ]
        avg_v = sum(v_values) / len(v_values)
    else:
        avg_pressure = 0.0
        avg_v = 1.0

    torsion_reports = mem.detect_torsion(threshold=0.5)
    torsion_count = len(torsion_reports)
    peak_torsion = max((r.torsion_score for r in torsion_reports), default=0.0)

    integrity_pct = _compute_integrity_pct(
        total=total,
        avg_v=avg_v,
        torsion_count=torsion_count,
        peak_torsion=peak_torsion,
        storage_ok=storage_ok,
        avg_pressure=avg_pressure,
    )

    return HealthReport(
        store=store,
        user=user,
        integrity_pct=integrity_pct,
        memory_density=_memory_density_label(total, avg_pressure),
        torsion_level=_torsion_level_label(torsion_count, peak_torsion),
        storage_ok=storage_ok,
        total_memories=total,
        torsion_count=torsion_count,
        peak_torsion=round(peak_torsion, 3),
    )


def render_health_dashboard(report: HealthReport) -> str:
    """Render the Identity Health Dashboard for terminal output."""
    storage_label = "OK" if report.storage_ok else "DEGRADED"
    bar = _ascii_bar(report.integrity_pct)

    lines = [
        "╔══════════════════════════════════════════════════════╗",
        "║       PDM Identity Health Dashboard                  ║",
        "╠══════════════════════════════════════════════════════╣",
        f"  Store:            {report.store}",
        f"  User:             {report.user}",
        "╠──────────────────────────────────────────────────────╣",
        f"  Integrity:        {bar} {report.integrity_pct:5.1f}%",
        f"  Memory Density:   {report.memory_density}",
        f"  Torsion Level:    {report.torsion_level}",
        "╠──────────────────────────────────────────────────────╣",
        f"  Storage:          {storage_label}",
        f"  Memories:         {report.total_memories}",
        f"  Torsion pairs:    {report.torsion_count}",
        "╚══════════════════════════════════════════════════════╝",
    ]
    return "\n".join(lines)
