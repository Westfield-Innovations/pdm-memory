# Goal Loop v1 (preview)

A job is described once, as a `GoalContract`. The `GoalLoopController` carries it
to the end: it waits cheaply, acts when a trigger fires, checks every step
before it runs, proves the result afterwards and writes down every move.

This is an opt-in, cooperative controller, not a sandbox. Keep the controller,
its store, the guard, the checkers and the executors outside the authority of
whatever proposes steps. Version 1 covers one category: watch a condition and
take one approved action when it changes.

## The promise and the plan

"The system may rewrite the plan. It may not rewrite the promise."

| Promise (needs `OwnerApproval`) | Plan (a new version, no approval) |
|---|---|
| `outcome`, `proof`, `never`, `autonomy`, `budget`, `sacrifice_order` | `preferences`, `trigger`, `notify`, `end` |

Every change writes version N+1 and keeps N. A contract without a named proof
source cannot be armed, and `arm()` also needs a checker for that source and an
executor for the job's operation.

Approval is cooperative. The SDK checks that the approval names the contract's
owner. Whether a person really gave it is the host's responsibility.

## States

```
drafted -> approved -> armed -> waiting -> triggered -> guard_checked
        -> acting -> verifying -> complete
```

Final states: `complete`, `out_of_budget`, `conflict`, `cancelled`, `impossible`,
`stopped`. Side states: `retrying` (waiting for its backoff), `escalated` (waiting
for a person) and `paused` (suspended by the owner).

`stopped` means the work ended without a proved result: the watch period closed
with an action unproved or nothing fired, or nobody confirmed whether a step
happened. Stopping is never a claim of success.

| From | To | When |
|---|---|---|
| drafted | approved | `approve()` by the owner |
| approved | armed, waiting | `arm()`; both moves in one commit |
| waiting | triggered | the trigger fired |
| triggered | guard_checked | the guard said proceed |
| triggered | escalated | outside the autonomy line, `verify()` found a partial conflict, or the guard failed on every try |
| triggered | retrying | the guard raised: a failed check, the step did not run, tries remain |
| triggered | conflict | `verify()` found a step contradicting a `never` rule |
| guard_checked | acting | the dispatch is reserved and stored |
| acting | verifying | the executor returned |
| acting | retrying | the executor raised `NotExecuted`, tries remain |
| acting | out_of_budget | `NotExecuted` on the last try |
| acting | escalated | any other error, or the lease ran out: outcome unknown |
| acting | cancelled | the step finished after `cancel()` was requested |
| verifying | complete | the checker proved it, `end.on_success="complete"` |
| verifying | waiting | the checker proved it, `end.on_success="rearm"`, budget remains |
| verifying | out_of_budget | proved with `rearm` but the budget is spent, or `FAILED` on the last try |
| verifying | retrying | `FAILED`, tries remain |
| verifying | impossible | the checker said `IMPOSSIBLE` |
| verifying | escalated | no proof before `proof_timeout` |
| retrying | triggered | the backoff ended; the step goes through the guard again |
| escalated | guard_checked, conflict | the owner approved; the guard ran again |
| escalated | waiting, cancelled | the owner skipped the step |
| escalated | verifying | evidence arrived for a step that may have run |
| escalated | triggered | the owner chose to retry after checking |
| escalated | stopped | a question about a step got no answer within `confirm_expiry` (unconfirmed), or the deadline passed while a step waited for approval |
| waiting | complete | the deadline passed and every action in the period was proved |
| waiting | stopped | the deadline passed with an action unproved, or nothing fired |
| waiting, retrying, escalated | paused | `pause()` |
| paused | the state it left | `resume()`, after the checker and executor are checked again |
| any live state except `acting` | cancelled | `cancel()` |

A step that is running cannot be recalled, so `cancel()` waits for it to return.
`complete` always carries a proof receipt; the controller refuses the move
without one. Closing a watch period reuses the last proof of that period.

## When the deadline passes

The deadline ends the watch period, as `ends_on` does in AZUS `goals`. After it
nothing new fires. A step that fired before it is not cut off: its retry still
runs, its proof is still accepted, and an open question to the owner still waits
for its answer or its expiry. Once nothing is open the period closes:

- every action in the period was proved: `complete`, `end_reason` starting with
  `end_date:`;
- an action was skipped, unproved or never ran: `stopped`;
- nothing fired: `stopped`. AZUS marks this period complete after checking that
  every close was observed; the controller does not know the host's observation
  coverage, so it cannot prove that nothing crossed.

## Pause

`pause(job_id, reason)` suspends a job that is waiting, retrying or escalated.
While paused nothing fires, retries or expires, and readings are ignored.
`resume(job_id)` checks the job's grounds again (a checker for the proof source,
an executor for the operation; `NotArmableError` otherwise, and the job stays
paused), returns to the state it left, and closes the period if the deadline
passed meanwhile. An armed trigger must see a reading below the line again, so a
crossing during the pause never fires late; a latched trigger stays latched.

## Why a job escalates

