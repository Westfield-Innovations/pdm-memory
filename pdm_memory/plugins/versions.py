# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# AUTHORIZED EXTENSION POINT (Westfield OS) — implementing this interface
# (e.g. BasePDMPlugin) is PERMITTED. Core software modification remains
# prohibited without a commercial license from Westfield Innovations LLC.

"""Semver-ish requirement parsing for plugin ``requires``."""

from __future__ import annotations

import re
from dataclasses import dataclass

_REQ_RE = re.compile(
    r"^\s*([A-Za-z_][\w-]*)\s*(>=|<=|==|!=|>|<)?\s*([0-9][0-9A-Za-z.\-]*)?\s*$"
)


@dataclass(frozen=True, slots=True)
class PluginRequirement:
    """One dependency: name plus optional version constraint."""

    name: str
    operator: str | None = None
    version: str | None = None

    @property
    def raw(self) -> str:
        if self.operator and self.version:
            return f"{self.name}{self.operator}{self.version}"
        return self.name


def parse_requirement(spec: str) -> PluginRequirement:
    """
    Parse ``GeoTagger``, ``GeoTagger>=1.2``, ``Foo==1.0.0``.

    Raises:
        ValueError: If the spec is empty or malformed.
    """
    text = str(spec).strip()
    if not text:
        raise ValueError("Empty plugin requirement")
    match = _REQ_RE.match(text)
    if not match:
        raise ValueError(f"Invalid plugin requirement: {spec!r}")
    name, op, ver = match.group(1), match.group(2), match.group(3)
    if op and not ver:
        raise ValueError(f"Requirement {spec!r} has operator but no version")
    if ver and not op:
        op = ">="
    return PluginRequirement(name=name, operator=op, version=ver)


def parse_requirements(specs: list[str] | tuple[str, ...] | None) -> list[PluginRequirement]:
    """Parse a list of requirement strings; drop empties; fail-fast on bad specs."""
    if not specs:
        return []
    out: list[PluginRequirement] = []
    seen: set[str] = set()
    for raw in specs:
        req = parse_requirement(str(raw))
        if req.name in seen:
            raise ValueError(f"Duplicate requirement for plugin {req.name!r}")
        seen.add(req.name)
        out.append(req)
    return out


def parse_version_tuple(version: str) -> tuple[int, ...]:
    """Best-effort numeric version tuple (``1.2.3rc1`` → ``(1, 2, 3)``)."""
    parts: list[int] = []
    for chunk in str(version).strip().split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else (0,)


def version_satisfies(installed: str, operator: str, required: str) -> bool:
    """Compare ``installed`` against ``required`` using ``operator``."""
    left = parse_version_tuple(installed)
    right = parse_version_tuple(required)
    # Pad to same length for comparison.
    width = max(len(left), len(right))
    left = left + (0,) * (width - len(left))
    right = right + (0,) * (width - len(right))
    match operator:
        case ">=":
            return left >= right
        case "<=":
            return left <= right
        case ">":
            return left > right
        case "<":
            return left < right
        case "==":
            return left == right
        case "!=":
            return left != right
        case _:
            raise ValueError(f"Unsupported version operator: {operator!r}")


def check_requirement(
    req: PluginRequirement,
    *,
    installed_version: str | None,
) -> str | None:
    """
    Return an error message if requirement is not met, else ``None``.

    ``installed_version is None`` means the plugin is not installed.
    """
    if installed_version is None:
        return f"'{req.raw}'"
    if not req.operator or not req.version:
        return None
    if version_satisfies(installed_version, req.operator, req.version):
        return None
    return (
        f"'{req.raw}' (installed {installed_version})"
    )
