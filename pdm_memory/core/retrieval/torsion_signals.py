# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Torsion contradiction signals, attribute clash, and polarity helpers."""

from __future__ import annotations

from datetime import timezone

from pdm_memory.core.math import infer_regime
from pdm_memory.core.retrieval.tokenize import NUMBER_PATTERN, WORD_PATTERN
from pdm_memory.core.signature import SignatureRecord
from pdm_memory.models import TorsionReport

_TOPIC_GATE: float = 0.35
_SAME_DRAWER_TOPIC_GATE: float = 0.25
_ATTRIBUTE_TAG_OVERLAP: float = 0.80
_ATTRIBUTE_ROLE_TAGS: frozenset[str] = frozenset(
    {"goal", "anchor", "principle", "policy", "stewardship", "foundational"}
)
_ATTRIBUTE_HINT_TAGS: frozenset[str] = frozenset(
    {
        "date", "deadline", "pressure", "reading", "release", "schedule",
        "state", "status", "time", "value", "version",
    }
)
_TEMPORAL_ATTRIBUTE_VALUES: frozenset[str] = frozenset(
    {
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
        "sunday", "today", "tomorrow", "yesterday",
    }
)
_STATUS_ATTRIBUTE_VALUES: frozenset[str] = frozenset(
    {
        "active", "approved", "blocked", "cancelled", "closed", "complete",
        "completed", "delayed", "done", "failed", "failing", "impossible",
        "inactive", "merged", "moved", "open", "pending", "ready", "rejected",
        "scheduled", "started", "stopped",
    }
)
_NEGATION_TOKENS: frozenset[str] = frozenset(
    {
        "not", "never", "no", "false", "without", "none", "neither", "nor",
        "cannot", "dont", "doesnt", "didnt", "isnt", "arent", "wasnt", "werent",
        "wont", "cant", "shouldnt", "wouldnt", "couldnt",
    }
)
_CONTRACTION_MAP: tuple[tuple[str, str], ...] = (
    ("don't", " do not "),
    ("doesn't", " does not "),
    ("didn't", " did not "),
    ("isn't", " is not "),
    ("aren't", " are not "),
    ("wasn't", " was not "),
    ("weren't", " were not "),
    ("won't", " will not "),
    ("can't", " can not "),
    ("shouldn't", " should not "),
    ("wouldn't", " would not "),
    ("couldn't", " could not "),
    ("cannot", " can not "),
    ("n't", " not "),
)
_ANTONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("prefer", "avoid"),
    ("likes", "hates"),
    ("love", "hate"),
    ("always", "never"),
    ("enable", "disable"),
    ("true", "false"),
    ("yes", "no"),
    ("increase", "decrease"),
    ("allowed", "forbidden"),
    ("required", "optional"),
)


