/**
 * PDM Guard — GAA gate for OpenClaw via before_tool_call hook.
 *
 * Rules live in ~/.openclaw/pdm-guard-rules.json (configurable).
 * Owners change rules through authenticated /pdm-guard commands.
 * The agent tool pdm_guard_rule may only list rules.
 * Calls pdm_memory.verify() directly (verify_bridge.py subprocess) before
 * every other tool execution — no HTTP endpoint involved.
 */

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import { buildIntentText } from "./intent";
import {
  buildBlockedReceipt,
  buildCompletedReceipt,
  buildUncertainReceipt,
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
const DEFAULT_PYTHON_BIN = "python3";
const GUARD_RULE_TOOL = "pdm_guard_rule";
const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url));
const VERIFY_BRIDGE_SCRIPT = join(PLUGIN_DIR, "verify_bridge.py");

interface PluginConfig {
  pythonBin?: string;
  goals?: string[];
  rulesFile?: string;
  timeoutMs?: number;
  /** Owner-controlled compatibility escape hatch. Default is block. */
  failureMode?: "block" | "allow";
  /** For hosts without a verified senderIsOwner field, explicit owner identities. */
  ownerSenderIds?: string[];
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
  handler: (ctx: {
    args?: string;
    senderId?: string;
    isAuthorizedSender?: boolean;
    senderIsOwner?: boolean;
    assertOwnerCurrent?: () => void;
  }) => Promise<{ text: string }> | { text: string };
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
  toolCallId?: string;
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
}

const VERIFY_OUTPUT_LIMIT = 2 * 1024 * 1024;

function checkedVerifyResponse(value: unknown): VerifyResponse | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  if (typeof row.status !== "string" ||
      !["ALIGNED", "CONFLICT", "TORSION"].includes(row.status)) return null;
  if (typeof row.is_safe_to_act !== "boolean") return null;
  if (row.is_safe_to_act !== (row.status === "ALIGNED")) return null;
  if (typeof row.explanation !== "string" || !row.explanation.trim()) return null;
  if (!Array.isArray(row.conflicting_goals) ||
      !row.conflicting_goals.every((goal) => typeof goal === "string")) return null;
  return row as unknown as VerifyResponse;
}

