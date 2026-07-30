"""Deterministic enforcement for numerical, role, and spatial constraints."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from pdm_memory.core.signature import SignatureRecord

# Capture the full unit token. "ms"/"kg"/"kW" are units, not SI multipliers.
# Standalone "k"/"m" after a number remain thousand/million shortcuts.
_NUMBER_RE = re.compile(
    r"(?P<currency>[$€£])?\s*"
    r"(?P<value>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*(?P<unit>[A-Za-z%]+))?"
)
_PURE_MULTIPLIER_UNITS: frozenset[str] = frozenset({"k", "m"})
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")
_ONLY_ACTOR_RE = re.compile(r"\bonly\s+([A-Za-z][A-Za-z0-9_.-]*)\b", re.IGNORECASE)
_ONLY_PARENTHETICAL_ACTOR_RE = re.compile(
    r"\bonly\b[^.\n]{0,80}?\(([A-Za-z][A-Za-z0-9_.-]*)\)",
    re.IGNORECASE,
)
_EXPLICIT_ACTOR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bas\s+([A-Za-z][A-Za-z0-9_.-]*)\b", re.IGNORECASE),
    re.compile(r"\bby\s+([A-Za-z][A-Za-z0-9_.-]*)\b", re.IGNORECASE),
    re.compile(r"\bfrom\s+([A-Za-z][A-Za-z0-9_.-]*)\b", re.IGNORECASE),
    re.compile(r"\bactor\s*[:=]\s*([A-Za-z][A-Za-z0-9_.-]*)\b", re.IGNORECASE),
)
_BOT_ACTOR_RE = re.compile(
    r"\b([A-Za-z0-9_.-]*(?:bot|agent)[A-Za-z0-9_.-]*)\b",
    re.IGNORECASE,
)
# "Only one person..." is cardinality, not an actor named "one".
_CARDINALITY_NAME_BLOCKLIST: frozenset[str] = frozenset(
    {"a", "an", "one", "single", "two", "three", "four", "five"}
)
_EXCLUSIVE_SLOT_RE = re.compile(
    r"\b(?:only|at\s+most)\s+one\b|\bone\s+\w+\s+at\s+a\s+time\b|\bsingle[- ]person\b",
    re.IGNORECASE,
)
_LOCATION_IN_RE = re.compile(
    r"\bin\s+(?:the\s+)?(?P<location>[A-Za-z0-9][A-Za-z0-9 _.-]*?)"
    r"(?:\s+at\s+a\s+time)?\s*$",
    re.IGNORECASE,
)
_PRESENCE_RE = re.compile(
    r"^\s*(?P<entity>[A-Za-z][A-Za-z0-9_.-]*)\s+is\s+in\s+(?:the\s+)?"
    r"(?P<location>[A-Za-z0-9][A-Za-z0-9 _.-]*?)\s*$",
    re.IGNORECASE,
)
_ADMISSION_RE = re.compile(
    r"\b(?:let|admit|allow|send)\s+(?P<entity>[A-Za-z][A-Za-z0-9_.-]*)\s+"
    r"(?:into|in|to)\s+(?:the\s+)?(?P<location>[A-Za-z0-9][A-Za-z0-9 _.-]*?)\s*$",
    re.IGNORECASE,
)
_LOCATION_STOPWORDS: frozenset[str] = frozenset(
    {"a", "an", "at", "in", "one", "person", "the", "time", "allowed"}
)

_LIMIT_CUES: frozenset[str] = frozenset(
    {"cap", "capped", "limit", "limited", "max", "maximum", "ceiling"}
)
_TOPIC_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "are",
        "for",
        "from",
        "has",
        "have",
        "into",
        "only",
        "per",
        "that",
        "this",
        "week",
        "weekly",
        "with",
    }
    | _LIMIT_CUES
)
_GENERIC_ACTION_WORDS: frozenset[str] = frozenset(
    {
        "approve",
        "authorise",
        "authorize",
        "execute",
        "need",
        "request",
        "scale",
        "scaling",
        "spend",
        "try",
        "trying",
        "want",
        "wants",
    }
)
_ROLE_SCOPE_WORDS: frozenset[str] = frozenset(
    {
        "approve",
        "authorise",
        "authorize",
        "bypass",
        "deploy",
        "execute",
        "migrate",
        "migration",
        "override",
        "release",
        "ship",
    }
)
_TOKEN_ALIASES: dict[str, str] = {
    "authorization": "authorize",
    "authorizations": "authorize",
    "authorizes": "authorize",
    "bypassed": "bypass",
    "bypassing": "bypass",
    "migrated": "migrate",
    "migrating": "migrate",
    "migration": "migrate",
    "migrations": "migrate",
    "overridden": "bypass",
    "override": "bypass",
    "overrides": "bypass",
}


@dataclass(frozen=True, slots=True)
class ConstraintViolation:
    """One hard rule violation suitable for GAA and Reverse Resonance."""

    kind: str
    strength: float
    topic_similarity: float
    explanation: str


@dataclass(frozen=True, slots=True)
class _Magnitude:
    value: float
    currency: str
    display: str | None = None


@dataclass(frozen=True, slots=True)
class ExclusiveSlot:
    """A capacity-limited physical/logical place from a Goal Anchor."""

    capacity: int
    location: str
    display_location: str


@dataclass(frozen=True, slots=True)
class OccupancyFact:
    """One entity occupying (or requesting) a place."""

    entity: str
    location: str
    display_location: str
    source_id: str | None = None
    source_text: str | None = None


def detect_constraint_violation(
    rule: SignatureRecord,
    candidate_text: str,
    *,
    candidate_tags: Sequence[str] = (),
    occupancy_records: Sequence[SignatureRecord] = (),
) -> ConstraintViolation | None:
    """Return a hard violation when candidate text breaks a stored rule."""
    rule_text = (rule.compressed_fact or "").strip()
    candidate = (candidate_text or "").strip()
    if not rule_text or not candidate:
        return None

    magnitude = _detect_magnitude_clash(
        rule_text,
        rule.intent_tags or [],
        candidate,
        candidate_tags,
    )
    role = _detect_role_violation(
        rule_text,
        rule.intent_tags or [],
        candidate,
        candidate_tags,
    )
    occupancy = _detect_occupancy_violation(
        rule_text,
        candidate,
        occupancy_records=occupancy_records,
    )
    violations = [violation for violation in (magnitude, role, occupancy) if violation is not None]
    return max(violations, key=lambda violation: violation.strength, default=None)


def parse_exclusive_slot(rule_text: str) -> ExclusiveSlot | None:
    """Parse 'Only one person allowed in the Server Room at a time'."""
    text = (rule_text or "").strip()
    if not text or _EXCLUSIVE_SLOT_RE.search(text) is None:
        return None
    location_match = _LOCATION_IN_RE.search(text)
    if location_match is None:
        return None
    display = _clean_location(location_match.group("location"))
    location = _normalize_location(display)
    if not location:
        return None
    return ExclusiveSlot(capacity=1, location=location, display_location=display)


def parse_presence(text: str, *, source_id: str | None = None) -> OccupancyFact | None:
    """Parse 'Roman is in the Server Room'."""
    match = _PRESENCE_RE.match((text or "").strip())
    if match is None:
        return None
    display = _clean_location(match.group("location"))
    location = _normalize_location(display)
    entity = match.group("entity").strip()
    if not location or not entity:
        return None
    return OccupancyFact(
        entity=entity,
        location=location,
        display_location=display,
        source_id=source_id,
        source_text=(text or "").strip(),
    )


def parse_admission(text: str) -> OccupancyFact | None:
    """Parse 'Let Vitalii into the Server Room'."""
    match = _ADMISSION_RE.search((text or "").strip())
    if match is None:
        return None
    display = _clean_location(match.group("location"))
    location = _normalize_location(display)
    entity = match.group("entity").strip()
    if not location or not entity:
        return None
    return OccupancyFact(
        entity=entity,
        location=location,
        display_location=display,
        source_text=(text or "").strip(),
    )


def collect_occupants(
    records: Sequence[SignatureRecord],
    *,
    location: str,
) -> list[OccupancyFact]:
    """Unique present entities for a normalized location."""
    by_entity: dict[str, OccupancyFact] = {}
    for record in records:
        if _is_rule_like(record):
            continue
        presence = parse_presence(record.compressed_fact or "", source_id=record.id)
        if presence is None or presence.location != location:
            continue
        by_entity[presence.entity.casefold()] = presence
    return list(by_entity.values())


def entity_exclusion_pair(
    left: OccupancyFact,
    right: OccupancyFact,
    *,
    slot: ExclusiveSlot,
) -> ConstraintViolation | None:
    """Two distinct occupants of a capacity-1 slot are an exclusion torsion."""
    if left.location != right.location or left.location != slot.location:
        return None
    if left.entity.casefold() == right.entity.casefold():
        return None
    if slot.capacity != 1:
        return None
    explanation = (
        f"Entity Exclusion: '{left.entity}' and '{right.entity}' both occupy "
        f"single-person slot '{slot.display_location}'."
    )
    return ConstraintViolation(
        kind="entity_exclusion",
        strength=1.0,
        topic_similarity=1.0,
        explanation=explanation,
    )


def _detect_occupancy_violation(
    rule_text: str,
    candidate_text: str,
    *,
    occupancy_records: Sequence[SignatureRecord],
) -> ConstraintViolation | None:
    slot = parse_exclusive_slot(rule_text)
    if slot is None:
        return None

    admission = parse_admission(candidate_text)
    if admission is None:
        # Existing presence facts are compared pairwise under exclusive slots.
        # Admission intents ("Let X into ...") use the occupancy projection below.
        return None
    request = admission
    if request.location != slot.location:
        return None

    occupants = collect_occupants(occupancy_records, location=slot.location)
    occupant_names = {item.entity.casefold() for item in occupants}
    projected = set(occupant_names)
    projected.add(request.entity.casefold())
    if len(projected) <= slot.capacity:
        return None

    current = ", ".join(sorted(item.entity for item in occupants)) or "(empty)"
    explanation = (
        f"Intent violates exclusive occupancy of '{slot.display_location}' "
        f"(capacity {slot.capacity}): current occupants [{current}]; "
        f"requested '{request.entity}'."
    )
    return ConstraintViolation(
        kind="entity_exclusion",
        strength=1.0,
        topic_similarity=1.0,
        explanation=explanation,
    )


def _is_rule_like(record: SignatureRecord) -> bool:
    metadata = record.metadata or {}
    if metadata.get("is_anchor") or metadata.get("role") in {
        "anchor",
        "goal",
        "stewardship",
    }:
        return True
    tags = {tag.lower() for tag in (record.intent_tags or []) if tag}
    return bool(tags & {"goal", "anchor", "stewardship", "policy", "rule"})


def _clean_location(raw: str) -> str:
    parts = [
        part
        for part in re.split(r"\s+", (raw or "").strip())
        if part and part.casefold() not in _LOCATION_STOPWORDS
    ]
    return " ".join(parts)


def _normalize_location(raw: str) -> str:
    return " ".join(part.casefold() for part in re.split(r"\s+", _clean_location(raw)) if part)


def _detect_magnitude_clash(
    rule_text: str,
    rule_tags: Sequence[str],
    candidate_text: str,
    candidate_tags: Sequence[str],
) -> ConstraintViolation | None:
    rule_tokens = _word_tokens(rule_text)
    if not (rule_tokens & _LIMIT_CUES):
        return None

    limits = _magnitudes(rule_text)
    requested = _magnitudes(candidate_text)
    if not limits or not requested:
        return None

    limit = limits[0]
    comparable = [
        value
        for value in requested
        if not limit.currency or not value.currency or value.currency == limit.currency
    ]
    if not comparable:
        return None
    request = max(comparable, key=lambda value: value.value)
    if request.value <= limit.value:
        return None

    topic_similarity, shared = _topic_similarity(
        rule_text,
        rule_tags,
        candidate_text,
        candidate_tags,
    )
    if not shared:
        return None

    topic = _topic_label(shared, rule_tokens)
    explanation = (
        f"Intent violates {topic} cap of {_format_magnitude(limit)}: "
        f"requested {_format_magnitude(request)}."
    )
    return ConstraintViolation(
        kind="magnitude_clash",
        strength=1.0,
        topic_similarity=topic_similarity,
        explanation=explanation,
    )


def _detect_role_violation(
    rule_text: str,
    rule_tags: Sequence[str],
    candidate_text: str,
    candidate_tags: Sequence[str],
) -> ConstraintViolation | None:
    required_actor = _required_actor(rule_text)
    if required_actor is None:
        return None
    actual_actor = _candidate_actor(candidate_text)
    if actual_actor is None or actual_actor.casefold() == required_actor.casefold():
        return None

    rule_scope = _normalized_tokens(f"{rule_text} {' '.join(rule_tags)}") & _ROLE_SCOPE_WORDS
    candidate_scope = (
        _normalized_tokens(f"{candidate_text} {' '.join(candidate_tags)}") & _ROLE_SCOPE_WORDS
    )
    topic_similarity, shared = _topic_similarity(
        rule_text,
        rule_tags,
        candidate_text,
        candidate_tags,
    )
    if not shared and not (rule_scope & candidate_scope):
        return None

    scope = sorted(rule_scope & candidate_scope)
    action = scope[0] if scope else "protected action"
    action = {
        "authorize": "authorization",
        "migrate": "migration",
    }.get(action, action)
    explanation = (
        f"Intent assigns {action} to '{actual_actor}', "
        f"but the Goal Anchor requires '{required_actor}'."
    )
    return ConstraintViolation(
        kind="role_violation",
        strength=1.0,
        topic_similarity=topic_similarity,
        explanation=explanation,
    )


def _candidate_actor(text: str) -> str | None:
    for pattern in _EXPLICIT_ACTOR_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return match.group(1)
    bot_match = _BOT_ACTOR_RE.search(text)
    return bot_match.group(1) if bot_match is not None else None


def _required_actor(text: str) -> str | None:
    parenthetical = _ONLY_PARENTHETICAL_ACTOR_RE.search(text)
    if parenthetical is not None:
        return parenthetical.group(1)
    # Cardinality rules ("Only one person...") are not role constraints.
    if _EXCLUSIVE_SLOT_RE.search(text) is not None:
        return None
    direct = _ONLY_ACTOR_RE.search(text)
    if direct is None:
        return None
    actor = direct.group(1)
    if actor.casefold() in _CARDINALITY_NAME_BLOCKLIST:
        return None
    return actor


def _magnitudes(text: str) -> list[_Magnitude]:
    values: list[_Magnitude] = []
    for match in _NUMBER_RE.finditer(text):
        raw_number = match.group("value")
        value = float(raw_number.replace(",", ""))
        currency = match.group("currency") or ""
        unit = match.group("unit") or ""
        unit_key = unit.casefold()
        display: str | None = None

        if currency:
            values.append(_Magnitude(value=value, currency=currency, display=None))
            continue

        if unit_key in _PURE_MULTIPLIER_UNITS:
            if unit_key == "k":
                value *= 1_000
            else:
                value *= 1_000_000
            display = f"{raw_number}{unit}"
        elif unit:
            display = f"{raw_number} {unit}".strip()

        values.append(_Magnitude(value=value, currency="", display=display))
    return values


def _topic_similarity(
    rule_text: str,
    rule_tags: Sequence[str],
    candidate_text: str,
    candidate_tags: Sequence[str],
) -> tuple[float, set[str]]:
    rule = _topic_tokens(rule_text, rule_tags)
    candidate = _topic_tokens(candidate_text, candidate_tags)
    shared = rule & candidate
    if not rule or not candidate:
        return 0.0, shared
    containment = len(shared) / max(min(len(rule), len(candidate)), 1)
    return min(1.0, containment), shared


def _topic_tokens(text: str, tags: Sequence[str]) -> set[str]:
    tokens = _normalized_tokens(f"{text} {' '.join(tags)}")
    return tokens - _TOPIC_STOPWORDS - _GENERIC_ACTION_WORDS - _ROLE_SCOPE_WORDS


def _normalized_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for token in _word_tokens(text):
        tokens.add(token)
        alias = _TOKEN_ALIASES.get(token)
        if alias is not None:
            tokens.add(alias)
        stemmed = _stem(token)
        tokens.add(stemmed)
        tokens.add(_stem(stemmed))
    return tokens


def _word_tokens(text: str) -> set[str]:
    return {token.casefold() for token in _WORD_RE.findall(text or "")}


def _stem(token: str) -> str:
    if token.endswith("tion") and len(token) > 6:
        return token[:-4] + "e"
    if token.endswith("ing") and len(token) > 5:
        base = token[:-3]
        return base[:-1] if base.endswith("pp") else base
    if token.endswith("s") and len(token) > 4 and not token.endswith("ss"):
        return token[:-1]
    return token


def _topic_label(shared: set[str], rule_tokens: set[str]) -> str:
    candidates = shared - {
        "anchor",
        "budget",
        "goal",
        "gw",
        "kva",
        "kw",
        "kwh",
        "mw",
        "policy",
        "rule",
        "stewardship",
        "va",
        "w",
        "wh",
        "ms",
        "kg",
        "c",
        "deg",
        "degree",
        "degrees",
    }
    text_candidates = candidates & (rule_tokens - _TOPIC_STOPWORDS)
    raw_topic = sorted(text_candidates or candidates or shared)[0]
    topic = raw_topic.upper() if len(raw_topic) <= 3 else raw_topic
    if "budget" in rule_tokens or "spend" in rule_tokens:
        return f"{topic} budget"
    return topic


def _format_magnitude(value: _Magnitude) -> str:
    if value.display:
        return value.display
    number = (
        f"{value.value:,.0f}"
        if value.value.is_integer()
        else f"{value.value:,.2f}".rstrip("0").rstrip(".")
    )
    return f"{value.currency}{number}"
