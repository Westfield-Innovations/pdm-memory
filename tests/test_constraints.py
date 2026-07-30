# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Hard numerical and actor-scoped Goal Anchor tests."""

from __future__ import annotations

from pdm_memory import Memory
from pdm_memory.core.constraints import (
    detect_constraint_violation,
    entity_exclusion_pair,
    parse_exclusive_slot,
    parse_presence,
)
from pdm_memory.core.signature import SignatureRecord


def _rule(text: str, tags: list[str]) -> SignatureRecord:
    return SignatureRecord(
        compressed_fact=text,
        intent_tags=tags,
        drawer_domain="stewardship",
        domain="structural",
        p_magnitude=100.0,
        metadata={"is_anchor": True, "role": "goal"},
    )


class TestConstraintDetector:
    def test_magnitude_clash_requires_same_topic(self) -> None:
        rule = _rule(
            "GPU budget cap is $500 per week",
            ["goal", "gpu", "budget", "cap"],
        )

        violation = detect_constraint_violation(
            rule,
            "Authorize $1500 for GPU scaling",
        )
        unrelated = detect_constraint_violation(
            rule,
            "Hire 1500 customer support agents",
        )

        assert violation is not None
        assert violation.kind == "magnitude_clash"
        assert "GPU budget cap of $500" in violation.explanation
        assert "$1,500" in violation.explanation
        assert unrelated is None

    def test_value_at_or_below_cap_is_not_violation(self) -> None:
        rule = _rule(
            "GPU budget cap is $500 per week",
            ["goal", "gpu", "budget", "cap"],
        )
        assert detect_constraint_violation(rule, "Authorize $500 for GPU scaling") is None

    def test_role_violation_requires_different_actor_and_scope(self) -> None:
        rule = _rule(
            "Only Roman authorizes migrations",
            ["goal", "migration", "authorization"],
        )

        violation = detect_constraint_violation(
            rule,
            "Execute migration as junior_bot",
        )
        allowed = detect_constraint_violation(
            rule,
            "Execute migration as Roman",
        )

        assert violation is not None
        assert violation.kind == "role_violation"
        assert "'junior_bot'" in violation.explanation
        assert "requires 'Roman'" in violation.explanation
        assert allowed is None

    def test_engineering_unit_magnitude_clash_preserves_unit(self) -> None:
        rule = _rule(
            "Power cap is 200 kW",
            ["goal", "power", "grid", "cap"],
        )

        violation = detect_constraint_violation(
            rule,
            "Set power draw to 300 kW",
        )

        assert violation is not None
        assert violation.kind == "magnitude_clash"
        assert "power cap of 200 kW" in violation.explanation
        assert "requested 300 kW" in violation.explanation

    def test_non_multiplier_units_are_not_scaled(self) -> None:
        """'ms' must not be parsed as mega; 'kg' must not be kilo-of-grams."""
        latency = _rule(
            "API latency ceiling is 120 ms",
            ["goal", "latency", "api", "ceiling"],
        )
        mass = _rule(
            "Pallet mass maximum is 750 kg",
            ["goal", "pallet", "mass", "maximum"],
        )

        latency_hit = detect_constraint_violation(
            latency,
            "Raise latency target to 250 ms",
        )
        mass_hit = detect_constraint_violation(
            mass,
            "Approve pallet load of 900 kg",
        )

        assert latency_hit is not None
        assert "120 ms" in latency_hit.explanation
        assert "250 ms" in latency_hit.explanation
        assert mass_hit is not None
        assert "750 kg" in mass_hit.explanation
        assert "900 kg" in mass_hit.explanation

    def test_parenthetical_actor_and_override_wording(self) -> None:
        rule = _rule(
            "Only Safety Chief (Vitalii) authorizes bypass",
            ["goal", "safety", "bypass", "authorization"],
        )

        violation = detect_constraint_violation(
            rule,
            "Override request from shift_supervisor_bot",
            candidate_tags=["safety", "bypass", "override"],
        )

        assert violation is not None
        assert violation.kind == "role_violation"
        assert "bypass" in violation.explanation
        assert "'shift_supervisor_bot'" in violation.explanation
        assert "requires 'Vitalii'" in violation.explanation

    def test_only_one_person_is_not_role_actor(self) -> None:
        rule = _rule(
            "Only one person allowed in the Server Room at a time",
            ["goal", "server", "room", "occupancy"],
        )
        slot = parse_exclusive_slot(rule.compressed_fact or "")
        assert slot is not None
        assert slot.capacity == 1
        assert slot.location == "server room"
        assert detect_constraint_violation(rule, "Deploy as one") is None

    def test_entity_exclusion_blocks_third_admission(self) -> None:
        rule = _rule(
            "Only one person allowed in the Server Room at a time",
            ["goal", "server", "room", "occupancy"],
        )
        roman = SignatureRecord(
            id="roman",
            compressed_fact="Roman is in the Server Room",
            intent_tags=["server", "room", "presence"],
            drawer_domain="facilities",
            p_magnitude=80.0,
        )
        demian = SignatureRecord(
            id="demian",
            compressed_fact="Demian is in the Server Room",
            intent_tags=["server", "room", "presence"],
            drawer_domain="facilities",
            p_magnitude=80.0,
        )
        slot = parse_exclusive_slot(rule.compressed_fact or "")
        assert slot is not None
        left = parse_presence(roman.compressed_fact or "", source_id=roman.id)
        right = parse_presence(demian.compressed_fact or "", source_id=demian.id)
        assert left is not None and right is not None

        violation = detect_constraint_violation(
            rule,
            "Let Vitalii into the Server Room",
            occupancy_records=[roman, demian],
        )
        pair = entity_exclusion_pair(left, right, slot=slot)

        assert violation is not None
        assert violation.kind == "entity_exclusion"
        assert "Vitalii" in violation.explanation
        assert "Server Room" in violation.explanation
        assert pair is not None
        assert pair.kind == "entity_exclusion"


