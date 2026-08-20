// pdm-guard.ts
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

// receipt.ts
var OPENCLAW_VERSION = "2026.7.1-2";
var PDM_VERSION = "0.2.4";
var MAX_TEXT = 2e3;
function clip(text, maxLen = MAX_TEXT) {
  return text.length <= maxLen ? text : `${text.slice(0, maxLen - 1)}\u2026`;
}
function asRecord(value) {
  if (value == null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value;
}
function readNumber(record, ...keys) {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
  }
  return null;
}
function readString(record, ...keys) {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string") {
      return value;
    }
  }
  return null;
}
function summarizeToolOutcome(toolName, result, error) {
  if (error) {
    return {
      state: {
        executed: true,
        side_effects: "observed",
        tool_name: toolName,
        exit_code: null,
        stdout: null,
        stderr: null,
        error: clip(error),
        detail: "tool reported error after gate allowed execution"
      },
      toolResult: null
    };
  }
  const record = asRecord(result);
  if (record) {
    const exitCode = readNumber(record, "exitCode", "exit_code", "code");
    const stdout = readString(record, "stdout", "output");
    const stderr = readString(record, "stderr");
    const nestedError = readString(record, "error", "message");
    if (exitCode != null || stdout != null || stderr != null) {
      return {
        state: {
          executed: true,
          side_effects: "observed",
          tool_name: toolName,
          exit_code: exitCode,
          stdout: stdout != null ? clip(stdout) : null,
          stderr: stderr != null ? clip(stderr) : null,
          error: nestedError != null ? clip(nestedError) : null,
          detail: "exec side-effect observed"
        },
        toolResult: result
      };
    }
  }
  if (result == null) {
    return {
      state: {
        executed: true,
        side_effects: "observed",
        tool_name: toolName,
        exit_code: null,
        stdout: null,
        stderr: null,
        error: null,
        detail: "tool completed with empty result"
      },
      toolResult: null
    };
  }
  const serialized = typeof result === "string" ? clip(result) : clip(JSON.stringify(result));
  return {
    state: {
      executed: true,
      side_effects: "observed",
      tool_name: toolName,
      exit_code: null,
      stdout: null,
      stderr: null,
      error: null,
      detail: serialized
    },
    toolResult: result
  };
}
function blockedResultingState(toolName) {
  return {
    executed: false,
    side_effects: "none",
    tool_name: toolName,
    exit_code: null,
    stdout: null,
    stderr: null,
    error: null,
    detail: "tool call stopped at before_tool_call; host did not execute"
  };
}
function buildBlockedReceipt(pending) {
  return {
    timestamp: pending.timestamp,
    proposed_action: pending.proposed_action,
    gate_status: pending.gate_status,
    governing_rules: pending.governing_rules,
    rules_source: pending.rules_source,
    tool_executed: false,
    explanation: pending.explanation,
    pdm_version: pending.pdm_version,
    openclaw_version: pending.openclaw_version,
    resulting_state: blockedResultingState(pending.tool_name),
    tool_result: null,
    duration_ms: null
  };
}
function buildCompletedReceipt(pending, event) {
  const { state, toolResult } = summarizeToolOutcome(
    event.toolName,
    event.result,
    event.error
  );
  return {
    timestamp: pending.timestamp,
    proposed_action: pending.proposed_action,
    gate_status: pending.gate_status,
    governing_rules: pending.governing_rules,
    rules_source: pending.rules_source,
    tool_executed: pending.tool_executed,
    explanation: pending.explanation,
    pdm_version: pending.pdm_version,
    openclaw_version: pending.openclaw_version,
    resulting_state: state,
    tool_result: toolResult,
    duration_ms: event.durationMs ?? null
  };
}
function logReceipt(receipt) {
  console.log(`[PDM GUARD] RECEIPT: ${JSON.stringify(receipt)}`);
}

