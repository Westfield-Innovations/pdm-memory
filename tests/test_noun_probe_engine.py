"""The Noun Probe engine: every phase, the detector gate, config and the pipeline."""

from __future__ import annotations

import dataclasses
import logging

import pytest

from pdm_memory.noun_probe import (
    Boundary,
    BoundaryError,
    EvidenceTag,
    NounClass,
    Perturbation,
    RelockedInsight,
    couple_verbs,
    detect_invariance,
    drift_veto,
    extract_structural_nouns,
    gated_probe,
    lock_boundary,
    noun_probe_config_from_settings,
    perturb,
    relock,
    run_noun_probe,
    should_probe,
)
from pdm_memory.noun_probe.pipeline import ProbePassError, audit, vision_pass


class TestBoundaryLock:
    def _valid_kwargs(self) -> dict:
        return {
            "system": "noun probe engine",
            "domain": "engineering",
            "scale": "single document",
            "not_analyzed": ["runtime perf", "ui"],
        }

    def test_valid_lock_returns_frozen_boundary(self):
        boundary = lock_boundary(**self._valid_kwargs())

        assert isinstance(boundary, Boundary)
        assert boundary.system == "noun probe engine"
        assert boundary.domain == "engineering"
        assert boundary.scale == "single document"
        assert boundary.not_analyzed == ("runtime perf", "ui")

    def test_axes_are_stripped(self):
        kwargs = self._valid_kwargs()
        kwargs["system"] = "  probe  "
        kwargs["not_analyzed"] = ["  ui  "]

        boundary = lock_boundary(**kwargs)

        assert boundary.system == "probe"
        assert boundary.not_analyzed == ("ui",)

    def test_blank_axis_aborts(self):
        for axis in ("system", "domain", "scale"):
            kwargs = self._valid_kwargs()
            kwargs[axis] = "   "
            with pytest.raises(BoundaryError) as ctx:
                lock_boundary(**kwargs)
            assert axis in str(ctx.value)

    def test_undeclared_not_analyzed_aborts(self):
        kwargs = self._valid_kwargs()
        kwargs["not_analyzed"] = []
        with pytest.raises(BoundaryError):
            lock_boundary(**kwargs)

    def test_whitespace_only_not_analyzed_aborts(self):
        kwargs = self._valid_kwargs()
        kwargs["not_analyzed"] = ["  ", ""]
        with pytest.raises(BoundaryError):
            lock_boundary(**kwargs)

    def test_bare_string_not_analyzed_rejected(self):
        kwargs = self._valid_kwargs()
        kwargs["not_analyzed"] = "ui"
        with pytest.raises(BoundaryError):
            lock_boundary(**kwargs)

    def test_not_analyzed_dedup_preserves_order(self):
        kwargs = self._valid_kwargs()
        kwargs["not_analyzed"] = ["ui", "perf", "ui", "perf", "i18n"]

        boundary = lock_boundary(**kwargs)

        assert boundary.not_analyzed == ("ui", "perf", "i18n")

    def test_boundary_is_immutable(self):
        boundary = lock_boundary(**self._valid_kwargs())
        with pytest.raises(dataclasses.FrozenInstanceError):
            boundary.system = "mutated"  # type: ignore[misc]


class TestStructuralNounExtraction:
    def setup_method(self):
        self.boundary = lock_boundary(
            system="noun probe",
            domain="engineering",
            scale="single document",
            not_analyzed=["ui"],
        )

    def _extract(self, text: str, **kwargs):
        return extract_structural_nouns(text, self.boundary, **kwargs)

    def test_requires_locked_boundary(self):
        with pytest.raises(BoundaryError):
            extract_structural_nouns("the signature degrades", None)  # type: ignore[arg-type]

    def test_empty_text_returns_empty(self):
        assert self._extract("   ") == ()

    def test_classifies_into_five_functional_classes(self):
        text = (
            "The signature degrades over time.\n\n"
            "The detector limits the failure and the budget decays."
        )
        nouns = self._extract(text)
        by_text = {n.text: n.noun_class for n in nouns}

        assert by_text.get("signature") == NounClass.STATE
        assert by_text.get("detector") == NounClass.ACTOR
        assert by_text.get("failure") == NounClass.FAILURE
        assert by_text.get("budget") == NounClass.RESOURCE

    def test_no_metaphor_class_every_noun_in_five(self):
        text = (
            "Radiation deposits charge.\n\n"
            "The resolver pins the constraint and the system observes drift."
        )
        nouns = self._extract(text)
        assert nouns
        allowed = set(NounClass)
        assert all(n.noun_class in allowed for n in nouns)

    def test_function_beats_raw_frequency(self):
        # 'thing' is frequent but role-less filler; 'constraint' is load-bearing.
        text = (
            "thing thing thing thing thing thing.\n\n"
            "The constraint limits the system. The constraint forces the actor."
        )
        nouns = self._extract(text)
        salience = {n.text: n.salience for n in nouns}
        assert "constraint" in salience
        if "thing" in salience:
            assert salience["constraint"] > salience["thing"]

    def test_top_n_limit(self):
        text = (
            "The signature degrades.\n\n"
            "The detector limits failure.\n\n"
            "The budget decays and the resolver shifts the constraint."
        )
        nouns = self._extract(text, top_n=2)
        assert len(nouns) == 2
        assert nouns[0].salience >= nouns[1].salience

    def test_invalid_top_n_aborts(self):
        with pytest.raises(ValueError):
            self._extract("the signature degrades", top_n=0)

    def test_spread_tracked_across_sections(self):
        text = "The constraint holds.\n\nThe constraint holds again."
        nouns = self._extract(text)
        constraint = next(n for n in nouns if n.text == "constraint")
        assert constraint.sections == (0, 1)
        assert constraint.occurrences == 2