`escalated` carries a kind, and the kind decides which answers are valid.

| Kind | The step | Valid answers |
|---|---|---|
| `approval_required` | has not run | `resolve(proceed)` (the guard still checks it) or `skip` |
| `outcome_uncertain` | may have run | `submit_evidence()` if it did, `resolve(retry)` if the owner has checked it did not, or `skip` |
| `proof_timeout` | ran, no proof yet | `submit_evidence()` when the proof arrives, `resolve(retry)` or `skip` |

A retry reuses the same `action_id`, so the outside service can deduplicate, and
it counts as another attempt.

The two questions about a step that may have run (`outcome_uncertain`,
`proof_timeout`) stay open for `confirm_expiry` (7 days by default, as in AZUS).
Unanswered, the job ends `stopped` with `end_reason` starting with
`unconfirmed:`. A proof that arrives later through `submit_evidence()` is still
recorded on the stopped job as a `late_proof` receipt; the state does not change.

## Triggers

A `threshold` trigger has a fire line and a reset line. For a rising trigger
with `fire_at=40.00` and `reset_at=39.50`:

- it fires when a reading reaches 40.00 while armed, then latches;
- it re-arms only when a reading drops below 39.50 (39.50 itself does not);
- the first reading never fires, so a job armed while the price is already above
  the line waits for a reading below `fire_at` and a new crossing;
- while the job is busy a reset is honoured, but a crossing is not consumed: the
  trigger stays armed and the first reading above the line after the job is free
  fires it.

`event` triggers fire once per distinct `observation_id`. `schedule` triggers
fire on an observation with `event="schedule"` sent by the host's scheduler.
Version 1 runs one action at a time; an event arriving mid-action is recorded as
`fire_suppressed_busy` so the host can replay it.

## Guard and checker are two questions

- **Guard**: is this step allowed? `DefaultGuard` denies operations not listed in
  `autonomy.allowed`, then runs `verify()` against `never`. It runs on every
  step, including retries and steps an owner approved. A guard that raises fails
  closed: the step does not run, the failure counts as a try and the step is
  checked again after the backoff. When the tries are spent the job escalates as
  `approval_required`; a `proceed` runs the guard again. Put only prohibitions in `never`; things the
  controller enforces structurally belong in the trigger and the budget.
- **Checker**: did it work? It looks at the world, not at what the executor said,
  and returns `PROVEN` with a `ProofReceipt` from the contract's proof source,
  `NOT_YET`, `FAILED` or `IMPOSSIBLE`. A checker that raises counts as `NOT_YET`.

## Budget

| Field | Counts |
|---|---|
| `max_actions` | distinct actions (`action_id`) over the whole job; a retry never adds one |
| `max_attempts` | tries of one action, including the first and failed guard checks; defaults to `default_max_attempts` (2: one retry, as in AZUS `goals`) |
| `max_cost` | the sum of `ActionResult.cost`; checked before each dispatch |
| `deadline` | ends the watch period; see "When the deadline passes" |

Running out of actions, tries or money ends the job in `out_of_budget` with
`end_reason`, never in `complete`, even when every action was proved. For example
`max_actions=1 reached (1 dispatched)`. A failed last try ends the job at once
instead of waiting out a backoff first. Backoff is
`min(retry_base * 2 ** (attempt - 1), retry_cap)`.

## Receipts

Every move writes one `StepReceipt` in the same commit as the new state: kind,
from and to state, contract version, action id, reason, the guard's report, the
proof receipt, and the hash of the receipt before it. `verify_receipts()`
recomputes the chain and raises `LedgerIntegrityError` on any edit, gap or
reorder. This is tamper-evidence, not protection from someone who can rewrite
the whole store.

Receipts are written for moves and for notable events (a trigger changing state,
a crossing suppressed while busy, a proof rejected, a contract revised). A
reading that changes nothing leaves no receipt.

## Writing a `JobStore`

Subclass `JobStore` and implement `create`, `load`, `commit`, `receipts`,
`contract_history`, `due` and `active`. The important one is `commit`: store the
new job, its receipts and (when given) the contract history together or not at
all, and only if the stored revision still equals `expected_revision`; otherwise
raise `StaleJobError` and store nothing. `due(now)` returns active jobs whose
`next_due_at` is at or before `now`, oldest first.

Check your backend with the shared tests:

```python
from pdm_memory.goal_loop.testing import run_job_store_contract

def test_my_store(tmp_path):
    run_job_store_contract(lambda: MyStore(tmp_path / "fresh.db"))
```

`InMemoryJobStore` (the default) and `SqliteJobStore` (stdlib only, WAL with full
synchronization) ship with the SDK.

## Recovery

The dispatch is stored, with a lease (`action_timeout`), before the executor is
called. If the process dies in between, the job stays `acting`. When the lease
runs out, `tick()` escalates it as `outcome_uncertain`. It never runs the step
again by itself. A result that arrives later for such a step is still checked.
A job found in `triggered` or `guard_checked` (a commit chain that stopped
early) is finished by `tick()`.
