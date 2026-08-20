// pdm-guard.ts
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
var OPENCLAW_VERSION = "2026.7.1-2";
var PDM_VERSION = "0.2.4";
var DEFAULT_VERIFY_URL = "http://localhost:8000/api/v1/pdm/gaa/verify/";
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
function logReceipt(receipt) {
  console.log(`[PDM GUARD] RECEIPT: ${JSON.stringify(receipt)}`);
}
function resolveGoals(cfg) {
  if (cfg.goals?.length) return cfg.goals;
  const fromEnv = process.env.PDM_GUARD_GOALS;
  if (fromEnv) {
    return fromEnv.split("|").map((g) => g.trim()).filter(Boolean);
  }
  return [];
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
var pdm_guard_default = definePluginEntry({
  id: "pdm-guard",
  name: "PDM Guard",
  description: "Goal-Anchor Alignment gate. Checks every tool call against your guard rules via pdm-memory verify().",
  register(api) {
    const registeredCfg = api.pluginConfig ?? {};
    api.on(
      "before_tool_call",
      async (event, ctx) => {
        const pluginCfg = resolvePluginConfig(registeredCfg, ctx);
        const goals = resolveGoals(pluginCfg);
        const verifyUrl = pluginCfg.verifyUrl ?? process.env.PDM_GUARD_URL ?? DEFAULT_VERIFY_URL;
        const timeoutMs = pluginCfg.timeoutMs ?? 8e3;
        const intent = buildIntentText(
          event.toolName,
          event.params ?? {}
        );
        if (!goals.length || !verifyUrl) {
          logReceipt({
            timestamp: (/* @__PURE__ */ new Date()).toISOString(),
            proposed_action: intent,
            gate_status: "SKIPPED_NO_RULES",
            governing_rules: [],
            tool_executed: true,
            explanation: "No goals configured - fail-open.",
            pdm_version: PDM_VERSION,
            openclaw_version: OPENCLAW_VERSION
          });
          return;
        }
        const result = await callVerify(verifyUrl, intent, goals, timeoutMs);
        if (!result) {
          logReceipt({
            timestamp: (/* @__PURE__ */ new Date()).toISOString(),
            proposed_action: intent,
            gate_status: "SKIPPED_SIDECAR_ERROR",
            governing_rules: goals,
            tool_executed: true,
            explanation: "verify() unreachable - fail-open.",
            pdm_version: PDM_VERSION,
            openclaw_version: OPENCLAW_VERSION
          });
          return;
        }
        const allowed = result.is_safe_to_act === true;
        logReceipt({
          timestamp: (/* @__PURE__ */ new Date()).toISOString(),
          proposed_action: intent,
          gate_status: result.status,
          governing_rules: goals,
          tool_executed: allowed,
          explanation: result.explanation,
          pdm_version: PDM_VERSION,
          openclaw_version: OPENCLAW_VERSION
        });
        if (!allowed) {
          const blockReason = `Action blocked by PDM guard (${result.status}): ${result.explanation}` + (result.conflicting_goals.length ? ` Rule: "${result.conflicting_goals[0]}"` : "");
          return { block: true, blockReason };
        }
      },
      { priority: 90 }
    );
  }
});
export {
  pdm_guard_default as default
};
