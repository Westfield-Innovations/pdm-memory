/**
 * PDM Guard — GAA gate for OpenClaw via before_tool_call hook.
 *
 * Rules live in ~/.openclaw/pdm-guard-rules.json (configurable).
 * Manage rules with:
 *   - slash command /pdm-guard (bypasses LLM)
 *   - agent tool pdm_guard_rule
 * Calls companion_api /api/v1/pdm/gaa/verify/ before every other tool execution.
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import {
  buildBlockedReceipt,
  buildCompletedReceipt,
  logReceipt,
  OPENCLAW_VERSION,
  PDM_VERSION,
  type AfterToolCallEvent,
  type PendingReceipt,
} from "./receipt";
import {
  DEFAULT_RULES_FILE,
  addGuardRule,
  expandHome,
  goalTextsFromFile,
  listGuardRules,
  loadGoalTexts,
  removeGuardRule,
  type GuardRulesFile,
} from "./rules-store";
const DEFAULT_VERIFY_URL = "http://localhost:8000/api/v1/pdm/gaa/verify/";
const GUARD_RULE_TOOL = "pdm_guard_rule";

interface PluginConfig {
  verifyUrl?: string;
  goals?: string[];
  rulesFile?: string;
  timeoutMs?: number;
}

interface PluginApi {
  pluginConfig?: Partial<PluginConfig>;
  on: (
    hook: "before_tool_call" | "after_tool_call",
    handler: (
      event: BeforeToolCallEvent | AfterToolCallEvent,
      ctx: BeforeToolCallContext,
    ) => Promise<BeforeToolCallResult | void>,
    opts?: { priority?: number },
  ) => void;
  registerTool?: (
    tool: RegisteredTool,
    opts?: { optional?: boolean },
  ) => void;
  registerCommand?: (def: PluginCommand) => void;
}

interface PluginCommand {
  name: string;
  description: string;
  acceptsArgs?: boolean;
  requireAuth?: boolean;
  handler: (ctx: { args?: string }) => Promise<{ text: string }> | { text: string };
}

interface RegisteredTool {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  outputSchema?: Record<string, unknown>;
  execute: (id: string, params: GuardRuleToolParams) => Promise<ToolExecuteResult>;
}

interface GuardRuleToolParams {
  action: "add" | "remove" | "list";
  rule?: string;
}

interface ToolExecuteResult {
  content: Array<{ type: "text"; text: string }>;
  details: Record<string, unknown>;
}

interface BeforeToolCallEvent {
  toolName: string;
  params?: Record<string, unknown>;
}

interface BeforeToolCallContext {
  pluginConfig?: Partial<PluginConfig>;
}

interface BeforeToolCallResult {
  block: boolean;
  blockReason: string;
}

interface VerifyResponse {
  status: "ALIGNED" | "CONFLICT" | "TORSION";
  is_safe_to_act: boolean;
  explanation: string;
  conflicting_goals: string[];
  version: string;
  elapsed_ms: number;
}

function buildIntentText(toolName: string, params: Record<string, unknown>): string {
  const summarize = (value: unknown, maxLen = 500): string => {
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

  const paramSummary = Object.entries(params)
    .slice(0, 8)
    .map(([k, v]) => `${k}=${summarize(v, 240)}`)
    .join(", ");
  return paramSummary ? `${toolName} (${paramSummary})` : toolName;
}

function resolveRulesFilePath(cfg: Partial<PluginConfig>): string {
  const fromCfg = cfg.rulesFile?.trim();
  if (fromCfg) return expandHome(fromCfg);
  const fromEnv = process.env.PDM_GUARD_RULES_FILE?.trim();
  if (fromEnv) return expandHome(fromEnv);
  return DEFAULT_RULES_FILE;
}

function resolveConfigGoals(cfg: Partial<PluginConfig>): string[] {
  if (cfg.goals?.length) return cfg.goals;
  const fromEnv = process.env.PDM_GUARD_GOALS;
  if (fromEnv) {
    return fromEnv.split("|").map((g) => g.trim()).filter(Boolean);
  }
  return [];
}

async function resolveGoals(cfg: Partial<PluginConfig>): Promise<{ goals: string[]; source: string }> {
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

function resolvePluginConfig(
  registeredCfg: Partial<PluginConfig>,
  ctx: BeforeToolCallContext,
): Partial<PluginConfig> {
  return ctx.pluginConfig ?? registeredCfg;
}

async function callVerify(
  verifyUrl: string,
  intent: string,
  goals: string[],
  timeoutMs: number,
): Promise<VerifyResponse | null> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const resp = await fetch(verifyUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ intent, goals }),
      signal: controller.signal,
    });
    clearTimeout(timer);
    if (!resp.ok) {
      console.warn(`[PDM GUARD] verify endpoint returned ${resp.status} - fail-open`);
      return null;
    }
    return (await resp.json()) as VerifyResponse;
  } catch (err) {
    console.warn(`[PDM GUARD] verify call failed (${err}) - fail-open`);
    return null;
  }
}

function formatRulesText(filePath: string, file: GuardRulesFile): string {
  const goals = goalTextsFromFile(file);
  if (!goals.length) {
    return `No guard rules in ${filePath}.\nAdd one: /pdm-guard add never hardcode localhost`;
  }
  const lines = goals.map((text, i) => `${i + 1}. ${text}`);
  return `Guard rules (${filePath}):\n${lines.join("\n")}`;
}

function guardRuleToolResult(details: Record<string, unknown>): ToolExecuteResult {
  return {
    content: [{ type: "text", text: JSON.stringify(details, null, 2) }],
    details,
  };
}

async function handleRuleAction(
  rulesFilePath: string,
  action: "add" | "remove" | "list",
  rule?: string,
): Promise<{ details: Record<string, unknown>; text: string }> {
  if (action === "list") {
    const file = await listGuardRules(rulesFilePath);
    const details = {
      action: "list",
      rules_file: rulesFilePath,
      rules: file.rules,
      goal_texts: goalTextsFromFile(file),
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
      goal_texts: goalTextsFromFile(result.file),
    };
    const text = result.deduped
      ? `Already present (deduped): "${result.entry.text}"\n\n${formatRulesText(rulesFilePath, result.file)}`
      : `Added: "${result.entry.text}"\n\n${formatRulesText(rulesFilePath, result.file)}`;
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
      goal_texts: goalTextsFromFile(result.file),
    };
    const text = result.removed
      ? `Removed: "${result.removed.text}"\n\n${formatRulesText(rulesFilePath, result.file)}`
      : `No matching rule for "${rule.trim()}".\n\n${formatRulesText(rulesFilePath, result.file)}`;
    return { details, text };
  }

  throw new Error(`unsupported action: ${String(action)}`);
}

function parseGuardCommandArgs(args?: string): { action: "add" | "remove" | "list"; rule?: string } {
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
    `Usage:\n/pdm-guard list\n/pdm-guard add <rule>\n/pdm-guard remove <rule>`,
  );
}

export default definePluginEntry({
  id: "pdm-guard",
  name: "PDM Guard",
  description:
    "Goal-Anchor Alignment gate. Checks every tool call against guard rules via pdm-memory verify().",

  register(api) {
    const registeredCfg = (api as PluginApi).pluginConfig ?? {};
    const rulesFilePath = resolveRulesFilePath(registeredCfg);
    const pluginApi = api as PluginApi;
    const pendingReceipts: PendingReceipt[] = [];

    const registerTool = pluginApi.registerTool;
    if (registerTool) {
      registerTool(
        {
          name: GUARD_RULE_TOOL,
          description:
            "REQUIRED for listing, adding, or deleting PDM guard rules. " +
            "When the user says show/list/add/set/remove/delete guard rules, call this tool. " +
            "Do not invent rules from memory — always call action=list first. " +
            "Rules are stored in ~/.openclaw/pdm-guard-rules.json and enforce before_tool_call.",
          parameters: {
            type: "object",
            additionalProperties: false,
            properties: {
              action: {
                type: "string",
                enum: ["add", "remove", "list"],
                description: "add a rule, remove by text/id, or list all rules",
              },
              rule: {
                type: "string",
                description: "Plain-English guard rule (required for add/remove)",
              },
            },
            required: ["action"],
          },
          outputSchema: {
            type: "object",
            additionalProperties: true,
            properties: {
              action: { type: "string" },
              rules: { type: "array" },
              rules_file: { type: "string" },
            },
            required: ["action", "rules_file"],
          },
          async execute(_id, params) {
            const { details } = await handleRuleAction(
              rulesFilePath,
              params.action,
              params.rule,
            );
            return guardRuleToolResult(details);
          },
        },
        { optional: false },
      );
    } else {
      console.warn("[PDM GUARD] registerTool unavailable — pdm_guard_rule tool not registered");
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
              parsed.rule,
            );
            return { text };
          } catch (err) {
            return { text: err instanceof Error ? err.message : String(err) };
          }
        },
      });
    } else {
      console.warn("[PDM GUARD] registerCommand unavailable — /pdm-guard not registered");
    }

    console.log(
      `[PDM GUARD] registered (rulesFile=${rulesFilePath}, tool=${GUARD_RULE_TOOL}, command=/pdm-guard)`,
    );

    function enqueuePendingReceipt(pending: PendingReceipt): void {
      pendingReceipts.push(pending);
    }

    function takePendingReceipt(): PendingReceipt | undefined {
      return pendingReceipts.shift();
    }

    api.on(
      "before_tool_call",
      async (event, ctx) => {
        const toolEvent = event as BeforeToolCallEvent;
        if (toolEvent.toolName === GUARD_RULE_TOOL) {
          return;
        }

        const pluginCfg = resolvePluginConfig(registeredCfg, ctx as BeforeToolCallContext);
        const { goals, source } = await resolveGoals(pluginCfg);
        const verifyUrl =
          pluginCfg.verifyUrl ?? process.env.PDM_GUARD_URL ?? DEFAULT_VERIFY_URL;
        const timeoutMs = pluginCfg.timeoutMs ?? 8000;

        const params = (toolEvent.params as Record<string, unknown>) ?? {};
        const intent = buildIntentText(toolEvent.toolName, params);
        const timestamp = new Date().toISOString();

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
            tool_name: toolEvent.toolName,
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
            tool_name: toolEvent.toolName,
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
              tool_name: toolEvent.toolName,
            }),
          );

          const blockReason =
            `Action blocked by PDM guard (${result.status}): ${result.explanation}` +
            (result.conflicting_goals.length
              ? ` Rule: "${result.conflicting_goals[0]}"`
              : "");
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
          tool_name: toolEvent.toolName,
        });
      },
      { priority: 90 },
    );

    api.on(
      "after_tool_call",
      async (event) => {
        const toolEvent = event as AfterToolCallEvent;
        if (toolEvent.toolName === GUARD_RULE_TOOL) {
          return;
        }

        const pending = takePendingReceipt();
        if (!pending) {
          console.warn(
            `[PDM GUARD] after_tool_call without pending receipt for ${toolEvent.toolName}`,
          );
          return;
        }

        logReceipt(buildCompletedReceipt(pending, toolEvent));
      },
      { priority: 90 },
    );
  },
});
