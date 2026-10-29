# Boundary Lock Guarded Effects Specification

Version 1 | October 23 2026 | SDK 0.2.6

Westfield Innovations | Carl D. Hildebrandt

## Purpose and the larger direction

Security warnings about website software and industrial controllers led to a practical question: who retains control when incoming software asks to change something? The design developed from a gate around incoming requests into an owner-controlled boundary around the effects a program can produce. Retained earlier states then added room to recover without erasing the verdict that stopped the program.

The SDK supplies a small, useful part of that larger design. It retains exact action requests, checks owner-defined conditions before dispatch, records what happened and restores explicit worker state without rewinding the protected history. The same mechanism can support an agent tool call, a shared-memory write, an AZUS reasoning step or a LineBoss request. Each environment supplies its own permitted operations and actual enforcement.

The release condition is whichever measurement closes the relevant leak. It can be a verdict, current equipment state, an evidence revision, acknowledgment of an earlier step, a quantity limit, time validity or a declared combination. Three and six seconds were examples of recovery positions. Waiting alone never grants permission.

This specification separates the common mechanism from the size of the installation. The first SDK increment uses explicit application state and registered operations. The larger owner-controlled computer environment remains a separate embodiment; this increment does not capture arbitrary computer memory or provide operating-system isolation.

## Inspected baseline and implemented increment

The baseline is the public pdm-memory main revision `111b4b2bd8c948a6c081c40d54eda66ef881aa17`, inspected October 23 2026. Its package version is 0.2.5.

| Existing component | Actual responsibility | Relationship to this increment |
| --- | --- | --- |
| `verify()` and `Memory.verify_alignment()` | Score proposed text against goal anchors; return ALIGNED, CONFLICT or TORSION | Preserved unchanged; may supply one finding to a stronger operation check |
| OpenClaw before and after hooks | Check tool requests and construct receipts | Harden error handling and rule ownership; retain the host hooks as integration seams |
| `EventLog` and eventful storage | Store source-event identity, evidence references and linked memories | Optional evidence destination; not the authoritative action or recovery ledger |
| Ordinary PDM memories | Retain, recall, reinforce, reconcile and decay meaning signatures | Useful for reasoning about decisions; decay must not erase action authority or outcomes |
| `GeoPass` client | Cloud relationship and authority operations | Optional external authority evidence; not silently required by the local increment |
| New `GuardedEffects` | Exact requests, current checks, protected dispatch history and explicit worker recovery | New opt-in module; no change to ordinary Memory semantics |

The preview also changes the OpenClaw plugin's default response to missing rules or a failed checker: protected calls are blocked. Its owner can explicitly select the previous permissive error behavior for compatibility, but that setting does not provide the protected guarantee.

## Boundary and ownership

The trusted host owns the guard, the operation registry, condition evaluators, configuration, ledger and recovery records. The worker supplies structured requests. It cannot supply an executor, redefine the target mapping, choose the judge or promote its own checkpoint into authority.

The Python module enforces this contract for calls routed through its owner-held object. An embedding application must keep that object, its files and its underlying resource access outside the worker's control. A worker that can directly call the original tool or edit the ledger has another crossing that this library alone cannot close.

Use narrow registrations such as `memory.save` for one owner store or `pump.request` for one named equipment target. Avoid registrations equivalent to executing an arbitrary command, opening any file or contacting any destination. Registering a broad executor grants broad effects.

## Exact requests and stable action identity

An `EffectRequest` identifies an `action_id`, `branch_id`, admitted `program_version`, current `policy_version`, `operation`, `target` and complete JSON `parameters`. The guard namespace supplies the owner. Operation and target must match an owner registration.

`action_id` identifies one intended action and remains stable across retries. It is separate from the program, branch, policy and observed-state revisions. Two legitimate identical operations may have different action identities. Changing a branch or policy must not silently convert an uncertain old action into a new one.

Staging retains a canonical copy of the full request. Reusing an action identity with different content is an error. Later mutation of the caller's dictionary cannot alter the staged request. JSON must be finite and bounded; do not accept a prose summary or shortened tool description as the authoritative request.

The host must bind the request's identity and versions to the actual worker session it controls. A fingerprint or version string supplied by an untrusted worker is an assertion, not authentication.