// rules-store.ts
import { createHash } from "node:crypto";
import { mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
var RULES_FILE_VERSION = 1;
var GUARD_RULE_MAX_LEN = 500;
var DEFAULT_RULES_FILE = join(homedir(), ".openclaw", "pdm-guard-rules.json");
var cachedPath = null;
var cachedMtimeMs = -1;
var cachedFile = null;
function expandHome(path) {
  if (path === "~") return homedir();
  if (path.startsWith("~/")) return join(homedir(), path.slice(2));
  return path;
}
function normalizeGuardRule(rule) {
  if (rule == null) return null;
  const text = String(rule).trim().replace(/\s+/g, " ");
  if (!text) return null;
  return text.length > GUARD_RULE_MAX_LEN ? text.slice(0, GUARD_RULE_MAX_LEN) : text;
}
function emptyRulesFile() {
  return { version: RULES_FILE_VERSION, rules: [] };
}
function ruleIdFor(text) {
  return createHash("sha256").update(text).digest("hex").slice(0, 12);
}
function findRuleIndex(rules, normalized) {
  return rules.findIndex((entry) => normalizeGuardRule(entry.text) === normalized);
}
async function ensureParentDir(filePath) {
  await mkdir(dirname(filePath), { recursive: true });
}
async function writeRulesFileAtomic(filePath, file) {
  await ensureParentDir(filePath);
  const tmpPath = `${filePath}.tmp`;
  const payload = `${JSON.stringify(file, null, 2)}
`;
  await writeFile(tmpPath, payload, "utf8");
  await rename(tmpPath, filePath);
  invalidateRulesCache();
}
function invalidateRulesCache() {
  cachedPath = null;
  cachedMtimeMs = -1;
  cachedFile = null;
}
async function readRulesFile(filePath) {
  const resolved = expandHome(filePath);
  try {
    const info = await stat(resolved);
    if (cachedPath === resolved && cachedFile && cachedMtimeMs === info.mtimeMs) {
      return cachedFile;
    }
    const raw = await readFile(resolved, "utf8");
    const parsed = JSON.parse(raw);
    const file = {
      version: typeof parsed.version === "number" ? parsed.version : RULES_FILE_VERSION,
      rules: Array.isArray(parsed.rules) ? parsed.rules.filter((entry) => {
        return !!entry && typeof entry === "object" && typeof entry.text === "string" && entry.text.trim().length > 0;
      }).map((entry) => ({
        id: typeof entry.id === "string" && entry.id ? entry.id : ruleIdFor(entry.text),
        text: normalizeGuardRule(entry.text) ?? entry.text.trim(),
        created_at: typeof entry.created_at === "string" && entry.created_at ? entry.created_at : (/* @__PURE__ */ new Date()).toISOString()
      })) : []
    };
    cachedPath = resolved;
    cachedMtimeMs = info.mtimeMs;
    cachedFile = file;
    return file;
  } catch (err) {
    const code = err.code;
    if (code === "ENOENT") {
      const empty = emptyRulesFile();
      cachedPath = resolved;
      cachedMtimeMs = 0;
      cachedFile = empty;
      return empty;
    }
    throw err;
  }
}
function goalTextsFromFile(file) {
  const seen = /* @__PURE__ */ new Set();
  const goals = [];
  for (const entry of file.rules) {
    const normalized = normalizeGuardRule(entry.text);
    if (!normalized || seen.has(normalized)) continue;
    seen.add(normalized);
    goals.push(normalized);
  }
  return goals;
}
async function loadGoalTexts(filePath) {
  const file = await readRulesFile(filePath);
  return goalTextsFromFile(file);
}
async function addGuardRule(filePath, rule) {
  const normalized = normalizeGuardRule(rule);
  if (!normalized) {
    throw new Error("rule must be a non-empty string");
  }
  const file = await readRulesFile(filePath);
  const existingIndex = findRuleIndex(file.rules, normalized);
  if (existingIndex >= 0) {
    const entry2 = file.rules[existingIndex];
    return { entry: entry2, created: false, deduped: true, file };
  }
  const entry = {
    id: ruleIdFor(normalized),
    text: normalized,
    created_at: (/* @__PURE__ */ new Date()).toISOString()
  };
  file.rules.push(entry);
  await writeRulesFileAtomic(expandHome(filePath), file);
  const saved = await readRulesFile(filePath);
  return {
    entry,
    created: true,
    deduped: false,
    file: saved
  };
}
async function removeGuardRule(filePath, ruleOrId) {
  const needle = normalizeGuardRule(ruleOrId) ?? ruleOrId.trim();
  if (!needle) {
    throw new Error("rule or id is required");
  }
  const file = await readRulesFile(filePath);
  const index = file.rules.findIndex((entry) => {
    if (entry.id === needle) return true;
    return normalizeGuardRule(entry.text) === needle;
  });
  if (index < 0) {
    return { removed: null, file };
  }
  const [removed] = file.rules.splice(index, 1);
  await writeRulesFileAtomic(expandHome(filePath), file);
  const saved = await readRulesFile(filePath);
  return { removed, file: saved };
}
async function listGuardRules(filePath) {
  return readRulesFile(filePath);
}

// pdm-guard.ts
var DEFAULT_VERIFY_URL = "http://localhost:8000/api/v1/pdm/gaa/verify/";
var GUARD_RULE_TOOL = "pdm_guard_rule";
function buildIntentText(toolName, params) {
  const summarize = (value, maxLen = 500) => {
    try {
      if (typeof value === "string") return value.slice(0, maxLen);
      return JSON.stringify(value).slice(0, maxLen);
    } catch {
      return String(value).slice(0, maxLen);
    }
  };
  const richFields = ["command", "input", "content", "text", "patch", "script"];
  for (const field of richFields) {
    const value = params[field];
    if (typeof value === "string" && value.trim()) {
      return `${toolName} (${field}=${summarize(value, 1200)})`;
    }
  }
  const paramSummary = Object.entries(params).slice(0, 8).map(([k, v]) => `${k}=${summarize(v, 240)}`).join(", ");
  return paramSummary ? `${toolName} (${paramSummary})` : toolName;
}
function resolveRulesFilePath(cfg) {
  const fromCfg = cfg.rulesFile?.trim();
  if (fromCfg) return expandHome(fromCfg);
  const fromEnv = process.env.PDM_GUARD_RULES_FILE?.trim();
  if (fromEnv) return expandHome(fromEnv);
  return DEFAULT_RULES_FILE;
}
function resolveConfigGoals(cfg) {
  if (cfg.goals?.length) return cfg.goals;
  const fromEnv = process.env.PDM_GUARD_GOALS;
  if (fromEnv) {
    return fromEnv.split("|").map((g) => g.trim()).filter(Boolean);
  }
  return [];
}
async function resolveGoals(cfg) {
  const rulesFile = resolveRulesFilePath(cfg);
  const fromFile = await loadGoalTexts(rulesFile);
  if (fromFile.length) {
    return { goals: fromFile, source: rulesFile };
  }
  const fromConfig = resolveConfigGoals(cfg);
  if (fromConfig.length) {
    return { goals: fromConfig, source: "openclaw.json" };
  }
  return { goals: [], source: "none" };
}
function resolvePluginConfig(registeredCfg, ctx) {
  return ctx.pluginConfig ?? registeredCfg;
}
async function callVerify(verifyUrl, intent, goals, timeoutMs) {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const resp = await fetch(verifyUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ intent, goals }),
      signal: controller.signal
    });
    clearTimeout(timer);
    if (!resp.ok) {
      console.warn(`[PDM GUARD] verify endpoint returned ${resp.status} - fail-open`);
      return null;
    }
    return await resp.json();
  } catch (err) {
    console.warn(`[PDM GUARD] verify call failed (${err}) - fail-open`);
    return null;
  }
}
function formatRulesText(filePath, file) {
  const goals = goalTextsFromFile(file);
  if (!goals.length) {
    return `No guard rules in ${filePath}.
Add one: /pdm-guard add never hardcode localhost`;
  }
  const lines = goals.map((text, i) => `${i + 1}. ${text}`);
  return `Guard rules (${filePath}):
${lines.join("\n")}`;
}
function guardRuleToolResult(details) {
  return {
    content: [{ type: "text", text: JSON.stringify(details, null, 2) }],
    details
  };
}
async function handleRuleAction(rulesFilePath, action, rule) {
  if (action === "list") {
    const file = await listGuardRules(rulesFilePath);
    const details = {
      action: "list",
      rules_file: rulesFilePath,
      rules: file.rules,
      goal_texts: goalTextsFromFile(file)
    };
    return { details, text: formatRulesText(rulesFilePath, file) };
  }
  if (!rule?.trim()) {
    throw new Error(`rule is required for action=${action}`);
  }
  if (action === "add") {
    const result = await addGuardRule(rulesFilePath, rule);
    const details = {
      action: "add",
      rules_file: rulesFilePath,
      created: result.created,
      deduped: result.deduped,
      rule: result.entry,
      rules: result.file.rules,
      goal_texts: goalTextsFromFile(result.file)
    };
    const text = result.deduped ? `Already present (deduped): "${result.entry.text}"

${formatRulesText(rulesFilePath, result.file)}` : `Added: "${result.entry.text}"

${formatRulesText(rulesFilePath, result.file)}`;
    return { details, text };
  }
  if (action === "remove") {
    const result = await removeGuardRule(rulesFilePath, rule);
    const details = {
      action: "remove",
      rules_file: rulesFilePath,
      removed: result.removed,
      deleted: result.removed != null,
      rules: result.file.rules,
      goal_texts: goalTextsFromFile(result.file)
    };
    const text = result.removed ? `Removed: "${result.removed.text}"

${formatRulesText(rulesFilePath, result.file)}` : `No matching rule for "${rule.trim()}".

${formatRulesText(rulesFilePath, result.file)}`;
    return { details, text };
  }
  throw new Error(`unsupported action: ${String(action)}`);
}
function parseGuardCommandArgs(args) {
  const raw = (args ?? "").trim();
  if (!raw) return { action: "list" };
  const [verb, ...rest] = raw.split(/\s+/);
  const action = verb.toLowerCase();
  const rule = rest.join(" ").trim();
  if (action === "list" || action === "ls" || action === "show") {
    return { action: "list" };
  }
  if (action === "add" || action === "set" || action === "create") {
    return { action: "add", rule };
  }
  if (action === "remove" || action === "delete" || action === "rm" || action === "drop") {
    return { action: "remove", rule };
  }
  throw new Error(
    `Usage:
/pdm-guard list
/pdm-guard add <rule>
/pdm-guard remove <rule>`
  );
}
var pdm_guard_default = definePluginEntry({
  id: "pdm-guard",
  name: "PDM Guard",
  description: "Goal-Anchor Alignment gate. Checks every tool call against guard rules via pdm-memory verify().",
  register(api) {
    const registeredCfg = api.pluginConfig ?? {};
    const rulesFilePath = resolveRulesFilePath(registeredCfg);
    const pluginApi = api;
    const pendingReceipts = [];
    const registerTool = pluginApi.registerTool;
    if (registerTool) {
      registerTool(
        {
          name: GUARD_RULE_TOOL,
          description: "REQUIRED for listing, adding, or deleting PDM guard rules. When the user says show/list/add/set/remove/delete guard rules, call this tool. Do not invent rules from memory \u2014 always call action=list first. Rules are stored in ~/.openclaw/pdm-guard-rules.json and enforce before_tool_call.",
          parameters: {
            type: "object",
            additionalProperties: false,
            properties: {
              action: {
                type: "string",
                enum: ["add", "remove", "list"],
                description: "add a rule, remove by text/id, or list all rules"
              },
              rule: {
                type: "string",
                description: "Plain-English guard rule (required for add/remove)"
              }
            },
            required: ["action"]
          },
          outputSchema: {
            type: "object",
            additionalProperties: true,
            properties: {
              action: { type: "string" },
              rules: { type: "array" },
              rules_file: { type: "string" }
            },
            required: ["action", "rules_file"]
          },
          async execute(_id, params) {
            const { details } = await handleRuleAction(
              rulesFilePath,
              params.action,
              params.rule
            );
            return guardRuleToolResult(details);
          }
        },
        { optional: false }
      );
    } else {
      console.warn("[PDM GUARD] registerTool unavailable \u2014 pdm_guard_rule tool not registered");
    }
    const registerCommand = pluginApi.registerCommand;
    if (registerCommand) {
      registerCommand({
        name: "pdm-guard",
        description: "List/add/remove PDM guard rules (bypasses LLM)",
        acceptsArgs: true,
        requireAuth: true,
        async handler(ctx) {
          try {
            const parsed = parseGuardCommandArgs(ctx.args);
            const { text } = await handleRuleAction(
              rulesFilePath,
              parsed.action,
              parsed.rule
            );
            return { text };
          } catch (err) {
            return { text: err instanceof Error ? err.message : String(err) };
          }
        }
      });
    } else {
      console.warn("[PDM GUARD] registerCommand unavailable \u2014 /pdm-guard not registered");
    }
    console.log(
      `[PDM GUARD] registered (rulesFile=${rulesFilePath}, tool=${GUARD_RULE_TOOL}, command=/pdm-guard)`
    );
    function enqueuePendingReceipt(pending) {
      pendingReceipts.push(pending);
    }
    function takePendingReceipt() {
      return pendingReceipts.shift();
    }
    api.on(
      "before_tool_call",
      async (event, ctx) => {
        const toolEvent = event;
        if (toolEvent.toolName === GUARD_RULE_TOOL) {
          return;
        }
        const pluginCfg = resolvePluginConfig(registeredCfg, ctx);
        const { goals, source } = await resolveGoals(pluginCfg);
        const verifyUrl = pluginCfg.verifyUrl ?? process.env.PDM_GUARD_URL ?? DEFAULT_VERIFY_URL;
        const timeoutMs = pluginCfg.timeoutMs ?? 8e3;
        const params = toolEvent.params ?? {};
        const intent = buildIntentText(toolEvent.toolName, params);
        const timestamp = (/* @__PURE__ */ new Date()).toISOString();
        if (!goals.length || !verifyUrl) {
          enqueuePendingReceipt({
            timestamp,
            proposed_action: intent,
            gate_status: "SKIPPED_NO_RULES",
            governing_rules: [],
            rules_source: source,
            explanation: "No guard rules configured - fail-open.",
            tool_executed: true,
            pdm_version: PDM_VERSION,
            openclaw_version: OPENCLAW_VERSION,
            tool_name: toolEvent.toolName
          });
          return;
        }
        const result = await callVerify(verifyUrl, intent, goals, timeoutMs);
        if (!result) {
          enqueuePendingReceipt({
            timestamp,
            proposed_action: intent,
            gate_status: "SKIPPED_SIDECAR_ERROR",
            governing_rules: goals,
            rules_source: source,
            explanation: "verify() unreachable - fail-open.",
            tool_executed: true,
            pdm_version: PDM_VERSION,
            openclaw_version: OPENCLAW_VERSION,
            tool_name: toolEvent.toolName
          });
          return;
        }
        const allowed = result.is_safe_to_act === true;
        if (!allowed) {
          logReceipt(
            buildBlockedReceipt({
              timestamp,
              proposed_action: intent,
              gate_status: result.status,
              governing_rules: goals,
              rules_source: source,
              explanation: result.explanation,
              tool_executed: false,
              pdm_version: PDM_VERSION,
              openclaw_version: OPENCLAW_VERSION,
              tool_name: toolEvent.toolName
            })
          );
          const blockReason = `Action blocked by PDM guard (${result.status}): ${result.explanation}` + (result.conflicting_goals.length ? ` Rule: "${result.conflicting_goals[0]}"` : "");
          return { block: true, blockReason };
        }
        enqueuePendingReceipt({
          timestamp,
          proposed_action: intent,
          gate_status: result.status,
          governing_rules: goals,
          rules_source: source,
          explanation: result.explanation,
          tool_executed: true,
          pdm_version: PDM_VERSION,
          openclaw_version: OPENCLAW_VERSION,
          tool_name: toolEvent.toolName
        });
      },
      { priority: 90 }
    );
    api.on(
      "after_tool_call",
      async (event) => {
        const toolEvent = event;
        if (toolEvent.toolName === GUARD_RULE_TOOL) {
          return;
        }
        const pending = takePendingReceipt();
        if (!pending) {
          console.warn(
            `[PDM GUARD] after_tool_call without pending receipt for ${toolEvent.toolName}`
          );
          return;
        }
        logReceipt(buildCompletedReceipt(pending, toolEvent));
      },
      { priority: 90 }
    );
  }
});
export {
  pdm_guard_default as default
};
