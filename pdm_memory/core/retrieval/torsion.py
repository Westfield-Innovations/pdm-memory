# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Reverse Resonance / torsion detection for PDM signatures."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pdm_memory.types import TorsionJudge

from pdm_memory.core.constraints import (
    collect_occupants,
    detect_constraint_violation,
    entity_exclusion_pair,
    parse_exclusive_slot,
    parse_presence,
)
from pdm_memory.core.retrieval.torsion_signals import TorsionSignalsMixin
from pdm_memory.core.signature import SignatureRecord
from pdm_memory.models import TorsionReport

logger = logging.getLogger(__name__)

_SMALL_CLUSTER: int = 48
_AUTO_CLUSTER_RESONANCE: float = 0.85
_INTEGRITY_DRAWERS: frozenset[str] = frozenset(
    {"anchors", "foundational", "goals", "mission", "principles", "stewardship"}
)
_INTEGRITY_TAGS: frozenset[str] = frozenset(
    {"anchor", "foundational", "goal", "policy", "principle", "rule", "stewardship"}
)


class TorsionMixin(TorsionSignalsMixin):
    """Reverse Resonance detection mixed into RetrievalEngine."""

    """Reverse Resonance detection mixed into RetrievalEngine."""

    def detect_torsion(
        self,
        records: Sequence[SignatureRecord],
        threshold: float = 0.7,
        judge: TorsionJudge | None = None,
    ) -> list[TorsionReport]:
        """
        Find Reverse Resonance pairs: high topic similarity + opposing facts/pressure.

        Clustering (in order):
          1. Explicit ``metadata['cluster_id']`` buckets.
          2. Fallback ``drawer|domain`` buckets for records without cluster_id.
          3. Auto-Discovery: records lacking ``cluster_id`` with
             ``topic_similarity > 0.85`` are unioned into temporary virtual
             clusters (so related sensors/facts compare even across drawers).
          4. Shared-location spatial clusters (e.g. Server Room presence).
          5. Exclusive-slot Entity Exclusion for capacity-1 places.

        Within a bucket, candidates come from a tag inverted index (shared intent
        tags). Small buckets may also compare all pairs. Integrity anchors use a
        deliberate anchors × signals pass across drawers; ordinary records never
        run blind global N².

        Optional ``judge`` callback may flag pairs rules-only detection missed.
        """
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError("threshold must be in [0.0, 1.0]")
        if len(records) < 2:
            return []

        by_id: dict[str, SignatureRecord] = {r.id: r for r in records if r.id}
        clusters = self._build_torsion_clusters(list(by_id.values()))

        reports: list[TorsionReport] = []
        seen_pairs: set[tuple[str, str]] = set()
        reported_pairs: set[tuple[str, str]] = set()

        # Rules are asymmetric: every stewardship/foundational anchor must be
        # checked against signals from every other drawer.
        for rule, signal in self._integrity_candidate_pairs(list(by_id.values())):
            pair_key = (rule.id, signal.id) if rule.id < signal.id else (signal.id, rule.id)
            report = self._score_integrity_violation(
                rule,
                signal,
                occupancy_records=list(by_id.values()),
            )
            if report is None:
                continue
            seen_pairs.add(pair_key)
            if report.torsion_score >= threshold:
                reports.append(report)
                reported_pairs.add(pair_key)

        # Exclusive spatial slots: multiple occupants of a capacity-1 place are
        # Entity Exclusion even when cluster_id was never supplied.
        for report in self._entity_exclusion_reports(list(by_id.values())):
            pair_key = (
                (report.signature_a_id, report.signature_b_id)
                if report.signature_a_id < report.signature_b_id
                else (report.signature_b_id, report.signature_a_id)
            )
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            if report.torsion_score >= threshold:
                reports.append(report)
                reported_pairs.add(pair_key)

        for cluster_key, group in clusters.items():
            if len(group) < 2:
                continue
            for a, b in self._torsion_candidate_pairs(group):
                pair_key = (a.id, b.id) if a.id < b.id else (b.id, a.id)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                report = self._score_torsion_pair(a, b, cluster_key=cluster_key)
                if report is not None and report.torsion_score >= threshold:
                    reports.append(report)
                    reported_pairs.add(pair_key)

        if judge is not None:
            reports = self._merge_judge_reports(clusters, reports, threshold, judge, reported_pairs)

        reports.sort(key=lambda r: r.torsion_score, reverse=True)
        return reports

    def _integrity_candidate_pairs(
        self,
        records: Sequence[SignatureRecord],
    ) -> Iterator[tuple[SignatureRecord, SignatureRecord]]:
        anchors = [record for record in records if self._is_integrity_anchor(record)]
        signals = [record for record in records if not self._is_integrity_anchor(record)]
        for anchor in anchors:
            for signal in signals:
                yield anchor, signal

    @staticmethod
    def _is_integrity_anchor(record: SignatureRecord) -> bool:
        metadata = record.metadata or {}
        if metadata.get("is_anchor") or metadata.get("role") in {
            "anchor",
            "goal",
            "stewardship",
        }:
            return True
        drawer = (record.drawer_domain or "").strip().lower()
        tags = {tag.lower() for tag in (record.intent_tags or []) if tag}
        return drawer in _INTEGRITY_DRAWERS or bool(tags & _INTEGRITY_TAGS)

    def _score_integrity_violation(
        self,
        rule: SignatureRecord,
        signal: SignatureRecord,
        *,
        occupancy_records: Sequence[SignatureRecord] | None = None,
    ) -> TorsionReport | None:
        violation = detect_constraint_violation(
            rule,
            signal.compressed_fact or "",
            candidate_tags=signal.intent_tags or [],
            occupancy_records=occupancy_records or (),
        )
        if violation is None:
            return None

        rule_text = (rule.compressed_fact or "")[:500]
        signal_text = (signal.compressed_fact or "")[:500]
        explanation = (
            f"Integrity Violation: {violation.explanation} "
            f"Rule: '{self._fact_preview(rule_text)}'. "
            f"Signal: '{self._fact_preview(signal_text)}'."
        )
        kind = "integrity_violation" if violation.kind != "entity_exclusion" else "entity_exclusion"
        return TorsionReport(
            signature_a_id=rule.id,
            signature_b_id=signal.id,
            signature_a_text=rule_text,
            signature_b_text=signal_text,
            drawer=rule.drawer_domain or "stewardship",
            domain=rule.domain or signal.domain or "structural",
            torsion_score=round(violation.strength, 4),
            topic_similarity=round(violation.topic_similarity, 4),
            contradiction_strength=round(violation.strength, 4),
            explanation=explanation,
            conflict_kind=kind,
            cluster_key=f"integrity:{rule.id[:8]}",
        )

    def _entity_exclusion_reports(
        self,
        records: Sequence[SignatureRecord],
    ) -> list[TorsionReport]:
        """Pairwise Entity Exclusion for exclusive spatial slots."""
        reports: list[TorsionReport] = []
        by_id = {record.id: record for record in records if record.id}
        for rule in records:
            slot = parse_exclusive_slot(rule.compressed_fact or "")
            if slot is None:
                continue
            occupants = collect_occupants(records, location=slot.location)
            if len(occupants) <= slot.capacity:
                continue
            for i, left in enumerate(occupants):
                for right in occupants[i + 1 :]:
                    violation = entity_exclusion_pair(left, right, slot=slot)
                    if violation is None:
                        continue
                    left_rec = by_id.get(left.source_id or "")
                    right_rec = by_id.get(right.source_id or "")
                    if left_rec is None or right_rec is None:
                        continue
                    reports.append(
                        TorsionReport(
                            signature_a_id=left_rec.id,
                            signature_b_id=right_rec.id,
                            signature_a_text=(left_rec.compressed_fact or "")[:500],
                            signature_b_text=(right_rec.compressed_fact or "")[:500],
                            drawer=left_rec.drawer_domain or right_rec.drawer_domain or "general",
                            domain=left_rec.domain or right_rec.domain or "insight",
                            torsion_score=1.0,
                            topic_similarity=1.0,
                            contradiction_strength=1.0,
                            explanation=(
                                f"{violation.explanation} "
                                f"Rule: '{self._fact_preview(rule.compressed_fact)}'."
                            ),
                            conflict_kind="entity_exclusion",
                            cluster_key=f"slot:{slot.location}",
                        )
                    )
        return reports

    def _build_torsion_clusters(
        self,
        records: Sequence[SignatureRecord],
    ) -> dict[str, list[SignatureRecord]]:
        """
        Build torsion comparison buckets.

        Explicit ``cluster_id`` wins. Unclustered records get ``drawer|domain``
        buckets PLUS auto-discovered virtual clusters when resonance > 0.85,
        PLUS shared-location spatial clusters (Server Room occupancy, etc.).
        """
        clusters: dict[str, list[SignatureRecord]] = {}
        unclustered: list[SignatureRecord] = []

        for rec in records:
            meta = rec.metadata or {}
            cluster_id = meta.get("cluster_id")
            if cluster_id is not None and str(cluster_id).strip():
                key = f"cluster:{str(cluster_id).strip()}"
                clusters.setdefault(key, []).append(rec)
            else:
                unclustered.append(rec)
                # Keep legacy coarse bucket so mid-resonance same-drawer pairs still compare
                coarse = self._torsion_drawer_domain_key(rec)
                clusters.setdefault(coarse, []).append(rec)

        for key, group in self._auto_discover_resonance_clusters(unclustered).items():
            clusters[key] = group

        for key, group in self._auto_discover_location_clusters(unclustered).items():
            clusters[key] = group

        return clusters

    def _auto_discover_location_clusters(
        self,
        records: Sequence[SignatureRecord],
    ) -> dict[str, list[SignatureRecord]]:
        """Group presence facts that share the same place (no cluster_id needed)."""
        by_location: dict[str, list[SignatureRecord]] = {}
        for record in records:
            presence = parse_presence(record.compressed_fact or "", source_id=record.id)
            if presence is None:
                continue
            by_location.setdefault(presence.location, []).append(record)
        return {
            f"slot:{location}": members
            for location, members in by_location.items()
            if len(members) >= 2
        }

    def _auto_discover_resonance_clusters(
        self,
        records: Sequence[SignatureRecord],
        *,
        min_resonance: float = _AUTO_CLUSTER_RESONANCE,
    ) -> dict[str, list[SignatureRecord]]:
        """
        Union-Find virtual clusters for records with topic_similarity > min_resonance.

        Candidate edges come from the same surgical tag/token index used for
        torsion pairs — never blind global N².
        """
        if len(records) < 2:
            return {}

        by_id = {r.id: r for r in records if r.id}
        parent: dict[str, str] = {rid: rid for rid in by_id}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for a, b in self._torsion_candidate_pairs(list(by_id.values())):
            if self._topic_similarity(a, b) > min_resonance:
                union(a.id, b.id)

        groups: dict[str, list[SignatureRecord]] = {}
        for rid, rec in by_id.items():
            groups.setdefault(find(rid), []).append(rec)

        out: dict[str, list[SignatureRecord]] = {}
        for idx, (root, members) in enumerate(groups.items()):
            if len(members) < 2:
                continue
            out[f"auto:{idx}:{root[:8]}"] = members
        return out

    def _merge_judge_reports(
        self,
        clusters: dict[str, list[SignatureRecord]],
        reports: list[TorsionReport],
        threshold: float,
        judge: TorsionJudge,
        reported_pairs: set[tuple[str, str]],
    ) -> list[TorsionReport]:
        """Append judge-flagged pairs not already reported by rules-only detection."""
        merged = list(reports)
        seen_pairs: set[tuple[str, str]] = set()
        for group in clusters.values():
            if len(group) < 2:
                continue
            for a, b in self._torsion_candidate_pairs(group):
                pair_key = (a.id, b.id) if a.id < b.id else (b.id, a.id)
                if pair_key in seen_pairs or pair_key in reported_pairs:
                    continue
                seen_pairs.add(pair_key)
                try:
                    judged = judge(a, b)
                except Exception as exc:
                    logger.warning("[PDM] torsion_judge failed for pair: %s", exc)
                    continue
                if judged is None or judged.torsion_score < threshold:
                    continue
                reported_pairs.add(pair_key)
                merged.append(judged)
        merged.sort(key=lambda r: r.torsion_score, reverse=True)
        return merged

    def _torsion_candidate_pairs(
        self,
        group: Sequence[SignatureRecord],
    ) -> Iterable[tuple[SignatureRecord, SignatureRecord]]:
        """Yield unordered unique pairs without full N² when the cluster is large."""
        by_id = {r.id: r for r in group}
        yielded: set[tuple[str, str]] = set()

        def emit(id_a: str, id_b: str) -> Iterable[tuple[SignatureRecord, SignatureRecord]]:
            if id_a == id_b:
                return
            key = (id_a, id_b) if id_a < id_b else (id_b, id_a)
            if key in yielded:
                return
            yielded.add(key)
            yield (by_id[key[0]], by_id[key[1]])

        tag_index: dict[str, list[str]] = {}
        for rec in group:
            for tag in {t.lower() for t in (rec.intent_tags or []) if t}:
                tag_index.setdefault(tag, []).append(rec.id)

        for ids in tag_index.values():
            if len(ids) < 2:
                continue
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    yield from emit(ids[i], ids[j])

        # Small clusters: also compare pairs with token overlap (no shared tags yet)
        if len(group) <= _SMALL_CLUSTER:
            token_cache = {r.id: set(self._tokenize_query(r.compressed_fact or "")) for r in group}
            ids = [r.id for r in group]
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    ta, tb = token_cache[ids[i]], token_cache[ids[j]]
                    if not ta or not tb:
                        continue
                    overlap = len(ta & tb) / max(len(ta), len(tb))
                    if overlap >= 0.25:
                        yield from emit(ids[i], ids[j])