/** Bind receipt correlation to complete JSON parameters, never the clipped intent. */
function parametersDigest(value: unknown): string | null {
  function ordered(v: unknown): unknown {
    if (v === null || typeof v === "string" || typeof v === "boolean") return v;
    if (typeof v === "number" && Number.isFinite(v)) return v;
    if (Array.isArray(v)) return v.map(ordered);
    if (v && typeof v === "object") {
      return Object.fromEntries(Object.keys(v).sort().map((key) => [
        key, ordered((v as Record<string, unknown>)[key]),
      ]));
    }
    throw new Error("parameters are not JSON values");
  }
  try {
    return createHash("sha256").update(JSON.stringify(ordered(value))).digest("hex");
  } catch {
    return null;
  }
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

async function callVerifyFunction(
  pythonBin: string,
  scriptPath: string,
  intent: string,
  goals: string[],
  timeoutMs: number,
): Promise<VerifyResponse | null> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value: VerifyResponse | null): void => {
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
    child.stdout?.on("data", (chunk: unknown) => {
      stdout += String(chunk);
      if (stdout.length > VERIFY_OUTPUT_LIMIT) {
        child.kill("SIGKILL");
        clearTimeout(timer);
        finish(null);
      }
    });
    child.stderr?.on("data", (chunk: unknown) => {
      stderr += String(chunk);
      if (stderr.length > VERIFY_OUTPUT_LIMIT) {
        child.kill("SIGKILL");
        clearTimeout(timer);
        finish(null);
      }
    });

    child.on("error", (err: unknown) => {
      clearTimeout(timer);
      console.warn(`[PDM GUARD] verify() call failed (${err})`);
      finish(null);
    });

    child.on("close", (code: number | null) => {
      clearTimeout(timer);
      if (code !== 0) {
        console.warn(
          `[PDM GUARD] verify() exited with code ${code}. ${stderr.trim().slice(-500)}`,
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

function formatRulesText(_filePath: string, file: GuardRulesFile): string {
  const goals = goalTextsFromFile(file);
  if (!goals.length) {
    return "No guard rules.\nAdd one: /pdm-guard add <rule>";
  }
  const lines = goals.map((text, i) => `${i + 1}. ${text}`);
  return `Guard rules:\n${lines.join("\n")}`;
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
    const pendingReceipts: Array<{
      receipt: PendingReceipt;
      callId: string | null;
      paramsDigest: string | null;
    }> = [];

    const registerTool = pluginApi.registerTool;
    if (registerTool) {
      registerTool(
        {
          name: GUARD_RULE_TOOL,
          description:
            "List the owner-controlled PDM guard rules. " +
            "Rule changes require the authenticated owner /pdm-guard command; " +
            "the agent cannot add or remove its own restrictions.",
          parameters: {
            type: "object",
            additionalProperties: false,
            properties: {
              action: {
                type: "string",
                enum: ["list"],
                description: "List the owner-controlled rules",
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
            if (params.action !== "list") {
              throw new Error("Guard rules are owner-controlled. Use the authenticated /pdm-guard command.");
            }
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
            if (parsed.action !== "list") {
              const configuredOwner = typeof ctx.senderId === "string" &&
                Array.isArray(registeredCfg.ownerSenderIds) &&
                registeredCfg.ownerSenderIds.includes(ctx.senderId);
              if (ctx.isAuthorizedSender !== true ||
                  (ctx.senderIsOwner !== true && !configuredOwner)) {
                return { text: "Rule changes require a verified owner. On older hosts, configure ownerSenderIds with the owner's exact host sender identity." };
              }
              // Newer hosts can recheck live owner standing immediately here.
              ctx.assertOwnerCurrent?.();
            }
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

    function enqueuePendingReceipt(pending: PendingReceipt, event: BeforeToolCallEvent): void {
      const callId = typeof event.toolCallId === "string" && event.toolCallId ? event.toolCallId : null;
      const digest = parametersDigest(event.params ?? {});
      pendingReceipts.push({
        receipt: { ...pending, tool_call_id: callId ?? undefined, params_digest: digest ?? undefined },
        callId,
        paramsDigest: digest,
      });
    }

    function takePendingReceipt(event: AfterToolCallEvent): PendingReceipt | undefined {
      const callId = typeof event.toolCallId === "string" && event.toolCallId ? event.toolCallId : null;
      const digest = event.params === undefined ? null : parametersDigest(event.params);
      const candidates = pendingReceipts.filter((pending) =>
        pending.receipt.tool_name === event.toolName &&
        (callId === null || pending.callId === callId));
      // A later host hook may rewrite arguments. Without a call ID, different
      // parameter digests cannot safely distinguish concurrent same-tool calls.
      if (candidates.length === 1) {
        const candidate = candidates[0];
        const identityMatches = callId !== null ||
          (digest !== null && candidate.paramsDigest === digest);
        const parametersAgree = digest === null || candidate.paramsDigest === digest;
        if (identityMatches && parametersAgree) {
          pendingReceipts.splice(pendingReceipts.indexOf(candidate), 1);
          return candidate.receipt;
        }
      }
      // With no reliable ID/parameters, contradictory parameters, or concurrent calls,
      // preserve uncertainty rather than attaching somebody else's outcome.
      for (const pending of candidates) {
        pendingReceipts.splice(pendingReceipts.indexOf(pending), 1);
        logReceipt(buildUncertainReceipt(pending.receipt,
          "Completion could not be uniquely matched to its gate decision; execution and side effects remain uncertain."));
      }
      return undefined;
    }

    function blockRuleMutation(event: BeforeToolCallEvent): BeforeToolCallResult | void {
      if (event.params?.action === "list") return;
      const explanation = "Guard rules are owner-controlled. Use the authenticated /pdm-guard command.";
      logReceipt(buildBlockedReceipt({
        timestamp: new Date().toISOString(), proposed_action: buildIntentText(event.toolName, event.params),
        gate_status: "BLOCKED_OWNER_RULE_CHANGE", governing_rules: [], rules_source: rulesFilePath,
        explanation, tool_executed: false, pdm_version: PDM_VERSION,
        openclaw_version: OPENCLAW_VERSION, tool_name: event.toolName,
      }));
      return { block: true, blockReason: explanation };
    }

    api.on(
      "before_tool_call",
      async (event, ctx) => {
        const toolEvent = event as BeforeToolCallEvent;
        if (toolEvent.toolName === GUARD_RULE_TOOL) {
          return blockRuleMutation(toolEvent);
        }

        const pluginCfg = resolvePluginConfig(registeredCfg, ctx as BeforeToolCallContext);
        const allowFailure = registeredCfg.failureMode === "allow";
        let goals: string[] = [];
        let source = resolveRulesFilePath(pluginCfg);
        const pythonBin =
          pluginCfg.pythonBin ?? process.env.PDM_GUARD_PYTHON ?? DEFAULT_PYTHON_BIN;
        const timeoutMs = pluginCfg.timeoutMs ?? 8000;

        const params = (toolEvent.params as Record<string, unknown>) ?? {};
        const intent = buildIntentText(toolEvent.toolName, params);
        const timestamp = new Date().toISOString();

        function failedCheck(code: string, explanation: string): BeforeToolCallResult | void {
          const pending: PendingReceipt = {
            timestamp, proposed_action: intent,
            gate_status: `${allowFailure ? "UNPROTECTED" : "BLOCKED"}_${code}`,
            governing_rules: goals, rules_source: source,
            explanation: `${explanation} ${allowFailure ? "Owner enabled unprotected failureMode=allow." : "No execution permission granted."}`,
            tool_executed: allowFailure, pdm_version: PDM_VERSION,
            openclaw_version: OPENCLAW_VERSION, tool_name: toolEvent.toolName,
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

        if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 2_147_483_647) {
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
          timeoutMs,
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
        }, toolEvent);
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

        const pending = takePendingReceipt(toolEvent);
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