class TestVerbCoupling:
    def setup_method(self):
        self.boundary = lock_boundary(
            system="noun probe",
            domain="engineering",
            scale="single document",
            not_analyzed=["ui"],
        )

    def _couple(self, text: str):
        nouns = extract_structural_nouns(text, self.boundary)
        return couple_verbs(nouns, text, self.boundary), {n.text: n for n in nouns}

    def test_requires_locked_boundary(self):
        with pytest.raises(BoundaryError):
            couple_verbs((), "the signature degrades", None)  # type: ignore[arg-type]

    def test_empty_nouns_returns_empty(self):
        result = couple_verbs((), "irrelevant text", self.boundary)
        assert result.couplings == ()
        assert result.structural_verbs == ()

    def test_primary_verb_active_subject(self):
        result, _ = self._couple("The signature degrades over time.")
        coupling = next(c for c in result.couplings if c.noun == "signature")
        assert coupling.primary_verb == "degrade"

    def test_passive_verb_object(self):
        result, _ = self._couple("Radiation deposits charge into the system.")
        charge = next(c for c in result.couplings if c.noun == "charge")
        assert charge.passive_verb == "deposit"

    def test_implied_verb_from_class(self):
        result, _ = self._couple("The signature degrades.")
        signature = next(c for c in result.couplings if c.noun == "signature")
        # STATE → persist (hidden assumption), regardless of observed verbs.
        assert signature.implied_verb == "persist"

    def test_structural_verb_couples_multiple_nouns(self):
        text = "The signature degrades.\n\nThe failure degrades and the budget decays."
        result, _ = self._couple(text)
        degrade = next(
            (sv for sv in result.structural_verbs if sv.verb == "degrade"), None
        )
        assert degrade is not None
        assert len(degrade.coupled_nouns) >= 2
        assert degrade.is_canonical

    def test_single_noun_verb_not_structural(self):
        result, _ = self._couple("The signature degrades over time.")
        verbs = {sv.verb for sv in result.structural_verbs}
        assert "degrade" not in verbs


class TestPerturbation:
    def setup_method(self):
        self.boundary = lock_boundary(
            system="noun probe",
            domain="engineering",
            scale="single document",
            not_analyzed=["ui"],
        )

    def _perturb(self, text: str, **kwargs):
        nouns = extract_structural_nouns(text, self.boundary)
        coupling = couple_verbs(nouns, text, self.boundary)
        return perturb(coupling, self.boundary, **kwargs)

    def _for(self, perturbations, noun: str):
        return next(p for p in perturbations if p.source_noun == noun)

    def test_requires_locked_boundary(self):
        coupling = couple_verbs((), "x", self.boundary)
        with pytest.raises(BoundaryError):
            perturb(coupling, None)  # type: ignore[arg-type]

    def test_invalid_substitutes_count_aborts(self):
        coupling = couple_verbs((), "x", self.boundary)
        with pytest.raises(ValueError):
            perturb(coupling, self.boundary, substitutes_per_noun=0)

    def test_class_verb_survives_as_invariant(self):
        # RESOURCE noun + RESOURCE-affinity verb → invariant survives.
        perts = self._perturb("The budget accumulates.")
        budget = self._for(perts, "budget")
        assert budget.survived
        assert budget.rule == "B-invariant"

    def test_substitute_stays_in_class(self):
        perts = self._perturb("The budget accumulates.")
        budget = self._for(perts, "budget")
        assert budget.substitute != "budget"
        assert budget.noun_class == NounClass.RESOURCE

    def test_foreign_class_verb_is_absurd(self):
        # STATE noun + RESOURCE verb → foreign-class job (smush signal).
        perts = self._perturb("The signature accumulates.")
        signature = self._for(perts, "signature")
        assert not signature.survived
        assert signature.rule == "C-absurd"
        assert "smush" in signature.note

    def test_idiosyncratic_verb_is_boundary_condition(self):
        # 'hide' is in no class affinity → boundary condition, not absurd.
        perts = self._perturb("The constraint hides the failure.")
        constraint = self._for(perts, "constraint")
        assert not constraint.survived
        assert constraint.rule == "B-boundary"

    def test_absurd_not_marked_survived(self):
        # Rule C: absurd structures are not kept (not survived) at Phase 3.
        perts = self._perturb("The signature accumulates.")
        absurd = [p for p in perts if p.rule == "C-absurd"]
        assert absurd
        assert all(not p.survived for p in absurd)


