// plugins/openclaw/pdm-guard.ts
import { spawn } from "node:child_process";
import { createHash as createHash2 } from "node:crypto";
import { dirname as dirname3, join as join3 } from "node:path";
import { fileURLToPath } from "node:url";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

// plugins/openclaw/intent.ts
var PATH_KEYS = ["path", "file", "filename", "file_path", "filepath", "target"];
var COMMAND_KEYS = ["command", "cmd"];
var CONTENT_KEYS = ["content", "text", "patch", "script", "input"];
var CONTENT_PREVIEW_MAX = 120;
var COMMAND_MAX = 800;
var INTENT_MAX = 1e3;
function asNonEmptyString(value) {
  if (typeof value !== "string") return "";
  const trimmed = value.trim();
  return trimmed ? trimmed : "";
}
function basename(path) {
  const normalized = path.replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  return parts[parts.length - 1] || path;
}
function firstString(params, keys) {
  for (const key of keys) {
    const value = asNonEmptyString(params[key]);
    if (value) return value;
  }
  return "";
}
function clip(value, maxLen) {
  if (value.length <= maxLen) return value;
  return `${value.slice(0, Math.max(0, maxLen - 1))}\u2026`;
}
function buildIntentText(toolName, params = {}) {
  const tool = asNonEmptyString(toolName) || "unknown_tool";
  const parts = [tool];
  const path = firstString(params, PATH_KEYS);
  if (path) {
    parts.push(`path=${basename(path)}`);
  }
  const command = firstString(params, COMMAND_KEYS);
  if (command) {
    parts.push(`command=${clip(command, COMMAND_MAX)}`);
  } else {
    const content = firstString(params, CONTENT_KEYS);
    if (content) {
      parts.push(`content\u2248${clip(content, CONTENT_PREVIEW_MAX)}`);
    }
  }
  if (parts.length === 1) {
    const leftovers = Object.entries(params).filter(([, v]) => v !== void 0 && v !== null && v !== "").slice(0, 6).map(([k, v]) => {
      try {
        const raw = typeof v === "string" ? v : JSON.stringify(v);
        return `${k}=${clip(raw, 80)}`;
      } catch {
        return `${k}=${clip(String(v), 80)}`;
      }
    });
    if (leftovers.length) {
      parts.push(...leftovers);
    }
  }
  return clip(parts.join(" "), INTENT_MAX);
}

