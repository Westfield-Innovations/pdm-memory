# PDM Guard — GAA Gate for OpenClaw

[![PDM Memory](https://img.shields.io/pypi/v/pdm-memory)](https://pypi.org/project/pdm-memory/)
[![OpenClaw](https://img.shields.io/badge/OpenClaw-plugin-blue)](https://github.com/openclaw/openclaw)
[![Documentation](https://img.shields.io/badge/Docs-azus.ai%2Fsupport-blue)](https://azus.ai/support)

OpenClaw agents can call tools freely. **PDM Guard** runs `verify()` at OpenClaw's `before_tool_call` hook — scoring every proposed action against plain-English rules before it executes.

Nothing in OpenClaw's own plugin catalog checks a proposed action against a stated rule before it fires. This plugin fills that lane.

- 🛡️ **Goal-Anchor Alignment.** Same gate as `pdm-memory`: **ALIGNED**, **CONFLICT**, or **TORSION**.
- 📝 **Plain-English rules.** Add `never hardcode localhost` — no code, no PDM store required.
- 🔒 **Local rules file.** `~/.openclaw/pdm-guard-rules.json` — your data stays on your machine.
- 🧾 **Receipts, not screenshots.** Every gate decision logs a JSON receipt with `resulting_state` to the OpenClaw gateway log.
- ⚡ **Five minutes.** Install plugin → add one rule → watch a violating action stop before it runs.

📖 **GAA background:** [Guarded Agents — GAA & Torsion](https://azus.ai/support) · Spike report: [`GO-NO-GO.md`](GO-NO-GO.md)

**Tested runtime:** OpenClaw **2026.7.1-2** (see spike report for block/permit receipts).

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
4. **Blocks** the tool when `is_safe_to_act` is `False`, or **permits** it through.

Statuses (same as standalone GAA):

| Status | Meaning | Tool runs? |
|--------|---------|------------|
| **ALIGNED** | Intent agrees with the rules | Yes |
| **CONFLICT** | Soft mismatch / fail-closed | No |
| **TORSION** | Hard contradiction | No |

Rule management (`pdm_guard_rule`, `/pdm-guard`) always bypasses the gate — otherwise you could lock yourself out.

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

### 2. Add a rule

```text
/pdm-guard add never hardcode localhost
```

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
grep '\[PDM GUARD\] RECEIPT:' /tmp/openclaw/openclaw-$(date +%Y-%m-%d).log | tail -1
```

A blocked receipt includes `"tool_executed": false` and `"resulting_state": { "executed": false, "side_effects": "none", … }`.

---

## ⚙️ Configuration

### Plugin config (`openclaw.json` → `plugins.entries.pdm-guard.config`)

| Key | Default | Description |
|-----|---------|-------------|
| `pythonBin` | `python3` | Interpreter used to run `verify_bridge.py` — must have `pdm-memory` importable |
| `rulesFile` | `~/.openclaw/pdm-guard-rules.json` | Local rules JSON path |
| `timeoutMs` | `8000` | Time budget for the `verify()` subprocess before it's killed |
| `goals` | `[]` | Legacy fallback rules when the rules file is empty |

### Environment overrides

| Variable | Effect |
|----------|--------|
| `PDM_GUARD_PYTHON` | Overrides `pythonBin` |
| `PDM_GUARD_RULES_FILE` | Overrides `rulesFile` |
| `PDM_GUARD_GOALS` | Pipe-separated fallback rules (`rule one\|rule two`) |

Plugin config is read from `api.pluginConfig` at `register()` time — typed hooks do not populate `ctx.pluginConfig`.

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

### Slash commands (recommended — bypasses LLM)

```text
/pdm-guard list
/pdm-guard add never hardcode localhost
/pdm-guard remove never hardcode localhost
```

Aliases: `ls`/`show`, `set`/`create`, `delete`/`rm`/`drop`.

### Agent tool

If the model has `pdm_guard_rule` allowlisted, ask in plain English:

```json
{ "action": "add", "rule": "never hardcode localhost" }
{ "action": "remove", "rule": "never hardcode localhost" }
{ "action": "list" }
```

Keep the normal coding profile in `openclaw.json`. Do **not** set `tools.allow` to only `pdm_guard_rule` — that replaces the whole tool catalog and removes `exec` / other coding tools.

---

## 🔍 Behavior & Receipts

| Condition | Gate behavior |
|-----------|---------------|
| Rules file has entries | Verify every tool call (except rule management) |
| No rules anywhere | Fail-open — tool proceeds |
| `verify()` process fails/times out/bad output | Fail-open — tool proceeds (`gate_status: SKIPPED_VERIFY_ERROR`) |
| `verify()` → not safe | Block with reason shown to user |

Intent strings prefer rich tool payload fields (`command`, `input`, `content`, `text`, `patch`, `script`) so `exec` / write calls carry enough context for `verify()`.

### Receipts

Source: OpenClaw gateway log at `/tmp/openclaw/openclaw-YYYY-MM-DD.log`

Format: `[PDM GUARD] RECEIPT: {…}`

Each receipt includes:

- `proposed_action`, `gate_status`, `governing_rules`
- `tool_executed` — whether the host actually ran the tool
- `resulting_state` — side-effect proof
- `pdm_version`, `openclaw_version`, `timestamp`

| Outcome | When logged | `resulting_state` |
|---------|-------------|-------------------|
| **Blocked** | `before_tool_call` | `executed: false`, `side_effects: "none"` |
| **Permitted / skipped** | `after_tool_call` | Real tool outcome (stdout, stderr, exit code when available) |

Example block receipt:

```json
{
  "gate_status": "TORSION",
  "tool_executed": false,
  "resulting_state": {
    "executed": false,
    "side_effects": "none",
    "tool_name": "exec",
    "detail": "tool call stopped at before_tool_call; host did not execute"
  }
}
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
# unit tests (no Python needed)
node --test plugins/tests/openclaw/receipt.test.mjs plugins/tests/openclaw/rules-store.test.mjs

# includes e2e — spawns verify_bridge.py directly, needs a Python >= 3.10
# with pdm-memory installed (set PDM_GUARD_PYTHON if it's not your default python3)
node --test plugins/tests/openclaw/*.test.mjs
```

### Source layout

| File | Role |
|------|------|
| `pdm-guard.ts` | Plugin source (`before_tool_call` + `after_tool_call`) |
| `pdm-guard.js` | Runtime bundle loaded by OpenClaw |
| `verify_bridge.py` | Reads `{intent, goals}` on stdin, calls `pdm_memory.verify()`, writes the report JSON to stdout |
| `rules-store.ts` | Local rules file read/write with dedup |
| `receipt.ts` | Receipt helpers (`resulting_state`, block/complete builders) |
| `openclaw.plugin.json` | Manifest (`activation.onCapabilities: ["hook", "tool"]`) |
| `GO-NO-GO.md` | Formal spike report (GO decision, live receipts) |
| `plugins/tests/openclaw/` | Node test suite |

Standalone GAA walkthrough (no OpenClaw):

```bash
pip install pdm-memory
python -m pdm_memory.examples.standalone_guard
```

---

## 📖 API Reference

### `verify_bridge.py` contract

Invoked as `{pythonBin} verify_bridge.py`, intent/goals passed as JSON on **stdin**, report JSON printed to **stdout**. Exit code 0 on success; nonzero (with a traceback on stderr) triggers fail-open.

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

### `pdm_guard_rule` tool parameters

| Field | Type | Description |
|-------|------|-------------|
| `action` | `"add" \| "remove" \| "list"` | Rule operation |
| `rule` | `string` | Plain-English rule text (required for add/remove) |

---

## 📄 License

Same terms as **pdm-memory** — use as-shipped under [Elastic License 2.0 (ELv2)](../../LICENSE) from **Westfield Innovations LLC**.

**Patent Pending** — U.S. App. No. **19/739,419** · **63/953,563** · **63/953,842**.

Built by **Westfield Innovations LLC** · [azus.ai](https://azus.ai)
