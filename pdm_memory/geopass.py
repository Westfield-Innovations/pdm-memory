"""
GeoPass — the SDK client for companion_api's GeoPass v0.2 §12 facade
(spec §5.3).

A thin, unfrozen wrapper the same way ``EventLog`` wraps ``Memory``: the
frozen surface (``memory.py``, ``storage/cloud_driver.py``,
``storage/errors.py``) is read from, never edited. Cloud only —
``GeoPass(memory)`` raises ``GeoPassUnavailable`` unless ``memory`` was
built with ``store="cloud"``, since every method here is a companion_api
HTTP call, not a local computation.

    from pdm_memory import Memory
    from pdm_memory.geopass import GeoPass

    mem = Memory(store="cloud", token="eyJ...")
    gp = GeoPass(mem)

    if gp.can("reveal", target_id):
        ...
    grant = gp.grant("agent:auditor", "reveal", target_id)
    gp.revoke(grant.id)

Distinct from GAA's ``verify()`` (README's "Guarded Agents" section):
GAA scores a proposed *action* against stored rules before it runs.
GeoPass answers whether an *observer* belongs to, and may act within, a
membership — a different question, over a different object, and the two
method sets never share a name.

``recognize`` is not exposed here. ``pdm.geopass_recognition.recognize()``
is a real, tested algorithm now (spec §5.6) — the "no algorithm yet"
reason the plan gave for withholding it is gone — but companion_api never
grew an HTTP route for it (neither §5.2 nor §5.8 built one), so there is
nothing this client could call. Revisit once that route exists.

``transition`` reaches spec §5.8's endpoint as built: five
``event_type``s are accepted server-side — ``explicit_consent`` and
``revocation`` (the caller must be the subject), plus
``employment_start``, ``employment_end`` and ``role_change`` (the caller
must own the target field, or be bootstrapping an empty one). Everything
else, ``court_order`` included, comes back 403
``AUTHORITY_TYPE_NOT_ALLOWED``. This client does not pre-filter: the
server is the one source of truth for which types are currently allowed,
so this docstring can go stale without the code going wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pdm_memory.storage.cloud_driver import CloudDriver

if TYPE_CHECKING:
    from pdm_memory.memory import Memory

__all__ = [
    "GeoPass",
    "GeoPassDecision",
    "GeoPassGrant",
    "GeoPassUnavailable",
]


class GeoPassUnavailable(RuntimeError):
    """``GeoPass(memory)`` was built over a non-cloud ``Memory``."""


@dataclass(frozen=True)
class GeoPassGrant:
    """A PerspectiveState row as grant()/revoke() report it."""

    id: str
    state: str


@dataclass(frozen=True)
class GeoPassDecision:
    """
    What an observer is told: allowed, and how much. Never carries
    ``reason_code`` — that stays audit-only server-side (see
    docvault.geopass_decision.ReasonCode's own docstring on why a reason
    can itself leak that a target exists) and companion_api's audit route
    does not currently serialize it either, so this field is always
    ``None`` until a server-side change adds it.
    """

    operation_allowed: bool
    permitted_view: str | None = None
    reason_code: str | None = None


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


class GeoPass:
    """See module docstring."""

    def __init__(self, memory: Memory) -> None:
        driver = getattr(memory, "_storage", None)
        if not isinstance(driver, CloudDriver):
            raise GeoPassUnavailable(
                'GeoPass requires a Memory built with store="cloud" — '
                "every method here is a companion_api HTTP call."
            )
        self._driver = driver

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        return self._driver._get(path, params={k: v for k, v in params.items() if v is not None}).json()

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        return self._driver._post(path, payload).json()

    def _patch(self, path: str, payload: dict[str, Any]) -> Any:
        return self._driver._patch(path, payload).json()

    # ------------------------------------------------------------------
    # GeoPass v0.2 §12
    # ------------------------------------------------------------------

    def belongs(
        self,
        field_id: str,
        *,
        at: datetime | str | None = None,
        known_at: datetime | str | None = None,
    ) -> bool:
        """
        Does the caller hold a live FieldMembership in ``field_id``?

        ``known_at`` answers from what was recorded by then, so an end
        backdated past ``at`` but recorded later does not apply. Needs ``at``.
        """
        if known_at is not None and at is None:
            raise ValueError("known_at requires at")
        data = self._get(
            "/api/v1/pdm/geopass/belongs/",
            {"field_id": field_id, "at": _iso(at), "known_at": _iso(known_at)},
        )
        return bool(data["belongs"])

    def can(
        self,
        operation: str,
        target_id: str,
        *,
        purpose: str | None = None,
        at: datetime | str | None = None,
    ) -> bool:
        """
        May the caller perform ``operation`` on ``target_id``? ``"act"`` is
        accepted as a synonym for ``"forward"``.
        """
        data = self._get(
            "/api/v1/pdm/geopass/can/",
            {
                "operation": operation,
                "target_id": target_id,
                "purpose": purpose,
                "at": _iso(at),
            },
        )
        return bool(data["can"])

    def view(
        self,
        target_id: str,
        requested_detail: str,
        *,
        purpose: str | None = None,
        at: datetime | str | None = None,
    ) -> str:
        """The minimum of ``requested_detail`` and what the caller may see."""
        data = self._get(
            "/api/v1/pdm/geopass/view/",
            {
                "target_id": target_id,
                "requested_detail": requested_detail,
                "purpose": purpose,
                "at": _iso(at),
            },
        )
        return str(data["permitted_view"])

    def grant(
        self,
        observer: str,
        operation: str,
        target: str,
        *,
        scope: dict[str, str] | None = None,
        interval: tuple[datetime | str | None, datetime | str | None] | None = None,
        reason: str = "",
    ) -> GeoPassGrant:
        """Create a PDM grant. The caller is always the grantor (server-side token)."""
        payload: dict[str, Any] = {
            "observer": observer,
            "operation": operation,
            "target": target,
        }
        if scope:
            payload["scope"] = scope
        if interval:
            valid_from, valid_to = interval
            payload["interval"] = {
                "valid_from": _iso(valid_from),
                "valid_to": _iso(valid_to),
            }
        if reason:
            payload["reason"] = reason
        data = self._post("/api/v1/pdm/geopass/grants/", payload)
        return GeoPassGrant(id=data["id"], state=data["state"])

    def revoke(self, grant_id: str, *, reason: str = "") -> GeoPassGrant:
        """Revoke a grant by id. The caller is always the grantor."""
        data = self._patch(
            f"/api/v1/pdm/geopass/grants/{grant_id}/revoke", {"reason": reason}
        )
        return GeoPassGrant(id=data["id"], state=data["state"])

    def audit(self, decision_id: str) -> dict[str, Any]:
        """
        Look up a ``GeoPassDecision.audit_event_id``. Raises
        ``CloudStorageError`` (404-shaped) for a decision the caller
        neither owns nor granted — identical to one that never existed
        (see companion_api's own NoPermittedInformation).
        """
        return self._get(f"/api/v1/pdm/geopass/audit/{decision_id}", {})

    def transition(
        self,
        event_type: str,
        subject_id: str,
        *,
        payload: dict[str, Any] | None = None,
        effective_at: datetime | str | None = None,
        evidence_ref: str = "",
        reason: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        File (or, with ``dry_run``, only preview) an AuthorityEvent. See
        this module's own docstring for which ``event_type``s the server
        accepts; the rest raise via its 403.
        """
        body: dict[str, Any] = {
            "event_type": event_type,
            "subject_id": subject_id,
            "payload": payload or {},
            "dry_run": dry_run,
        }
        if effective_at is not None:
            body["effective_at"] = _iso(effective_at)
        if evidence_ref:
            body["evidence_ref"] = evidence_ref
        if reason:
            body["reason"] = reason
        return self._post("/api/v1/pdm/geopass/transitions/", body)