// plugins/openclaw/receipt.ts
import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
var OPENCLAW_VERSION = "2026.7.1-2";
var PDM_VERSION = (() => {
  try {
    return readFileSync(new URL("../../pyproject.toml", import.meta.url), "utf8").match(/^version\s*=\s*"([^"]+)"/m)?.[1] ?? "unknown";
  } catch {
    return "unknown";
  }
})();
var DEFAULT_RECEIPT_LOG = join("/tmp", "openclaw", "pdm-guard-receipts.jsonl");
var FALLBACK_RECEIPT_LOG = join(
  homedir(),
  ".openclaw",
  "pdm-guard-receipts.jsonl"
);
var MAX_TEXT = 2e3;
var LOG_ACTION_MAX = 72;
var LOG_WHY_MAX = 100;
function clip2(text, maxLen = MAX_TEXT) {
  return text.length <= maxLen ? text : `${text.slice(0, maxLen - 1)}\u2026`;
}
function compactAction(proposedAction) {
  const pathMatch = proposedAction.match(/^(\S+)\s+path=(\S+)/);
  if (pathMatch) {
    return clip2(`${pathMatch[1]} path=${pathMatch[2]}`, LOG_ACTION_MAX);
  }
  const fileMatch = proposedAction.match(/^(\S+)\s+file=(\S+)/);
  if (fileMatch) {
    return clip2(`${fileMatch[1]} path=${fileMatch[2]}`, LOG_ACTION_MAX);
  }
  const cmdMatch = proposedAction.match(/^(\S+)\s+command=(.+)$/s);
  if (cmdMatch) {
    return clip2(`${cmdMatch[1]} command=${cmdMatch[2].trim()}`, LOG_ACTION_MAX);
  }
  return clip2(proposedAction, LOG_ACTION_MAX);
}
function toReceiptLog(receipt) {
  return {
    time: receipt.timestamp,
    status: receipt.gate_status,
    tool: receipt.resulting_state.tool_name,
    action: compactAction(receipt.proposed_action),
    rules: receipt.governing_rules,
    executed: receipt.tool_executed,
    why: clip2(receipt.explanation, LOG_WHY_MAX),
    ...receipt.tool_call_id ? { tool_call_id: receipt.tool_call_id } : {},
    ...receipt.params_digest ? { params_digest: receipt.params_digest } : {},
    ...receipt.outcome_status ? { outcome_status: receipt.outcome_status } : {}
  };
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
        error: clip2(error),
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
          stdout: stdout != null ? clip2(stdout) : null,
          stderr: stderr != null ? clip2(stderr) : null,
          error: nestedError != null ? clip2(nestedError) : null,
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
  const serialized = typeof result === "string" ? clip2(result) : clip2(JSON.stringify(result));
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
    duration_ms: null,
    tool_call_id: pending.tool_call_id,
    params_digest: pending.params_digest
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
    duration_ms: event.durationMs ?? null,
    tool_call_id: pending.tool_call_id,
    params_digest: pending.params_digest
  };
}
function buildUncertainReceipt(pending, explanation) {
  return {
    ...buildBlockedReceipt(pending),
    gate_status: "UNCERTAIN_CORRELATION",
    explanation,
    tool_executed: null,
    outcome_status: "UNCERTAIN",
    resulting_state: {
      executed: null,
      side_effects: "unknown",
      tool_name: pending.tool_name,
      detail: explanation
    }
  };
}
function resolveReceiptLogPath() {
  const fromEnv = process.env.PDM_GUARD_RECEIPT_LOG?.trim();
  if (fromEnv) {
    if (fromEnv.startsWith("~/")) {
      return join(homedir(), fromEnv.slice(2));
    }
    return fromEnv;
  }
  return DEFAULT_RECEIPT_LOG;
}
function appendReceiptLine(file, line) {
  mkdirSync(dirname(file), { recursive: true });
  appendFileSync(file, line, "utf8");
}
function logReceipt(receipt) {
  const row = toReceiptLog(receipt);
  const line = `${JSON.stringify(row)}
`;
  const primary = resolveReceiptLogPath();
  try {
    appendReceiptLine(primary, line);
  } catch (err) {
    console.log(`[PDM GUARD] receipt log write failed (${primary}): ${err}`);
    if (primary !== FALLBACK_RECEIPT_LOG) {
      try {
        appendReceiptLine(FALLBACK_RECEIPT_LOG, line);
        console.log(`[PDM GUARD] receipt log fell back to ${FALLBACK_RECEIPT_LOG}`);
      } catch (err2) {
        console.log(`[PDM GUARD] receipt log fallback failed (${FALLBACK_RECEIPT_LOG}): ${err2}`);
      }
    }
  }
  console.log(
    `[PDM GUARD] ${row.status} ${row.tool} executed=${row.executed} \xB7 ${row.action}`
  );
}