class _PipelineMixin:
    def setup_method(self):
        self.boundary = lock_boundary(
            system="noun probe",
            domain="engineering",
            scale="single document",
            not_analyzed=["ui"],
        )

    def _relock(self, text: str):
        nouns = extract_structural_nouns(text, self.boundary)
        coupling = couple_verbs(nouns, text, self.boundary)
        perts = perturb(coupling, self.boundary)
        return coupling, relock(perts, self.boundary)


class TestConstraintRelock(_PipelineMixin):
    def _tag_for(self, text: str, noun: str) -> EvidenceTag:
        _, relocked = self._relock(text)
        insight = next(r for r in relocked if r.perturbation.source_noun == noun)
        return insight.evidence

    def test_requires_locked_boundary(self):
        with pytest.raises(BoundaryError):
            relock((), None)  # type: ignore[arg-type]

    def test_invariant_tagged_known_and_kept(self):
        _, relocked = self._relock("The budget accumulates.")
        budget = next(r for r in relocked if r.perturbation.source_noun == "budget")
        assert budget.evidence == EvidenceTag.KNOWN
        assert budget.kept

    def test_boundary_condition_tagged_hypothesis_and_kept(self):
        _, relocked = self._relock("The constraint hides the failure.")
        constraint = next(
            r for r in relocked if r.perturbation.source_noun == "constraint"
        )
        assert constraint.evidence == EvidenceTag.HYPOTHESIS
        assert constraint.kept

    def test_absurd_tagged_metaphor_and_dropped(self):
        _, relocked = self._relock("The signature accumulates.")
        signature = next(
            r for r in relocked if r.perturbation.source_noun == "signature"
        )
        assert signature.evidence == EvidenceTag.METAPHOR
        assert not signature.kept


class TestInvarianceDetection(_PipelineMixin):
    def _detect(self, text: str):
        coupling, relocked = self._relock(text)
        return detect_invariance(coupling, relocked, self.boundary)

    def test_requires_locked_boundary(self):
        coupling, relocked = self._relock("The budget accumulates.")
        with pytest.raises(BoundaryError):
            detect_invariance(coupling, relocked, None)  # type: ignore[arg-type]

    def test_structural_verb_yields_invariant(self):
        text = "The failure degrades.\n\nThe signature degrades."
        signals = self._detect(text)
        invariants = [s for s in signals if s.kind == "invariant"]
        assert any(s.subject == "degrade" for s in invariants)

    def test_absurd_yields_constraint_relocation(self):
        signals = self._detect("The signature accumulates.")
        relocations = [s for s in signals if s.kind == "constraint_relocation"]
        assert any(s.subject == "signature" for s in relocations)

    def test_boundary_condition_yields_inconsistency(self):
        signals = self._detect("The constraint hides the failure.")
        inconsistencies = [s for s in signals if s.kind == "boundary_inconsistency"]
        assert any(s.subject == "constraint" for s in inconsistencies)

    def test_actor_acted_upon_is_misframed(self):
        signals = self._detect("The detector is deployed by the team.")
        misframed = [s for s in signals if s.kind == "misframed"]
        assert any(s.subject == "detector" for s in misframed)