The ledger retains the current owner configuration and a generation number. Reopening the same configuration preserves that generation. Replacing it requires the owner's explicit `replace_config=True` and advances the generation, including when reusing an earlier version label. The ledger also fingerprints the declared openings: each operation and target, its dispatch allowance and whether it has a current check. Reopening with different openings under the same version labels is refused until the owner replaces the configuration. A ledger created by the 0.2.6a1 preview has no fingerprint; the first reopen with the same version labels records the current openings and keeps the configuration generation. The host must still activate a new configuration when changing what a handler or check does; the library cannot inspect a callback and discover a policy change. Superseded controller objects cannot mutate current work, and old held requests do not regain permission when a label is reused. Configuration replacement cannot retract a handler already dispatched; the host must settle or stop that work when immediate revocation is required.

## Current conditions and release measurements

Every opening supplies a parameter validator and may supply a current-condition check. These functions belong to the owner. They receive the retained structured request. Conditions obtain their evidence through trusted host connections, rather than accepting the worker's declaration that its prerequisites are true.

| Condition disposition | Meaning | Execution consequence |
| --- | --- | --- |
| SATISFIED | The declared condition is supported | May proceed only if all other gates also permit |
| CONTRADICTED | Evidence establishes incompatibility | Reject the request |
| UNKNOWN | Required evidence is missing or unavailable | Keep held; no dispatch |
| CONFLICT | Material evidence disagrees | Keep held; no dispatch |

`ConditionVerdict` retains the disposition, reason and supporting evidence metadata. A check exception or invalid result supplies no permission. A later call to release may re-evaluate a held request against current evidence. An elapsed timer is not a release decision.

For an earlier-step acknowledgment, check that the named prior operation has the required recorded outcome. For a state revision, obtain the current revision and compare it with the contract. For time, declare what clock and validity interval matter. For quantity, enforce counters outside worker recovery. These are different measurements using the same release boundary.

A current check and an external operation are separate steps. When the condition can change between them, the adapter needs a final local check or an operation that compares the expected state and applies the change together. A remote reading alone cannot establish that the world stayed unchanged until the effect occurred.

GAA remains useful for interpreting proposed intent. Its ALIGNED result means that its check detected no blocking textual conflict; it does not itself establish permission for an arbitrary operation, target or parameter set. Hard missing or conflicting evidence cannot be overridden by a favorable alignment score.

## Action lifecycle and delivery uncertainty

The guard stages a request, validates its current authority and scope, evaluates current conditions, reserves applicable allowance and durably records dispatch intent before entering the registered handler. Concurrent attempts to release one action must not cause multiple executions.

| Lifecycle state | What the caller may conclude |
| --- | --- |
| HELD | The protected executor has not been entered; the request can be reconsidered or cancelled |
| REJECTED | The request did not meet the declared boundary; the reason survives recovery |
| DISPATCHING | Dispatch was reserved and recorded; execution may be in progress or interrupted. Releasing it again from the dispatching controller returns this state unchanged; any other controller records it as UNCERTAIN |
| RETURNED | The registered handler returned a result; this is not automatic proof of a physical outcome |
| UNCERTAIN | Execution or its outcome cannot be established from the available record; do not blindly retry |
| CANCELLED | A held request was invalidated without dispatch |
| OBSERVED | The owner recorded explicit outcome evidence; `executed` states whether the operation was observed to execute |

An executor exception does not establish that no side effect occurred. A crash after the durable reservation can occur before or after the receiving system acts. Reopening the ledger never automatically dispatches that action again. Trusted reconciliation records evidence about execution and the observed result; it does not erase history or automatically resend.

Exactly-once physical execution is not supplied by a local ledger. A receiving system may provide its own duplicate protection. Otherwise the adapter must reconcile present state before any separately authorized retry. The host must not label a returned message as a measured physical response without an actual observation.

Ordinary changed conditions can keep a healthy worker's request held or refused. They do not require restoring the worker. Recovery is a separate operation for an interrupted, rejected or invalid working branch.

## Protected history and bounded worker recovery

The new local ledger is separate from ordinary PDM signatures. It retains complete action binding, verdict metadata, dispatch attempts, results, uncertainty, branch state, counters and checkpoint metadata. SQLite transactions reserve dispatches and the ledger requests full synchronous durability. Actual durability still depends on the host storage honoring that contract.

The first recovery format is explicit bounded JSON application state, captured by the trusted host at a declared worker step. It is not a raw process snapshot. A checkpoint records its branch and admitted program and policy versions. Calling it accepted identifies an owner-admitted recovery position; it does not certify every possible future behavior of the program.

