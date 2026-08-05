# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
SDK-facing report models (no Django dependency).

Keep these stable: they are part of the public API surface for tooling/CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TorsionReport:
    """
    One Reverse Resonance hit: two signatures about the same topic that disagree.

    torsion_score in [0, 1] — product of topic similarity and contradiction strength.
    """

    signature_a_id: str
    signature_b_id: str
    signature_a_text: str
    signature_b_text: str
    drawer: str
    domain: str
    torsion_score: float
    topic_similarity: float
    contradiction_strength: float
    explanation: str
    conflict_kind: str  # deadline | factual | polarity | pressure | semantic

    cluster_key: str | None = None

    def render(self) -> str:
        """Human-readable one-liner for CLI / logs."""
        return (
            f"[{self.torsion_score:.2f}] {self.conflict_kind} | "
            f"{self.drawer}/{self.domain}\n"
            f"  {self.explanation}"
        )


@dataclass(slots=True)
class AlignmentReport:
    """
    Goal-Anchor Alignment (GAA) result for a proposed intent / ACT.

    status:
      ALIGNED  — intent resonates with high-IAW goals, low deviation
      CONFLICT — soft mismatch or insufficient anchor coverage
      TORSION  — intent contradicts a core goal (guarded agents must block ACT)
    score: composite alignment in [0, 1] (higher = safer / more aligned)
    """

    status: str
    score: float
    conflicting_goals: list[str] = field(default_factory=list)
    explanation: str = ""
    resonance: float = 0.0
    torsion: float = 0.0
    anchor_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": round(float(self.score), 4),
            "conflicting_goals": list(self.conflicting_goals),
            "explanation": self.explanation,
        }

    def render(self) -> str:
        goals = "; ".join(self.conflicting_goals[:3]) if self.conflicting_goals else "(none)"
        return (
            f"[{self.status}] score={self.score:.3f} "
            f"resonance={self.resonance:.3f} torsion={self.torsion:.3f}\n"
            f"  {self.explanation}\n"
            f"  conflicting_goals: {goals}"
        )

    @property
    def is_safe_to_act(self) -> bool:
        """True only when a guarded agent may proceed with ACT."""
        return self.status == "ALIGNED"


@dataclass(slots=True)
class MemoryListPage:
    """Keyset-paginated list of memories."""

    items: list[Any]
    next_cursor_id: str | None = None


@dataclass(slots=True)
class SurfaceReport:
    """
    Lite agent-loop snapshot: recall + torsion scan + alignment gate for one query.
    """

    hits: list[Any]
    torsion_count: int
    alignment: str
    alignment_score: float = 0.0
    torsion_reports: list[TorsionReport] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": [
                {
                    "id": h.id,
                    "text": h.text,
                    "pressure": round(float(h.pressure), 2),
                    "p_raw": round(float(h.p_raw), 2),
                    "drawer": h.drawer,
                    "coupling_score": round(float(h.coupling_score), 4),
                    "tags": list(h.intent_tags),
                }
                for h in self.hits
            ],
            "torsion_count": self.torsion_count,
            "alignment": self.alignment,
            "alignment_score": round(float(self.alignment_score), 4),
        }


# ---------------------------------------------------------------------------
# Memory / plugin status (Alive panel)
# ---------------------------------------------------------------------------


def _ansi_enabled(color: bool | None) -> bool:
    """Auto-detect TTY; honor ``NO_COLOR`` unless ``color`` is set explicitly."""
    import os
    import sys

    if color is False:
        return False
    if color is True:
        return True
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


@dataclass(slots=True)
class PluginStatusEntry:
    """One installed plugin as seen by :meth:`Memory.status`."""

    name: str
    version: str
    class_name: str
    hooks: list[str] = field(default_factory=list)
    source: str = "manual"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "class_name": self.class_name,
            "hooks": list(self.hooks),
            "source": self.source,
        }


@dataclass(slots=True)
class MemoryStatusReport:
    """
    Structured + printable snapshot of Memory health and plugin map.

    Use::

        report = mem.status()
        print(report)                 # ANSI when stdout is a TTY
        print(report.render(color=True))
        data = report.as_dict()
    """

    alive: bool
    user: str
    store: str
    sdk_version: str
    memory_count: int | None
    plugins: list[PluginStatusEntry] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "alive": self.alive,
            "user": self.user,
            "store": self.store,
            "sdk_version": self.sdk_version,
            "memory_count": self.memory_count,
            "plugins": [p.as_dict() for p in self.plugins],
        }

    def render(self, *, color: bool | None = None) -> str:
        """Human-readable panel; ``color=None`` auto-detects TTY / ``NO_COLOR``."""
        use_color = _ansi_enabled(color)

        def c(code: str, text: str) -> str:
            if not use_color:
                return text
            return f"\033[{code}m{text}\033[0m"

        # Styles: bold, dim, green, cyan, yellow, magenta, red, blue
        bold = lambda t: c("1", t)
        dim = lambda t: c("2", t)
        green = lambda t: c("32", t)
        cyan = lambda t: c("36", t)
        yellow = lambda t: c("33", t)
        magenta = lambda t: c("35", t)
        red = lambda t: c("31", t)
        blue = lambda t: c("34", t)
        title_style = lambda t: c("1;36", t)

        width = 52
        top = "╔" + "═" * width + "╗"
        mid = "╠" + "═" * width + "╣"
        soft = "╠" + "─" * width + "╣"
        bot = "╚" + "═" * width + "╝"

        def row(content: str) -> str:
            # Strip ANSI for padding width
            plain = content
            if "\033[" in content:
                import re

                plain = re.sub(r"\033\[[0-9;]*m", "", content)
            pad = max(0, width - len(plain))
            return f"║ {content}{' ' * pad}║"

        status_dot = green("● ALIVE") if self.alive else red("● DEAD")
        title = f"{title_style('PDM Memory')}  {status_dot}"

        lines = [
            top,
            row(title),
            mid,
            row(f"{dim('user')}    {self.user}"),
            row(f"{dim('store')}   {blue(self.store)}"),
            row(f"{dim('sdk')}     v{self.sdk_version}"),
        ]
        if self.memory_count is not None:
            lines.append(row(f"{dim('memories')} {self.memory_count}"))

        lines.append(soft)
        lines.append(row(bold(f"Plugins ({len(self.plugins)})")))

        if not self.plugins:
            lines.append(row(dim("(none — brain is stock)")))
        else:
            for plugin in self.plugins:
                hooks = ", ".join(plugin.hooks) if plugin.hooks else "—"
                label = green("[Plugin]")
                name = cyan(plugin.name)
                ver = yellow(f"v{plugin.version}")
                lines.append(row(f"{label} {name} {ver}"))
                lines.append(row(f"         {dim('Hooks:')} {magenta(hooks)}"))
                source = plugin.source or "manual"
                if len(source) > 40:
                    source = "…" + source[-39:]
                lines.append(row(f"         {dim('Source:')} {blue(source)}"))

        lines.append(bot)
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.render(color=None)
