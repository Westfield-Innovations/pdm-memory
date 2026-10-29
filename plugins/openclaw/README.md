# PDM Guard — GAA Gate for OpenClaw

[![PDM Memory](https://img.shields.io/pypi/v/pdm-memory)](https://pypi.org/project/pdm-memory/)
[![OpenClaw](https://img.shields.io/badge/OpenClaw-plugin-blue)](https://github.com/openclaw/openclaw)
[![Documentation](https://img.shields.io/badge/Docs-azus.ai%2Fsupport-blue)](https://azus.ai/support)

**PDM Guard** runs `verify()` at OpenClaw's `before_tool_call` hook, comparing a summary of a proposed action with owner-controlled, plain-English rules before that tool executes. Missing rules and failed checks block execution by default.

- 🛡️ **Goal-Anchor Alignment.** Same gate as `pdm-memory`: **ALIGNED**, **CONFLICT**, or **TORSION**.
- 📝 **Plain-English rules.** Add `never hardcode localhost` — no code, no PDM store required.
- 🔒 **Local rules file.** `~/.openclaw/pdm-guard-rules.json` — your data stays on your machine.
- 🧾 **Compact receipts.** Gate decisions and matched tool completions produce local JSON log entries.
- ⚡ **Five minutes.** Install plugin → add one rule → watch a violating action stop before it runs.

📖 **GAA background:** [Guarded Agents — GAA & Torsion](https://azus.ai/support)

**Existing live-host test baseline:** OpenClaw **2026.7.1-2** (see spike report for block/permit receipts). The hardening tests also invoke the registered hooks with a stub host entry helper; they do not replace testing a deployed host version.

---

## Table of Contents

1. [What It Does](#-what-it-does)
2. [Prerequisites](#-prerequisites)
3. [Quick Start](#-quick-start)
4. [Configuration](#-configuration)
5. [Managing Rules](#-managing-rules)
6. [Behavior & Receipts](#-behavior--receipts)
7. [Developer Tools](#-developer-tools)
8. [API Reference](#-api-reference)
9. [License](#-license)

---

## 🛡️ What It Does

Before an agent **acts**, PDM Guard:

1. Loads guard rules from `~/.openclaw/pdm-guard-rules.json` (or config fallback).
2. Builds an intent string from the tool name and payload (`command`, `text`, `patch`, …).
3. Calls `pdm_memory.verify(intent, goals)` directly — spawns a short-lived Python process (`verify_bridge.py`) that imports `pdm_memory` in-process and calls the function. No HTTP, no server to run.
4. Validates the checker's response, then **blocks** unsafe results or **permits** a valid `ALIGNED` result. Missing rules, checker errors, timeouts, and malformed or inconsistent responses block by default.

Statuses (same as standalone GAA):

| Status | Meaning | Tool runs? |
|--------|---------|------------|
| **ALIGNED** | No conflict detected in the summarized intent | Yes |
| **CONFLICT** | Soft mismatch / fail-closed | No |
| **TORSION** | Hard contradiction | No |

The agent can list rules using `pdm_guard_rule`. It cannot add or remove its own restrictions. Rule changes use the direct `/pdm-guard` command and require a verified owner; that command is independent of the action checker so an owner can repair missing rules.

This is a text-alignment hook, not a complete permission boundary. `ALIGNED` does not prove that a tool is authorized to affect every resource it can reach. The host must invoke the hook for every protected action. The plugin does not isolate programs, intercept arbitrary computer operations, or undo external effects.

---

## 📦 Prerequisites

```bash
pip install pdm-memory          # verify() engine (>= 0.2.4), needs Python >= 3.10
# OpenClaw gateway with plugin hooks (tested: 2026.7.1-2)
```

The plugin spawns a `python3` process to call `verify()` — no server, sidecar, or network hop required. Point `pythonBin` (config) or `PDM_GUARD_PYTHON` (env) at whichever interpreter has `pdm-memory` installed if it isn't your default `python3`.

No PDM store, AZUS account, or signup required for the gate itself.

---

## ⚡ Quick Start

### 1. Enable the plugin

`~/.openclaw/openclaw.json`:

```json5
{
  plugins: {
    load: {
      paths: ["/path/to/pdm-memory/plugins/openclaw/pdm-guard.js"]
    },
    entries: {
      "pdm-guard": {
        enabled: true,
        hooks: {
          allowConversationAccess: true
        },
        config: {
          pythonBin: "python3",
          rulesFile: "~/.openclaw/pdm-guard-rules.json",
          timeoutMs: 8000
        }
      }
    }
  }
}
```

Restart the gateway:

```bash
openclaw daemon restart
```

### 2. Add a rule as the owner

```text
/pdm-guard add never hardcode localhost
```

The host must identify you as both authorized and an owner. If your host supplies only an authorized sender identity, configure your exact identity in `ownerSenderIds`; missing owner evidence denies changes. Add the first rule before asking the agent to run other tools, since an empty rule set blocks by default.

### 3. Trigger a violating action

Ask the agent to run something that breaks the rule, e.g.:

```text
curl http://localhost:8080/api/health
```

**Expected:** the agent stops before `exec` runs. User-visible message:

```text
Action blocked by PDM guard (TORSION): …
```

### 4. Confirm the receipt

```bash
tail -1 /tmp/openclaw/pdm-guard-receipts.jsonl
```

Clean one-liner (not the OpenClaw gateway envelope). Blocked example:

```json
{"time":"2026-08-21T08:33:18.388Z","status":"TORSION","tool":"write","action":"write path=config.py","rules":["never create a file named config.py"],"executed":false,"why":"…"}
```

---

## ⚙️ Configuration

### Plugin config (`openclaw.json` → `plugins.entries.pdm-guard.config`)

| Key | Default | Description |
|-----|---------|-------------|
| `pythonBin` | `python3` | Interpreter used to run `verify_bridge.py` — must have `pdm-memory` importable |
| `rulesFile` | `~/.openclaw/pdm-guard-rules.json` | Local rules JSON path |
| `timeoutMs` | `8000` | Positive time budget, at most 2147483647 milliseconds, for the checker process |
| `goals` | `[]` | Legacy fallback rules when the rules file is empty |
| `failureMode` | `"block"` | Default blocks missing rules and failed checks. Explicit `"allow"` is **unprotected compatibility mode** for those failures. It still blocks a valid checker refusal. |
| `ownerSenderIds` | `[]` | Exact authenticated host sender identities permitted to change rules when the host does not supply verified owner status |

### Environment overrides

| Variable | Effect |
|----------|--------|
| `PDM_GUARD_PYTHON` | Overrides `pythonBin` |
| `PDM_GUARD_RULES_FILE` | Overrides `rulesFile` |
| `PDM_GUARD_GOALS` | Pipe-separated fallback rules (`rule one\|rule two`) |
| `PDM_GUARD_RECEIPT_LOG` | Clean receipt JSONL path (default `/tmp/openclaw/pdm-guard-receipts.jsonl`) |

Plugin config is read from `api.pluginConfig` at `register()` time — typed hooks do not populate `ctx.pluginConfig`.

`failureMode` and `ownerSenderIds` are taken only from the registered owner configuration. A per-call hook context cannot enable unprotected compatibility mode or appoint an owner. Protect this configuration and the rules file from the agent's ordinary file-writing tools using host or operating-system permissions; this plugin cannot supply that protection itself.

### Rules file format

`~/.openclaw/pdm-guard-rules.json`:

```json
{
  "version": 1,
  "rules": [
    {
      "id": "a1b2c3d4e5f6",
      "text": "never hardcode localhost",
      "created_at": "2026-08-20T10:00:00.000Z"
    }
  ]
}
```

---

## 📋 Managing Rules

### Owner slash commands (bypass the model)

```text
/pdm-guard list
/pdm-guard add never hardcode localhost
/pdm-guard remove never hardcode localhost
```

Aliases: `ls`/`show`, `set`/`create`, `delete`/`rm`/`drop`.

The command is registered with `requireAuth: true`. Add/remove also require `isAuthorizedSender: true` plus either host-verified `senderIsOwner: true` or an exact match in owner-configured `ownerSenderIds`. When the host supplies `assertOwnerCurrent`, the handler rechecks owner standing immediately before mutation. An authorized non-owner cannot change rules. Older hosts without owner metadata need the explicit identity list; authentication alone is insufficient.

### Agent tool

If the model has `pdm_guard_rule` allowlisted, it can inspect the active rules:

```json
{ "action": "list" }
```

Add/remove are rejected both by the registered before hook and by the tool implementation, including a direct call that skips the hook. Ask the owner to use `/pdm-guard` for changes.

Keep the normal coding profile in `openclaw.json`. Do **not** set `tools.allow` to only `pdm_guard_rule` — that replaces the whole tool catalog and removes `exec` / other coding tools.

---

## 🔍 Behavior & Receipts

| Condition | Gate behavior |
|-----------|---------------|
| Rules file has entries | Verify normal tool calls; agent rule listing is separate |
| No rules anywhere | Block (`BLOCKED_NO_RULES`) |
| Rules cannot be loaded | Block (`BLOCKED_RULES_ERROR`) |
| Invalid checker timeout | Block (`BLOCKED_CONFIG_ERROR`) |
| Checker cannot start, fails, times out, or returns malformed/inconsistent output | Block (`BLOCKED_VERIFY_ERROR`) |
| `verify()` → not safe | Block with reason shown to user |
| Owner explicitly sets `failureMode: "allow"` and a check cannot be made | Proceed without protection, with an `UNPROTECTED_*` receipt when the outcome can be matched |
| Agent attempts to add/remove a rule | Block (`BLOCKED_OWNER_RULE_CHANGE`) |

Intent strings are built by `intent.ts`: tool name, then `path=` (basename) / `command=` first; `content≈` is a short preview only. The label is `path=`, not `file=` — the word `file` false-triggers rules like "never create a file named …".

These summaries omit information and clip long input. A full-parameter digest used for receipt matching does not change what the alignment engine sees or establish resource authority.

### Receipts

**Use the clean JSONL file** (not the OpenClaw gateway envelope around `console.log`):

```bash
tail -1 /tmp/openclaw/pdm-guard-receipts.jsonl
```

Override path with `PDM_GUARD_RECEIPT_LOG` if needed.

Each line is one compact object:

| Field | Meaning |
|-------|---------|
| `time` | ISO timestamp of the gate decision |
| `status` | `ALIGNED`, `CONFLICT`, `TORSION`, `BLOCKED_*`, `UNPROTECTED_*`, or `UNCERTAIN_CORRELATION` |
| `tool` | Tool name |
| `action` | Short intent (`write path=config.py`, …) |
| `rules` | Active guard rules |
| `executed` | `false` for a blocked call, `true` for a matched completion, `null` when completion cannot be uniquely matched |
| `why` | Short explanation |
| `tool_call_id` | Host call identity, when supplied |
| `params_digest` | Digest of the complete JSON parameters, when available |
| `outcome_status` | `UNCERTAIN` when result correlation is ambiguous |

| Outcome | When logged |
|---------|-------------|
| **Blocked** | `before_tool_call` (`executed: false`) |
| **Permitted / unprotected** | Uniquely matched `after_tool_call` (`executed: true`) |
| **Uncertain** | An unmatched or ambiguous completion affects pending candidates (`executed: null`) |

Completions are matched by the host's tool-call identity, or by tool name plus complete-parameter digest when there is only one pending call of that tool. Concurrent same-tool calls without identities are uncertain even when their parameters differ: later host hooks can rewrite arguments. Contradictory final parameters and completions without enough identifying information are also uncertain instead of using arrival order. A missing completion remains pending in memory; a process restart loses that pending state.

Receipt files are compact, best-effort audit logs. They are not a durable action journal, a complete copy of tool results, a recovery checkpoint, or proof of an external effect. Log write failure does not stop an already permitted tool. The SDK's guarded-effects controller is a separate addition; this plugin has not yet been connected to its durable admission/release/recovery lifecycle.

Example:

```json
{"time":"2026-08-21T08:33:18.388Z","status":"TORSION","tool":"write","action":"write path=config.py","rules":["never create a file named config.py"],"executed":false,"why":"…contradicts Goal Anchor…"}
```

---

## 🛠️ Developer Tools

### Rebuild runtime bundle

After editing `.ts` sources:

```bash
cd pdm-memory/plugins/openclaw
npx esbuild pdm-guard.ts --bundle --platform=node --format=esm \
  --outfile=pdm-guard.js --external:openclaw --external:"openclaw/*"
openclaw daemon restart
```

### Run tests

From repo root:

```bash
# registered-hook tests (no real Python or external services needed; Node >= 24)
node --experimental-strip-types --test plugins/tests/openclaw/registered-hooks.test.mjs

# includes e2e — spawns verify_bridge.py directly, needs a Python >= 3.10
# with pdm-memory installed (set PDM_GUARD_PYTHON if it's not your default python3)
node --experimental-strip-types --test plugins/tests/openclaw/*.test.mjs
```

### Source layout

| File | Role |
|------|------|
| `pdm-guard.ts` | Plugin source (`before_tool_call` + `after_tool_call`) |
| `pdm-guard.js` | Runtime bundle loaded by OpenClaw |
| `verify_bridge.py` | Reads `{intent, goals}` on stdin, calls `pdm_memory.verify()`, writes the report JSON to stdout |
| `rules-store.ts` | Local rules file read/write with dedup |
| `intent.ts` | Short deterministic intent strings (`path=` / `command=` first, tiny `content≈`) |
| `receipt.ts` | Receipt helpers, including explicit uncertain correlation |
| `openclaw.plugin.json` | Manifest (`activation.onCapabilities: ["hook", "tool"]`) |
| `plugins/tests/openclaw/` | Node test suite |

Standalone GAA walkthrough (no OpenClaw):

```bash
pip install pdm-memory
python -m pdm_memory.examples.standalone_guard
```

---

## 📖 API Reference

### `verify_bridge.py` contract

Invoked as `{pythonBin} verify_bridge.py`, intent/goals passed as JSON on **stdin**, report JSON printed to **stdout**. Exit code 0 on success. Nonzero exit, excessive output, process failure, timeout, invalid JSON, unknown status, missing fields, or an inconsistent safety flag blocks by default. Compatibility mode records these as unprotected execution instead.

stdin:

```json
{
  "intent": "exec (command=curl http://localhost:8080/api/health)",
  "goals": ["never hardcode localhost"]
}
```

stdout:

```json
{
  "status": "TORSION",
  "score": 0.0466,
  "conflicting_goals": ["never hardcode localhost"],
  "explanation": "…",
  "is_safe_to_act": false
}
```

This is a thin wrapper — `report = pdm_memory.verify(intent, goals)`, then `report.as_dict()` plus `is_safe_to_act`. No HTTP, no Django, no `companion_api` dependency for the gate itself.

The plugin accepts only `ALIGNED` with `is_safe_to_act: true`, or `CONFLICT`/`TORSION` with `is_safe_to_act: false`; a nonempty explanation and a string-array `conflicting_goals` are required. The underlying `verify()` semantics are unchanged.

### `pdm_guard_rule` tool parameters

| Field | Type | Description |
|-------|------|-------------|
| `action` | `"list"` | Inspect owner-controlled rules; mutation is not exposed to agents |

---

## 📄 License

Same terms as **pdm-memory** — use as-shipped under [Elastic License 2.0 (ELv2)](../../LICENSE) from **Westfield Innovations LLC**.

**Patent Pending** — U.S. App. No. **19/739,419** · **63/953,563** · **63/953,842**.

Built by **Westfield Innovations LLC** · [azus.ai](https://azus.ai)