class TestDriftVeto(_PipelineMixin):
    def _insight(self, **overrides) -> RelockedInsight:
        pert_kwargs = {
            "source_noun": "budget",
            "noun_class": NounClass.RESOURCE,
            "verb": "accumulate",
            "substitute": "capital",
            "survived": True,
            "rule": "B-invariant",
            "note": "resource nouns share accumulate",
        }
        insight_kwargs = {
            "evidence": EvidenceTag.KNOWN,
            "kept": True,
            "reason": "boundary intact + class-consistent",
        }
        for key in list(overrides):
            if key in pert_kwargs:
                pert_kwargs[key] = overrides.pop(key)
        insight_kwargs.update(overrides)
        return RelockedInsight(
            perturbation=Perturbation(**pert_kwargs), **insight_kwargs
        )

    def test_requires_locked_boundary(self):
        with pytest.raises(BoundaryError):
            drift_veto((), None)  # type: ignore[arg-type]

    def test_clean_pipeline_vetoes_nothing(self):
        _, relocked = self._relock("The budget accumulates.")
        result = drift_veto(relocked, self.boundary)
        assert result.vetoed == ()
        assert result.kept

    def test_clean_insight_kept(self):
        result = drift_veto([self._insight()], self.boundary)
        assert len(result.kept) == 1
        assert result.vetoed == ()

    def test_dropped_insight_is_not_a_veto_event(self):
        # kept=False insights are already excluded — not logged as vetoed.
        insight = self._insight(kept=False, evidence=EvidenceTag.METAPHOR)
        result = drift_veto([insight], self.boundary)
        assert result.kept == ()
        assert result.vetoed == ()

    def test_cross_class_substitution_vetoed(self):
        # RESOURCE noun but substitute is an ACTOR word → Rule A breach.
        insight = self._insight(substitute="detector")
        result = drift_veto([insight], self.boundary)
        assert result.kept == ()
        assert result.vetoed[0].reason == "cross-class substitution"

    def test_kept_absurd_as_truth_vetoed(self):
        insight = self._insight(
            rule="C-absurd", survived=False, evidence=EvidenceTag.METAPHOR, kept=True
        )
        result = drift_veto([insight], self.boundary)
        assert result.vetoed[0].reason == "kept absurd-as-truth"

    def test_invented_quantitative_claim_vetoed(self):
        insight = self._insight(reason="confidence is 87% structural truth")
        result = drift_veto([insight], self.boundary)
        assert result.vetoed[0].reason == "invented quantitative claim"

    def test_a_noun_on_a_reference_that_drifted_a_tooth_is_vetoed(self):
        result = drift_veto(
            [self._insight()], self.boundary, reference_drift={"budget": 1.5}
        )
        assert result.kept == ()
        [record] = result.vetoed
        assert (record.subject, record.reason, record.kind) == (
            "budget",
            "reference_drift",
            "reference_drift",
        )
        assert record.displacement_teeth == 1.5

    def test_the_direction_of_the_drift_does_not_matter(self):
        result = drift_veto(
            [self._insight()], self.boundary, reference_drift={"budget": -2.0}
        )
        assert result.vetoed[0].displacement_teeth == 2.0

    def test_a_drift_under_a_tooth_vetoes_nothing(self):
        result = drift_veto(
            [self._insight()], self.boundary, reference_drift={"budget": 0.9}
        )
        assert result.vetoed == ()
        assert len(result.kept) == 1

    def test_a_drift_of_another_noun_vetoes_nothing(self):
        result = drift_veto(
            [self._insight()], self.boundary, reference_drift={"ledger": 5.0}
        )
        assert result.vetoed == ()

    def test_a_breach_of_discipline_keeps_its_own_reason(self):
        result = drift_veto(
            [self._insight(substitute="detector")],
            self.boundary,
            reference_drift={"budget": 5.0},
        )
        assert result.vetoed[0].reason == "cross-class substitution"
        assert result.vetoed[0].kind == "discipline"

    def test_without_the_argument_nothing_changes(self):
        result = drift_veto([self._insight()], self.boundary)
        assert result.vetoed == ()
        assert (
            drift_veto([self._insight(substitute="detector")], self.boundary)
            .vetoed[0]
            .displacement_teeth
        ) is None


class TestDetectorGate:
    def test_casual_prompt_does_not_fire(self):
        decision = should_probe("what's the weather like today?")
        assert not decision.should_run

    def test_patent_doc_fires(self):
        decision = should_probe(
            "This patent claim describes an embodiment of the invention."
        )
        assert decision.should_run
        assert "patent" in decision.categories

    def test_spec_with_rfc_markers_fires(self):
        decision = should_probe(
            "The interface specification: the system shall validate and must reject."
        )
        assert decision.should_run
        assert "spec" in decision.categories

    def test_single_casual_mention_does_not_fire(self):
        # One financial keyword, short, single section → below threshold.
        decision = should_probe("Please review the contract.")
        assert not decision.should_run

    def test_force_overrides(self):
        decision = should_probe("hello there", force=True)
        assert decision.should_run
        assert "forced" in decision.reasons

    def test_committed_document_fires_on_single_hit(self):
        body = "policy " + "word " * 60
        decision = should_probe(body)
        assert decision.should_run
        assert "governance" in decision.categories

    def test_invalid_thresholds_abort(self):
        with pytest.raises(ValueError):
            should_probe("x", min_hits=0)