class TorsionSignalsMixin:
    """Contradiction / attribute / polarity scoring for Reverse Resonance."""

    def _score_torsion_pair(
        self,
        a: SignatureRecord,
        b: SignatureRecord,
        *,
        cluster_key: str,
    ) -> TorsionReport | None:
        topic = self._topic_similarity(a, b)
        same_drawer = self._same_drawer(a, b)
        topic_gate = _SAME_DRAWER_TOPIC_GATE if same_drawer else _TOPIC_GATE
        if topic < topic_gate:
            return None

        kind, strength, detail = self._contradiction_signals(a, b, topic)
        if strength <= 0.0:
            return None

        score = round(max(0.0, min(1.0, topic * strength)), 4)
        # A structured date disagreement or high-overlap attribute clash in the
        # same drawer is stronger evidence than lexical topic similarity alone.
        # Keep these visible at the default detect_torsion(threshold=0.7).
        if same_drawer and kind == "deadline":
            score = max(score, round(min(1.0, 0.85 + 0.10 * strength), 4))
        elif same_drawer and kind == "attribute_clash":
            score = max(score, round(min(0.95, 0.75 + 0.20 * strength), 4))
        explanation = self._humanize_torsion(a, b, kind=kind, detail=detail)
        return TorsionReport(
            signature_a_id=a.id,
            signature_b_id=b.id,
            signature_a_text=(a.compressed_fact or "")[:500],
            signature_b_text=(b.compressed_fact or "")[:500],
            drawer=(a.drawer_domain or b.drawer_domain or "general"),
            domain=(a.domain or b.domain or "insight"),
            torsion_score=score,
            topic_similarity=round(topic, 4),
            contradiction_strength=round(strength, 4),
            explanation=explanation,
            conflict_kind=kind,
            cluster_key=cluster_key,
        )

    def _topic_similarity(self, a: SignatureRecord, b: SignatureRecord) -> float:
        """Blend tag Jaccard, token Jaccard, and TAS tag-overlap coupling."""
        tags_a = {t.lower() for t in (a.intent_tags or []) if t}
        tags_b = {t.lower() for t in (b.intent_tags or []) if t}
        if tags_a or tags_b:
            tag_j = len(tags_a & tags_b) / max(len(tags_a | tags_b), 1)
        else:
            tag_j = 0.0

        tok_a = set(self._tokenize_query(a.compressed_fact or ""))
        tok_b = set(self._tokenize_query(b.compressed_fact or ""))
        if tok_a and tok_b:
            tok_j = len(tok_a & tok_b) / max(len(tok_a | tok_b), 1)
        else:
            tok_j = 0.0

        # Reuse TAS tag component: treat A's tags (else tokens) as the "query"
        query_tags = list(tags_a) if tags_a else list(tok_a)
        if query_tags:
            coupling = self._compute_coupling(
                b,
                query_tags=query_tags,
                p_eff=float(b.p_magnitude or 0.0),
                p_raw=float(b.p_magnitude or 0.0),
                effective_domain=(a.domain or None),
                effective_regime=infer_regime(list(tags_a)) if tags_a else None,
                target_pressure=float(a.p_magnitude or 50.0),
            )
            coupling_tag = coupling.tag_overlap
        else:
            coupling_tag = 0.0
        return max(0.0, min(1.0, 0.45 * tag_j + 0.35 * tok_j + 0.20 * coupling_tag))

    def _contradiction_signals(
        self,
        a: SignatureRecord,
        b: SignatureRecord,
        topic: float,
    ) -> tuple[str, float, str]:
        """Return (kind, strength, detail). Strength in [0, 1]."""
        # Prefer structured deadline over numeric bleed from the same dates in text
        if a.t_deadline is not None and b.t_deadline is not None:
            da = a.t_deadline if a.t_deadline.tzinfo else a.t_deadline.replace(tzinfo=timezone.utc)
            db = b.t_deadline if b.t_deadline.tzinfo else b.t_deadline.replace(tzinfo=timezone.utc)
            delta_days = abs((da - db).total_seconds()) / 86400.0
            if delta_days >= 1.0:
                strength = min(1.0, 0.7 + delta_days / 20.0)
                return (
                    "deadline",
                    strength,
                    f"{da.date().isoformat()} vs {db.date().isoformat()}",
                )

        best_kind = "semantic"
        best_strength = 0.0
        best_detail = ""

        # Numeric disagreement in otherwise similar facts
        nums_a = self._standalone_numbers(a.compressed_fact or "")
        nums_b = self._standalone_numbers(b.compressed_fact or "")
        if nums_a and nums_b and nums_a != nums_b and topic >= 0.4:
            strength = min(1.0, 0.5 + 0.5 * topic)
            detail = f"{self._format_numbers(nums_a)} vs {self._format_numbers(nums_b)}"
            if strength > best_strength:
                best_kind, best_strength, best_detail = "factual", strength, detail

        attribute = self._attribute_clash(a, b)
        if attribute is not None:
            strength, detail = attribute
            if strength > best_strength:
                best_kind, best_strength, best_detail = (
                    "attribute_clash",
                    strength,
                    detail,
                )

        # Negation / antonym polarity on shared content
        norm_a = self._normalize_polarity_text(a.compressed_fact or "")
        norm_b = self._normalize_polarity_text(b.compressed_fact or "")
        tok_a = set(self._tokenize_query(norm_a))
        tok_b = set(self._tokenize_query(norm_b))
        content_a = tok_a - _NEGATION_TOKENS
        content_b = tok_b - _NEGATION_TOKENS
        content_overlap = (
            len(content_a & content_b) / max(len(content_a | content_b), 1)
            if content_a or content_b
            else 0.0
        )
        neg_a = self._has_negation(norm_a)
        neg_b = self._has_negation(norm_b)
        if neg_a != neg_b and content_overlap >= 0.28:
            strength = min(1.0, 0.45 + 0.55 * content_overlap)
            if strength > best_strength:
                best_kind, best_strength, best_detail = (
                    "polarity",
                    strength,
                    "one affirms, the other negates the shared topic",
                )

        text_blob_a = f"{' '.join(a.intent_tags or [])} {norm_a}".lower()
        text_blob_b = f"{' '.join(b.intent_tags or [])} {norm_b}".lower()
        for left, right in _ANTONYM_PAIRS:
            a_has_l, a_has_r = left in text_blob_a, right in text_blob_a
            b_has_l, b_has_r = left in text_blob_b, right in text_blob_b
            crossed = (a_has_l and b_has_r) or (a_has_r and b_has_l)
            if crossed and topic >= 0.35:
                strength = min(1.0, 0.6 + 0.4 * topic)
                if strength > best_strength:
                    best_kind, best_strength, best_detail = (
                        "polarity",
                        strength,
                        f"opposing cues '{left}' / '{right}'",
                    )

        # Opposing pressure vectors (weaker; needs solid topic match)
        p_delta = abs(float(a.p_magnitude or 0.0) - float(b.p_magnitude or 0.0))
        if topic >= 0.55 and p_delta >= 40.0:
            strength = min(0.85, (p_delta / 100.0) * topic)
            if strength > best_strength:
                best_kind, best_strength, best_detail = (
                    "pressure",
                    strength,
                    f"P={a.p_magnitude:.0f} vs P={b.p_magnitude:.0f}",
                )

        return best_kind, best_strength, best_detail

    def _attribute_clash(
        self,
        a: SignatureRecord,
        b: SignatureRecord,
    ) -> tuple[float, str] | None:
        """
        Detect different attribute values for the same entity/topic.

        This is deliberately scoped to the same drawer and requires at least
        80% tag-overlap after removing role tags and mutable weekday/status
        values. It catches "Release Friday" vs "Release Saturday" without
        pretending Friday/Saturday are linguistic antonyms.
        """
        if not self._same_drawer(a, b):
            return None

        tags_a = self._attribute_identity_tags(a)
        tags_b = self._attribute_identity_tags(b)
        if not tags_a or not tags_b:
            return None
        overlap = len(tags_a & tags_b) / max(min(len(tags_a), len(tags_b)), 1)
        if overlap < _ATTRIBUTE_TAG_OVERLAP:
            return None

        tokens_a = set(self._tokenize_query(a.compressed_fact or ""))
        tokens_b = set(self._tokenize_query(b.compressed_fact or ""))
        values_a = tokens_a & (_TEMPORAL_ATTRIBUTE_VALUES | _STATUS_ATTRIBUTE_VALUES)
        values_b = tokens_b & (_TEMPORAL_ATTRIBUTE_VALUES | _STATUS_ATTRIBUTE_VALUES)
        categorical_diff = values_a != values_b and bool(values_a or values_b)

        tail_a = self._trailing_attribute(a.compressed_fact or "")
        tail_b = self._trailing_attribute(b.compressed_fact or "")
        trailing_diff = bool(tail_a and tail_b and tail_a != tail_b)
        has_attribute_context = bool((tags_a | tags_b) & _ATTRIBUTE_HINT_TAGS)
        # Arbitrary different nouns are not automatically conflicting attributes
        # ("football" vs "thing" belongs to an optional semantic judge).
        if trailing_diff and not categorical_diff and not has_attribute_context:
            trailing_diff = False
        if not categorical_diff and not trailing_diff:
            return None

        strength = 0.85
        if categorical_diff:
            strength = 1.0
        detail_values_a = sorted(values_a) or ([tail_a] if tail_a else [])
        detail_values_b = sorted(values_b) or ([tail_b] if tail_b else [])
        detail = (
            f"shared tags={overlap:.0%}; "
            f"attribute values {detail_values_a or ['unknown']} "
            f"vs {detail_values_b or ['unknown']}"
        )
        return strength, detail

    @staticmethod
    def _same_drawer(a: SignatureRecord, b: SignatureRecord) -> bool:
        drawer_a = (a.drawer_domain or "general").strip().lower() or "general"
        drawer_b = (b.drawer_domain or "general").strip().lower() or "general"
        return drawer_a == drawer_b

    @staticmethod
    def _attribute_identity_tags(rec: SignatureRecord) -> set[str]:
        return {
            tag.lower().strip()
            for tag in (rec.intent_tags or [])
            if tag
            and tag.lower().strip() not in _ATTRIBUTE_ROLE_TAGS
            and tag.lower().strip() not in _TEMPORAL_ATTRIBUTE_VALUES
            and tag.lower().strip() not in _STATUS_ATTRIBUTE_VALUES
        }

    def _trailing_attribute(self, text: str) -> str | None:
        tokens = self._tokenize_query(text)
        return tokens[-1] if tokens else None

    @staticmethod
    def _torsion_drawer_domain_key(rec: SignatureRecord) -> str:
        drawer = (rec.drawer_domain or "general").strip().lower() or "general"
        domain = (rec.domain or "insight").strip().lower() or "insight"
        return f"{drawer}|{domain}"

    @staticmethod
    def _torsion_cluster_key(rec: SignatureRecord) -> str:
        """Legacy single-key helper (explicit cluster_id or drawer|domain)."""
        meta = rec.metadata or {}
        cluster_id = meta.get("cluster_id")
        if cluster_id is not None and str(cluster_id).strip():
            return f"cluster:{str(cluster_id).strip()}"
        return TorsionSignalsMixin._torsion_drawer_domain_key(rec)

    @staticmethod
    def _humanize_torsion(
        a: SignatureRecord,
        b: SignatureRecord,
        *,
        kind: str,
        detail: str,
    ) -> str:
        """Plain-English conflict line (Morning Brief style, English-only)."""
        a_snip = TorsionSignalsMixin._fact_preview(a.compressed_fact)
        b_snip = TorsionSignalsMixin._fact_preview(b.compressed_fact)
        base = f"Conflict found between Signature A ({a_snip}) and Signature B ({b_snip})"
        match kind:
            case "deadline":
                return f"{base}: deadlines disagree ({detail})."
            case "factual":
                return f"{base}: conflicting numeric/factual claims ({detail})."
            case "attribute_clash":
                return f"{base}: potential entity-attribute clash ({detail})."
            case "entity_exclusion":
                return f"{base}: exclusive-slot occupancy clash ({detail})."
            case "polarity":
                return f"{base}: opposing polarity on the same topic ({detail})."
            case "pressure":
                return f"{base}: opposing pressure vectors ({detail})."
            case _:
                return f"{base}: reverse resonance on a shared topic."

    @staticmethod
    def _normalize_polarity_text(text: str) -> str:
        """Expand contractions / slang so negation tokens become detectable."""
        t = (text or "").lower()
        for src, dst in _CONTRACTION_MAP:
            t = t.replace(src, dst)
        # Common no-apostrophe typos after apostrophe expansion
        for src, dst in (
            (" dont ", " do not "),
            (" doesnt ", " does not "),
            (" didnt ", " did not "),
            (" isnt ", " is not "),
            (" arent ", " are not "),
            (" wasnt ", " was not "),
            (" werent ", " were not "),
            (" wont ", " will not "),
            (" cant ", " can not "),
        ):
            t = t.replace(src, dst)
        # Leading typo without spaces: "i dont love" already spaced by lower()
        if t.startswith("dont "):
            t = "do not " + t[5:]
        return t

    @staticmethod
    def _has_negation(text: str) -> bool:
        """True if text contains an English negation cue after normalization."""
        norm = TorsionSignalsMixin._normalize_polarity_text(text)
        tokens = set(WORD_PATTERN.findall(norm))
        # Also catch leftover no-apostrophe forms as whole tokens
        return bool(tokens & _NEGATION_TOKENS)

    @staticmethod
    def _standalone_numbers(text: str) -> set[str]:
        """Extract numeric tokens that are not glued to letters (skip Q3, v2)."""
        return set(NUMBER_PATTERN.findall(text))

    @staticmethod
    def _format_numbers(nums: set[str], limit: int = 3) -> str:
        ordered = sorted(nums, key=lambda n: (len(n), n))[:limit]
        return ", ".join(ordered)

    @staticmethod
    def _fact_preview(text: str | None, max_len: int = 72) -> str:
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return "empty"
        if len(cleaned) <= max_len:
            return cleaned
        return cleaned[: max_len - 1].rstrip() + "…"