Recovery retires the current branch, cancels its held work and returns the retained state plus surviving action history to a fresh branch. The guard keeps current policy and does not restore expired permissions from the checkpoint. An incompatible program state must not be loaded into another admitted program version without a separately defined conversion.

The restored worker receives the current world state through trusted inputs and the surviving action receipts. The guard continues enforcing refusals and limits even if the worker ignores those receipts. Keep current authority, cumulative counters, revocations and uncertain outcomes outside the recoverable state.

The preview bounds individual requests, checkpoint state and outstanding held actions. Its lifetime dispatch allowance is a protected cumulative limit. More elaborate rate windows, cross-worker quotas, automatic retention and eviction, or arbitrary process capture require additional explicit contracts. The host also owns computation and storage limits for the execution environment.

## SDK interface

All new types are available from `pdm_memory` and `pdm_memory.guarded_effects`. The module uses the standard Python library and adds no required runtime dependency. It accepts a persistent SQLite file; empty or in-memory paths are refused.

| Type or operation | Contract |
| --- | --- |
| `GuardedEffects(path, *, owner, program_version, policy_version, openings, replace_config=False, ...)` | Owner-held controller and durable ledger; one current branch per owner namespace |
| `Opening(operation, target, handler, validate, check=None, max_dispatches=None)` | Exact operation and target mapping, required parameter validator, optional current check and cumulative dispatch allowance |
| `EffectRequest(action_id, branch_id, program_version, policy_version, operation, target, parameters={})` | Complete immutable capture of one proposed action; the input object itself is not execution permission |
| `ConditionVerdict(status, reason="", evidence={})` | A condition finding using `CheckResult`; callbacks may also return that enum, a boolean or `None` for unknown |
| `start_branch(branch_id)` | Establish the first branch; a different active branch requires explicit recovery |
| `stage(request)` | Capture and validate a request without executing it; return an `ActionReceipt` |
| `release(action_id)` | Recheck current configuration, branch, parameters, conditions and allowance; reserve before dispatch |
| `receipt(action_id)` and `cancel(action_id, reason=...)` | Read the current action receipt or cancel held work |
| `dispatch_count(operation, target)` | Read cumulative dispatch attempts for this owner and opening; recovery does not reset them |
| `reconcile(action_id, *, executed, evidence)` | Record trusted evidence for a returned or uncertain action; never resend it. `executed=None` attaches the evidence and keeps the current RETURNED or UNCERTAIN status |
| `checkpoint(checkpoint_id, *, branch_id, state)` | Capture bounded explicit worker state; return the checkpoint identity |
| `recover(checkpoint_id, *, new_branch_id, receipt_limit=128)` | Retire the old branch and return `RecoveryContext` with captured state, current versions and bounded surviving receipts |
| `history(action_id=None, *, after_sequence=0, limit=100)` | Read ordered events; continue from the last returned sequence |
| `verify_history()` and `close()` | Verify the retained chain or close the ledger; context-manager use is supported |

The constructor defaults to 65,536 bytes for a complete request, 65,536 bytes for a result or check evidence, 1,048,576 bytes for checkpoint state and 128 held requests. The corresponding options are `max_request_bytes`, `max_result_bytes`, `max_state_bytes` and `max_pending`. JSON nesting and value count are also bounded. Limits are owner configuration, separate from worker state.

`ActionReceipt` includes the action identity, lifecycle status, condition disposition, reason, branch, program and policy versions, captured request hash, event sequence, result, evidence and observed execution field. A returned handler result leaves `executed` unknown until observation is supplied. `RecoveryContext` identifies the restored checkpoint and new branch, captured state, checkpoint and current versions, surviving receipts, pagination flag and history starting sequence. When `has_more_receipts` is true, read the remaining ordered history before treating the returned evidence as complete.

The exceptions distinguish identity reuse, invalid branches, superseded configurations, resource bounds and inconsistent retained content: `IdReuseError`, `BranchError`, `ConfigurationError`, `ResourceLimitError` and `LedgerIntegrityError`. Storage errors propagate rather than permitting dispatch without a secured record.

Run the shipped examples from the installed preview:

```bash
python -m pdm_memory.examples.guarded_effects
python -m pdm_memory.examples.guarded_pdm_write
```

The second example creates a real PDM store. A note remains held while its evidence has no owner review. Matching the reviewed revision and exact text permits one SDK dispatch; reusing the evidence label for different text is refused. Restoring earlier reasoning state retains that dispatch record, and releasing the original action again does not write a second note. This demonstrates the shared-memory use without changing ordinary PDM save or recall behavior.