class TestRunNounProbe:
    def setup_method(self):
        self.boundary = lock_boundary(
            system="noun probe",
            domain="engineering",
            scale="single document",
            not_analyzed=["ui"],
        )
        # Crafted to yield all four finding kinds:
        #  - budget RESOURCE+accumulate → broaden
        #  - signature STATE+accumulate (foreign) → split
        #  - accumulate couples budget+signature → invariant
        #  - detector ACTOR only acted upon → misframed
        self.text = (
            "The budget accumulates.\n\n"
            "The signature accumulates.\n\n"
            "The failure degrades.\n\n"
            "The detector is deployed by the team."
        )

    def test_requires_locked_boundary(self):
        with pytest.raises(BoundaryError):
            run_noun_probe("the budget accumulates", None)  # type: ignore[arg-type]

    def test_empty_text_no_findings(self):
        report = run_noun_probe("   ", self.boundary)
        assert report.findings == ()
        assert report.noun_count == 0

    def test_report_kinds_present(self):
        report = run_noun_probe(self.text, self.boundary)
        kinds = {f.kind for f in report.findings}
        assert "broaden" in kinds
        assert "split" in kinds
        assert "invariant" in kinds
        assert "misframed" in kinds

    def test_split_targets_smush_noun(self):
        report = run_noun_probe(self.text, self.boundary)
        split = [f for f in report.findings if f.kind == "split"]
        assert any(f.term == "signature" for f in split)

    def test_findings_sorted_by_kind_then_term(self):
        report = run_noun_probe(self.text, self.boundary)
        order = [(_kind_rank(f.kind), f.term) for f in report.findings]
        assert order == sorted(order)

    def test_findings_deduplicated(self):
        report = run_noun_probe(self.text, self.boundary)
        keys = [(f.kind, f.term) for f in report.findings]
        assert len(keys) == len(set(keys))

    def test_to_dict_shape(self):
        report = run_noun_probe(self.text, self.boundary)
        data = report.to_dict()
        assert data["boundary"]["domain"] == "engineering"
        assert isinstance(data["findings"], list)
        assert "noun_count" in data


def _kind_rank(kind: str) -> int:
    return {"split": 0, "broaden": 1, "invariant": 2, "misframed": 3}.get(kind, 99)


class TestNounProbeConfig:
    def test_defaults_load(self):
        cfg = noun_probe_config_from_settings()
        assert cfg.enabled
        assert cfg.top_n == 12
        assert cfg.substitutes_per_noun == 1

    def test_non_positive_top_n_aborts(self, monkeypatch):
        monkeypatch.setenv("PDM_NOUN_PROBE_TOP_N", "0")
        with pytest.raises(ValueError):
            noun_probe_config_from_settings()

    def test_negative_min_hits_aborts(self, monkeypatch):
        monkeypatch.setenv("PDM_NOUN_PROBE_MIN_HITS", "-1")
        with pytest.raises(ValueError):
            noun_probe_config_from_settings()


class TestGatedProbe:
    def setup_method(self):
        self.boundary = lock_boundary(
            system="noun probe",
            domain="engineering",
            scale="single document",
            not_analyzed=["ui"],
        )

    def test_disabled_returns_none(self, monkeypatch):
        monkeypatch.setenv("PDM_NOUN_PROBE_ENABLED", "false")
        result = gated_probe("The patent claim is broad.", self.boundary)
        assert result is None

    def test_casual_input_gated_out(self):
        result = gated_probe("what's the weather?", self.boundary)
        assert result is None

    def test_high_stakes_runs_and_logs(self, caplog):
        text = "The patent claim describes an embodiment of the invention."
        with caplog.at_level(logging.INFO, logger="pdm_memory.noun_probe"):
            result = gated_probe(text, self.boundary)
        assert result is not None
        events = [getattr(r, "event", "") for r in caplog.records]
        assert "NOUN_PROBE_RUN" in events

    def test_force_runs_on_casual(self):
        result = gated_probe("budget accumulates", self.boundary, force=True)
        assert result is not None


def test_an_audit_inside_a_vision_pass_is_refused():
    with vision_pass(), pytest.raises(ProbePassError):
        audit("The budget accumulates.", system="s", domain="d", scale="claim")
