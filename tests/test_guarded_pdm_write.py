"""The guarded operation enters the real PDM save path exactly once."""

from pdm_memory.examples.guarded_pdm_write import run_example


def test_owner_evidence_controls_real_memory_write_and_recovery(tmp_path):
    result = run_example(tmp_path)
    assert result == {
        "before_review": "HELD",
        "after_review": "RETURNED",
        "unreviewed_text": "REJECTED",
        "after_recovery": "RETURNED",
        "restored_state": {"step": "review-note"},
        "memory_count": 1,
        "dispatch_count": 1,
    }
