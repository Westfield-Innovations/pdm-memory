"""Consequence and learning: expectations retained before action, graded later.

An expectation is committed before the action it forecasts; observations
arrive on their own; evaluation compares them under the owner's declared
rules; learning leaves only as a proposal for the owner's update path.
Nothing here writes to a PDM store.
"""

from pdm_memory.consequence.compare import NOT_OBSERVED
from pdm_memory.consequence.ledger import ConsequenceLedger
from pdm_memory.consequence.records import (
    PROTECTED_TARGETS,
    Account,
    Admission,
    Applicability,
    Application,
    Cause,
    CauseHypothesis,
    Comparison,
    Criterion,
    Evaluation,
    EvidenceKind,
    Expectation,
    Finding,
    Horizon,
    NotSupported,
    Observation,
    Ordering,
    OwnerPolicy,
    QuantityStatus,
    ReconcileFirst,
    Response,
    ReviewLimits,
    Route,
    SourceDecl,
    Stage,
    TaskRef,
    UnauthorizedChange,
    UpdateState,
)

__all__ = [
    "NOT_OBSERVED",
    "PROTECTED_TARGETS",
    "Account",
    "Admission",
    "Applicability",
    "Application",
    "Cause",
    "CauseHypothesis",
    "Comparison",
    "ConsequenceLedger",
    "Criterion",
    "Evaluation",
    "EvidenceKind",
    "Expectation",
    "Finding",
    "Horizon",
    "NotSupported",
    "Observation",
    "Ordering",
    "OwnerPolicy",
    "QuantityStatus",
    "ReconcileFirst",
    "Response",
    "ReviewLimits",
    "Route",
    "SourceDecl",
    "Stage",
    "TaskRef",
    "UnauthorizedChange",
    "UpdateState",
]