## Application in PDM and AZUS

For shared-memory writes, the host retains the actual `Memory` object and registers a narrow save operation. Incoming material proposes the text, tags and permitted source fields. Owner checks determine what can become active shared memory. The request, decision and outcome remain in the guard ledger even if an ordinary memory is later reinforced, decayed or reconciled.

The source-event layer can retain a reference to a guard event and PDM can derive a concise memory from it. That is an evidence mirror after the authoritative record exists. A failed mirror must not turn a completed effect into a new action or erase the original verdict. The preview does not automatically insert guard history into a customer's PDM store.

In AZUS reasoning, a reasoning worker proposes a next step and predicted consequence. The owner-held guard checks the permitted effect and current evidence. After execution, actual observations are recorded separately from prediction. Restoring reasoning state does not reset the protected record of what was released, refused or uncertain.

## Application in OpenClaw and LineBoss

The existing OpenClaw before-call hook is the place to obtain an alignment finding and refuse an inadmissible tool request. The after-call hook supplies the returned result. The hardened plugin must block protected calls when checking cannot be completed, prevent agent-controlled rule changes and avoid assigning an outcome to the wrong concurrent request.

The new Python lifecycle is available to trusted executors now. This preview does not claim that the TypeScript host hooks already route every tool through that durable Python executor. A complete OpenClaw adapter must bind trustworthy host call identities, reserve before dispatch, use full parameters and preserve the returned or uncertain outcome through host restart. Tools outside the hooked host path remain separate crossings.

For LineBoss, register bounded equipment requests and obtain present conditions from the local controller or independently protected observations. The controller retains immediate physical permissives. A request that can wait remains held until its declared conditions are satisfied. Immediate local protection continues without waiting for a reasoning response or ledger round trip.

Starting and stopping require action-specific rules. A condition needed to start a pump must not accidentally prevent its required stop. The exact pump role, allowed sequences, physical limits, current observations and loss behavior belong to that equipment adapter. The SDK does not invent production tag names or universal stop values.

## First increment and the next pieces

The first delivery comprises the opt-in guarded-effects module, meaningful regression coverage, examples for a non-time release condition and a guarded PDM write, and OpenClaw hardening. It preserves existing memory and GAA calls. The package is a reviewable alpha preview, not a published release.

The next integration pieces are the durable OpenClaw executor adapter, selected AZUS reasoning-state capture and the LineBoss equipment adapter. Each must state which crossings it controls and how it observes results. These pieces share the same action and recovery contract without pretending that their execution environments are interchangeable.

The broader computer product can place an entire changing computer environment inside an owner-controlled enclosure, with selected files, displays, communications and equipment access retained outside. That is the same theme at a larger boundary. It requires its own resource mapping and enforcement work rather than control of every hardware component in this first module.

## Review and delivery evidence

The implementation must demonstrate that a non-time condition can hold and later permit an action; changed request content cannot reuse an identity; concurrent release attempts do not dispatch twice; unknown evidence and check failures do not execute; uncertain outcomes survive restart; and recovery preserves decisions, cancels old held work and retains cumulative limits.

Existing SDK alignment, storage and evidence behavior must remain compatible. OpenClaw coverage must exercise the registered hooks, including unavailable checking, missing rules, inconsistent responses and owner-only rule changes. Test results and the exact delivered file inventory belong to the handoff record.

| Completed check | Result and scope |
| --- | --- |
| Full Python suite on Python 3.12 | 724 passed and 15 skipped; skipped tests require a configured live PostgreSQL service |
| Final guarded-effects and PDM integration coverage | 46 passed; includes current evidence, exact request binding, configuration generations, concurrency, uncertainty and recovery |
| OpenClaw plugin suite on Node 24 | 46 passed; includes 33 tests invoking actual registered hooks in the shipped bundle |
| Bundled examples | Non-time simulation and real PDM save run locally; no production controller or deployed OpenClaw host used |

The preview was delivered as a 0.2.6a1 wheel, source archive and patch against `111b4b2`. It ships in pdm-memory 0.2.6 with fixes to same-controller re-release, unknown reconciliation of returned actions and the openings fingerprint.

This specification builds on the six-panel review and the earlier Boundary Lock Control and Recovery Across Time paper. It does not treat panel agreement as implementation evidence or claim patent novelty. The inspected repository revision, implementation and recorded checks define what this preview actually contains.
