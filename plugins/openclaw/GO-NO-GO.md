# The Guard — OpenClaw Spike Report (GO/NO-GO)

Westfield Innovations — Internal | August 2026 | v2.1  
**Decision: GO**

---

## Summary

Standalone GAA (`verify(intent, goals)`) was proven inside OpenClaw via `before_tool_call`.  
One real action was blocked; one real action was permitted after rule change.  
Receipts were captured in gateway logs — not screenshots.

---

## Report fields (Definition of Done)

| Field | Value |
|-------|--------|
| **Owner** | Demian |
| **Done** | 2026-08-20 |
| **Runtime** | OpenClaw **2026.7.1-2** (local gateway, `openclaw tui`, agent `main`) |
| **Block test result** | **PASS** — `exec` with `curl http://localhost:8080/api/health` blocked when rule `never hardcode localhost` active; `tool_executed: false`, `gate_status: TORSION` |
| **Permit test result** | **PASS** — same curl permitted when only unrelated rule `never write lorem ipsum` active; `tool_executed: true`, `gate_status: ALIGNED` (exec ran; connection refused is network, not gate) |
| **Fallback runtime** | n/a — first candidate socket (OpenClaw) passed hard gate and spike; no swap required |
| **Release date** | Standalone gate: **pdm-memory 0.2.4** (PyPI). OpenClaw plugin + relevance fix: **pending merge** (`feat/openclaw-gaa-plugin` + local `alignment.py` patch) |

---

## Hard gate (before plugin)

**Question:** Does `before_tool_call` actually stop the tool, not just log?

**Answer:** Yes. Verified before and during plugin spike.

- Hook returns `{ block: true, blockReason }` when `verify()` reports `is_safe_to_act: false`
- Blocked runs: no shell side-effect from blocked `exec`
- Permitted runs: `exec` reaches the shell (observed `curl: (7) Failed to connect…`, exit code 7)

---

## Spike scenario (exact mode demonstrated)

### Setup

- Plugin: `pdm-memory/plugins/openclaw/pdm-guard.js`
- Config: `~/.openclaw/openclaw.json` → `plugins.entries.pdm-guard`
- Rules file: `~/.openclaw/pdm-guard-rules.json`
- Verify path: `verify_bridge.py` → `pdm_memory.verify()` (local Python subprocess; no HTTP sidecar)
- Rule management: `/pdm-guard list|add|remove` (slash command, bypasses LLM)

### Test 1 — Block

1. `/pdm-guard add never hardcode localhost`
2. Agent asked to run: `curl http://localhost:8080/api/health`
3. **Result:** Blocked. User-visible: `Action blocked by PDM guard (TORSION)…`

### Test 2 — Permit

1. Rules: only `never write lorem ipsum` (unrelated to localhost curl)
2. Same curl command
3. **Result:** Permitted. `curl` executed; exit 7 (nothing listening on :8080)

### Test 3 — Block again (rule re-added)

1. `/pdm-guard add never hardcode localhost` (alongside lorem rule)
2. Same curl
3. **Result:** Blocked again (TORSION)

---

## Receipts (proof)

Source: `/tmp/openclaw/openclaw-2026-08-20.log`  
Format: `[PDM GUARD] RECEIPT: {…}`

### Permit receipt

```json
{
  "timestamp": "2026-08-20T08:13:42.414Z",
  "proposed_action": "exec (command=curl http://localhost:8080/api/health)",
  "gate_status": "ALIGNED",
  "governing_rules": ["never write lorem ipsum"],
  "rules_source": "/Users/admin/.openclaw/pdm-guard-rules.json",
  "tool_executed": true,
  "explanation": "Intent does not engage any guard rule (max anchor resonance=0.00, peak torsion=0.00). Proceed.",
  "pdm_version": "0.2.4",
  "openclaw_version": "2026.7.1-2",
  "resulting_state": {
    "executed": true,
    "side_effects": "observed",
    "tool_name": "exec",
    "exit_code": 7,
    "stdout": "",
    "stderr": "curl: (7) Failed to connect to localhost port 8080",
    "error": null,
    "detail": "exec side-effect observed"
  },
  "duration_ms": 42
}
```

### Block receipt

```json
{
  "timestamp": "2026-08-20T07:56:42.546Z",
  "proposed_action": "exec (command=curl http://localhost:8080/api/health)",
  "gate_status": "TORSION",
  "governing_rules": ["never hardcode localhost"],
  "rules_source": "/Users/admin/.openclaw/pdm-guard-rules.json",
  "tool_executed": false,
  "explanation": "This intent is dangerous for system integrity: 'exec (command=curl http://localhost:8080/api/health)' contradicts Goal Anchor 'never hardcode localhost' (intent performs what the goal forbids (localhost)). Block ACT until the intent is revised.",
  "pdm_version": "0.2.4",
  "openclaw_version": "2026.7.1-2",
  "resulting_state": {
    "executed": false,
    "side_effects": "none",
    "tool_name": "exec",
    "exit_code": null,
    "stdout": null,
    "stderr": null,
    "error": null,
    "detail": "tool call stopped at before_tool_call; host did not execute"
  },
  "tool_result": null,
  "duration_ms": null
}
```

Permit receipts are logged from `after_tool_call` with `resulting_state` populated from the real tool outcome (stdout/stderr/exit code when available).

---

## Part 1 — Standalone gate (companion to spike)

| Item | Status |
|------|--------|
| `verify(intent_text, goals)` store-free | Done — `pdm-memory` 0.2.4 |
| Local verify bridge (no HTTP) | Done — `plugins/openclaw/verify_bridge.py` |
| AZUS doors 1–2 (explain + paste code) | Done — `pdm-standalone-guard` doc |
| AZUS door 3 (hosted no-code guard) | Done — `guard_rules` + Junior gate |
| Unrelated rules must not block unrelated actions | Fixed during spike — `RELEVANCE_MIN_RESONANCE` in `alignment.py` |

---

## OpenClaw lane confirmation

From OpenClaw plugin/hook documentation: `before_tool_call` can block, rewrite, or require approval.  
No existing OpenClaw plugin in their catalog provides pre-action goal-alignment against user-stated rules.  
Memory lane in that ecosystem has incumbent alternatives; gate lane remains open.

---

## Not in scope (unchanged)

- OpenClaw replacement or universal agent platform
- Full memory system for OpenClaw
- All adapters / channels
- Cloud auto-setup for OpenClaw

---

## Artifacts

| Artifact | Path |
|----------|------|
| Plugin source | `pdm-memory/plugins/openclaw/pdm-guard.ts` |
| Receipt helpers | `pdm-memory/plugins/openclaw/receipt.ts` |
| Plugin runtime | `pdm-memory/plugins/openclaw/pdm-guard.js` |
| Rules store | `pdm-memory/plugins/openclaw/rules-store.ts` |
| Manifest | `pdm-memory/plugins/openclaw/openclaw.plugin.json` |
| README | `pdm-memory/plugins/openclaw/README.md` |
| Live tests (standalone) | `pdm-memory/tests/test_verify_live.py` |
| Plugin tests (OpenClaw) | `pdm-memory/plugins/tests/openclaw/` |
| Alignment tests | `pdm-memory/tests/test_alignment.py` |

---

## Beat (30-second proof)

A user adds a rule in plain English (`/pdm-guard add never hardcode localhost`), asks the agent to curl localhost, and watches the action stop before it runs. That happened live on 2026-08-20.

**GO.**