// plugins/openclaw/rules-store.ts
import { createHash } from "node:crypto";
import { mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import { homedir as homedir2 } from "node:os";
import { dirname as dirname2, join as join2 } from "node:path";
var RULES_FILE_VERSION = 1;
var GUARD_RULE_MAX_LEN = 500;
var DEFAULT_RULES_FILE = join2(homedir2(), ".openclaw", "pdm-guard-rules.json");
var cachedPath = null;
var cachedMtimeMs = -1;
var cachedFile = null;
function expandHome(path) {
  if (path === "~") return homedir2();
  if (path.startsWith("~/")) return join2(homedir2(), path.slice(2));
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
  await mkdir(dirname2(filePath), { recursive: true });
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

// plugins/openclaw/pdm-guard.ts
var DEFAULT_PYTHON_BIN = "python3";
var GUARD_RULE_TOOL = "pdm_guard_rule";
var PLUGIN_DIR = dirname3(fileURLToPath(import.meta.url));
var VERIFY_BRIDGE_SCRIPT = join3(PLUGIN_DIR, "verify_bridge.py");
var VERIFY_OUTPUT_LIMIT = 2 * 1024 * 1024;
function checkedVerifyResponse(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value;
  if (typeof row.status !== "string" || !["ALIGNED", "CONFLICT", "TORSION"].includes(row.status)) return null;
  if (typeof row.is_safe_to_act !== "boolean") return null;
  if (row.is_safe_to_act !== (row.status === "ALIGNED")) return null;
  if (typeof row.explanation !== "string" || !row.explanation.trim()) return null;
  if (!Array.isArray(row.conflicting_goals) || !row.conflicting_goals.every((goal) => typeof goal === "string")) return null;
  return row;
}
function parametersDigest(value) {
  function ordered(v) {
    if (v === null || typeof v === "string" || typeof v === "boolean") return v;
    if (typeof v === "number" && Number.isFinite(v)) return v;
    if (Array.isArray(v)) return v.map(ordered);
    if (v && typeof v === "object") {
      return Object.fromEntries(Object.keys(v).sort().map((key) => [
        key,
        ordered(v[key])
      ]));
    }
    throw new Error("parameters are not JSON values");
  }
  try {
    return createHash2("sha256").update(JSON.stringify(ordered(value))).digest("hex");
  } catch {
    return null;
  }
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
async function callVerifyFunction(pythonBin, scriptPath, intent, goals, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    let child;
    try {
      child = spawn(pythonBin, [scriptPath], { stdio: ["pipe", "pipe", "pipe"] });
    } catch (err) {
      console.warn(`[PDM GUARD] failed to spawn ${pythonBin} (${err})`);
      finish(null);
      return;
    }
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      console.warn(`[PDM GUARD] verify() call timed out after ${timeoutMs}ms`);
      finish(null);
    }, timeoutMs);
    let stdout = "";
    let stderr = "";
    child.stdout?.on("data", (chunk) => {
      stdout += String(chunk);
      if (stdout.length > VERIFY_OUTPUT_LIMIT) {
        child.kill("SIGKILL");
        clearTimeout(timer);
        finish(null);
      }
    });
    child.stderr?.on("data", (chunk) => {
      stderr += String(chunk);
      if (stderr.length > VERIFY_OUTPUT_LIMIT) {
        child.kill("SIGKILL");
        clearTimeout(timer);
        finish(null);
      }
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      console.warn(`[PDM GUARD] verify() call failed (${err})`);
      finish(null);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        console.warn(
          `[PDM GUARD] verify() exited with code ${code}. ${stderr.trim().slice(-500)}`
        );
        finish(null);
        return;
      }
      try {
        finish(checkedVerifyResponse(JSON.parse(stdout)));
      } catch (err) {
        console.warn(`[PDM GUARD] verify() returned invalid JSON (${err})`);
        finish(null);
      }
    });
    child.stdin?.on("error", () => {
      clearTimeout(timer);
      child.kill("SIGKILL");
      finish(null);
    });
    child.stdin?.write(JSON.stringify({ intent, goals }));
    child.stdin?.end();
  });
}
function formatRulesText(_filePath, file) {
  const goals = goalTextsFromFile(file);
  if (!goals.length) {
    return "No guard rules.\nAdd one: /pdm-guard add <rule>";
  }
  const lines = goals.map((text, i) => `${i + 1}. ${text}`);
  return `Guard rules:
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
          description: "List the owner-controlled PDM guard rules. Rule changes require the authenticated owner /pdm-guard command; the agent cannot add or remove its own restrictions.",
          parameters: {
            type: "object",
            additionalProperties: false,
            properties: {
              action: {
                type: "string",
                enum: ["list"],
                description: "List the owner-controlled rules"
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
            if (params.action !== "list") {
              throw new Error("Guard rules are owner-controlled. Use the authenticated /pdm-guard command.");
            }
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
            if (parsed.action !== "list") {
              const configuredOwner = typeof ctx.senderId === "string" && Array.isArray(registeredCfg.ownerSenderIds) && registeredCfg.ownerSenderIds.includes(ctx.senderId);
              if (ctx.isAuthorizedSender !== true || ctx.senderIsOwner !== true && !configuredOwner) {
                return { text: "Rule changes require a verified owner. On older hosts, configure ownerSenderIds with the owner's exact host sender identity." };
              }
              ctx.assertOwnerCurrent?.();
            }
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
    function enqueuePendingReceipt(pending, event) {
      const callId = typeof event.toolCallId === "string" && event.toolCallId ? event.toolCallId : null;
      const digest = parametersDigest(event.params ?? {});
      pendingReceipts.push({
        receipt: { ...pending, tool_call_id: callId ?? void 0, params_digest: digest ?? void 0 },
        callId,
        paramsDigest: digest
      });
    }
    function takePendingReceipt(event) {
      const callId = typeof event.toolCallId === "string" && event.toolCallId ? event.toolCallId : null;
      const digest = event.params === void 0 ? null : parametersDigest(event.params);
      const candidates = pendingReceipts.filter((pending) => pending.receipt.tool_name === event.toolName && (callId === null || pending.callId === callId));
      if (candidates.length === 1) {
        const candidate = candidates[0];
        const identityMatches = callId !== null || digest !== null && candidate.paramsDigest === digest;
        const parametersAgree = digest === null || candidate.paramsDigest === digest;
        if (identityMatches && parametersAgree) {
          pendingReceipts.splice(pendingReceipts.indexOf(candidate), 1);
          return candidate.receipt;
        }
      }
      for (const pending of candidates) {
        pendingReceipts.splice(pendingReceipts.indexOf(pending), 1);
        logReceipt(buildUncertainReceipt(
          pending.receipt,
          "Completion could not be uniquely matched to its gate decision; execution and side effects remain uncertain."
        ));
      }
      return void 0;
    }
    function blockRuleMutation(event) {
      if (event.params?.action === "list") return;
      const explanation = "Guard rules are owner-controlled. Use the authenticated /pdm-guard command.";
      logReceipt(buildBlockedReceipt({
        timestamp: (/* @__PURE__ */ new Date()).toISOString(),
        proposed_action: buildIntentText(event.toolName, event.params),
        gate_status: "BLOCKED_OWNER_RULE_CHANGE",
        governing_rules: [],
        rules_source: rulesFilePath,
        explanation,
        tool_executed: false,
        pdm_version: PDM_VERSION,
        openclaw_version: OPENCLAW_VERSION,
        tool_name: event.toolName
      }));
      return { block: true, blockReason: explanation };
    }
    api.on(
      "before_tool_call",
      async (event, ctx) => {
        const toolEvent = event;
        if (toolEvent.toolName === GUARD_RULE_TOOL) {
          return blockRuleMutation(toolEvent);
        }
        const pluginCfg = resolvePluginConfig(registeredCfg, ctx);
        const allowFailure = registeredCfg.failureMode === "allow";
        let goals = [];
        let source = resolveRulesFilePath(pluginCfg);
        const pythonBin = pluginCfg.pythonBin ?? process.env.PDM_GUARD_PYTHON ?? DEFAULT_PYTHON_BIN;
        const timeoutMs = pluginCfg.timeoutMs ?? 8e3;
        const params = toolEvent.params ?? {};
        const intent = buildIntentText(toolEvent.toolName, params);
        const timestamp = (/* @__PURE__ */ new Date()).toISOString();
        function failedCheck(code, explanation) {
          const pending = {
            timestamp,
            proposed_action: intent,
            gate_status: `${allowFailure ? "UNPROTECTED" : "BLOCKED"}_${code}`,
            governing_rules: goals,
            rules_source: source,
            explanation: `${explanation} ${allowFailure ? "Owner enabled unprotected failureMode=allow." : "No execution permission granted."}`,
            tool_executed: allowFailure,
            pdm_version: PDM_VERSION,
            openclaw_version: OPENCLAW_VERSION,
            tool_name: toolEvent.toolName
          };
          if (allowFailure) {
            enqueuePendingReceipt(pending, toolEvent);
            return;
          }
          logReceipt(buildBlockedReceipt(pending));
          return { block: true, blockReason: pending.explanation };
        }
        try {
          ({ goals, source } = await resolveGoals(pluginCfg));
        } catch {
          return failedCheck("RULES_ERROR", "The owner rules could not be loaded.");
        }
        if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 2147483647) {
          return failedCheck("CONFIG_ERROR", "The verifier timeout configuration is invalid.");
        }
        if (!goals.length) {
          return failedCheck("NO_RULES", "No owner guard rules are configured.");
        }
        const result = await callVerifyFunction(
          pythonBin,
          VERIFY_BRIDGE_SCRIPT,
          intent,
          goals,
          timeoutMs
        );
        if (!result) {
          return failedCheck("VERIFY_ERROR", "The verifier failed or returned an invalid or inconsistent response.");
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
        }, toolEvent);
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
        const pending = takePendingReceipt(toolEvent);
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
