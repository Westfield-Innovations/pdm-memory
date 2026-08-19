/**
 * PDM Guard — GAA gate for OpenClaw via before_tool_call hook.
 *
 * Calls companion_api /api/v1/pdm/gaa/verify/ before every tool execution.
 * Blocks when verify() reports a direct contradiction.
 * Fail-open if verifyUrl is unreachable or goals are empty.
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const OPENCLAW_VERSION = "2026.7.1-2";
const PDM_VERSION = "0.2.4";

interface PluginConfig {
  verifyUrl: string;
  goals: string[];
  timeoutMs?: number;
}

interface VerifyResponse {
  status: "ALIGNED" | "CONFLICT" | "TORSION";
  is_safe_to_act: boolean;
  explanation: string;
  conflicting_goals: string[];
  version: string;
  elapsed_ms: number;
}

interface Receipt {
  timestamp: string;
  proposed_action: string;
  gate_status: string;
  governing_rules: string[];
  tool_executed: boolean;
  explanation: string;
  pdm_version: string;
  openclaw_version: string;
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

function logReceipt(receipt: Receipt): void {
  console.log(`[PDM GUARD] RECEIPT: ${JSON.stringify(receipt)}`);
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

export default definePluginEntry({
  id: "pdm-guard",
  name: "PDM Guard",
  description:
    "Goal-Anchor Alignment gate. Checks every tool call against your guard rules via pdm-memory verify().",

  register(api) {
    api.on(
      "before_tool_call",
      async (event, _ctx) => {
        const pluginCfg = (api as unknown as { pluginConfig?: Partial<PluginConfig> }).pluginConfig ?? {};

        const goals: string[] =
          pluginCfg.goals && pluginCfg.goals.length > 0
            ? pluginCfg.goals
            : process.env.PDM_GUARD_GOALS
              ? process.env.PDM_GUARD_GOALS.split("|").map((g) => g.trim()).filter(Boolean)
              : [
                  "never say the word banana",
                  "never write lorem ipsum text",
                ];

        const verifyUrl: string =
          pluginCfg.verifyUrl ??
          process.env.PDM_GUARD_URL ??
          "http://localhost:8000/api/v1/pdm/gaa/verify/";

        const timeoutMs: number = pluginCfg.timeoutMs ?? 8000;

        const intent = buildIntentText(
          event.toolName,
          (event.params as Record<string, unknown>) ?? {},
        );

        if (!goals.length || !verifyUrl) {
          logReceipt({
            timestamp: new Date().toISOString(),
            proposed_action: intent,
            gate_status: "SKIPPED_NO_RULES",
            governing_rules: [],
            tool_executed: true,
            explanation: "No goals configured - fail-open.",
            pdm_version: PDM_VERSION,
            openclaw_version: OPENCLAW_VERSION,
          });
          return;
        }

        const result = await callVerify(verifyUrl, intent, goals, timeoutMs);

        if (!result) {
          logReceipt({
            timestamp: new Date().toISOString(),
            proposed_action: intent,
            gate_status: "SKIPPED_SIDECAR_ERROR",
            governing_rules: goals,
            tool_executed: true,
            explanation: "verify() unreachable - fail-open.",
            pdm_version: PDM_VERSION,
            openclaw_version: OPENCLAW_VERSION,
          });
          return;
        }

        const shouldBlock =
          result.status === "TORSION" ||
          (result.status === "CONFLICT" &&
            Array.isArray(result.conflicting_goals) &&
            result.conflicting_goals.length > 0);
        const allowed = !shouldBlock;

        logReceipt({
          timestamp: new Date().toISOString(),
          proposed_action: intent,
          gate_status: result.status,
          governing_rules: goals,
          tool_executed: allowed,
          explanation: result.explanation,
          pdm_version: PDM_VERSION,
          openclaw_version: OPENCLAW_VERSION,
        });

        if (!allowed) {
          const blockReason =
            `Action blocked by PDM guard (${result.status}): ${result.explanation}` +
            (result.conflicting_goals.length
              ? ` Rule: "${result.conflicting_goals[0]}"`
              : "");
          return { block: true, blockReason };
        }
      },
      { priority: 90 },
    );
  },
});