class TestBudgetHierarchyIntegration:
    def test_gaa_and_cross_drawer_integrity_violations(self, tmp_path) -> None:
        with Memory(store=str(tmp_path / "budget.db"), user="budget-test") as memory:
            memory.save(
                "GPU budget cap is $500 per week",
                tags=["goal", "gpu", "budget", "cap", "stewardship"],
                drawer="stewardship",
                p_magnitude=100.0,
                metadata={"is_anchor": True, "role": "goal"},
            )
            memory.save(
                "Only Roman authorizes migrations",
                tags=["goal", "migration", "authorization", "stewardship"],
                drawer="stewardship",
                p_magnitude=100.0,
                metadata={"is_anchor": True, "role": "goal"},
            )
            memory.save(
                "Marketing wants to spend $2000 on GPU campaigns",
                tags=["gpu", "budget", "marketing", "spend"],
                drawer="marketing",
                p_magnitude=80.0,
            )
            memory.save(
                "junior_bot is trying to migrate servers",
                tags=["migration", "servers", "automation"],
                drawer="operations",
                p_magnitude=80.0,
            )

            budget = memory.verify_alignment("Authorize $1500 for GPU scaling")
            role = memory.verify_alignment("Execute migration as junior_bot")
            torsion = memory.detect_torsion()

        assert budget.status == "TORSION"
        assert budget.torsion == 1.0
        assert "GPU budget cap of $500" in budget.explanation
        assert "GPU budget cap is $500 per week" in budget.explanation

        assert role.status == "TORSION"
        assert role.torsion == 1.0
        assert "junior_bot" in role.explanation
        assert "Only Roman authorizes migrations" in role.explanation

        integrity = [report for report in torsion if report.conflict_kind == "integrity_violation"]
        assert len(integrity) == 2
        assert all("Integrity Violation" in report.explanation for report in integrity)
        assert any("$2,000" in report.explanation for report in integrity)
        assert any(
            "assigns migration to 'junior_bot'" in report.explanation for report in integrity
        )


class TestSpatialExclusionIntegration:
    def test_server_room_entity_exclusion(self, tmp_path) -> None:
        with Memory(store=str(tmp_path / "spatial.db"), user="spatial-test") as memory:
            memory.save(
                "Only one person allowed in the Server Room at a time",
                tags=["goal", "server", "room", "occupancy", "stewardship"],
                drawer="stewardship",
                p_magnitude=100.0,
                metadata={"is_anchor": True, "role": "goal"},
            )
            memory.save(
                "Roman is in the Server Room",
                tags=["server", "room", "presence", "roman"],
                drawer="facilities",
                p_magnitude=80.0,
            )
            memory.save(
                "Demian is in the Server Room",
                tags=["server", "room", "presence", "demian"],
                drawer="facilities",
                p_magnitude=80.0,
            )

            alignment = memory.verify_alignment("Let Vitalii into the Server Room")
            torsion = memory.detect_torsion()

        assert alignment.status == "TORSION"
        assert alignment.torsion == 1.0
        assert "Server Room" in alignment.explanation
        assert "Vitalii" in alignment.explanation

        exclusions = [report for report in torsion if report.conflict_kind == "entity_exclusion"]
        assert exclusions
        assert any(report.cluster_key == "slot:server room" for report in exclusions)
        assert any(
            "Roman" in report.explanation and "Demian" in report.explanation
            for report in exclusions
        )
