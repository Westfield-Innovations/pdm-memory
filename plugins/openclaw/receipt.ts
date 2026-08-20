/**
 * Receipt helpers for PDM Guard — one complete record per gate decision.
 *
 * Blocked tools log immediately (no after_tool_call). Allowed/skipped tools
 * defer resulting_state until after_tool_call observes the real outcome.
 */

export const OPENCLAW_VERSION = "2026.7.1-2";
export const PDM_VERSION = "0.2.4";

export interface Receipt {
  timestamp: string;
  proposed_action: string;
  gate_status: string;
  governing_rules: string[];
  rules_source: string;
  tool_executed: boolean;
  explanation: string;
  pdm_version: string;
  openclaw_version: string;
  resulting_state: ResultingState;
  tool_result?: unknown;
  duration_ms?: number | null;
}

export interface ResultingState {
  executed: boolean;
  side_effects: "none" | "observed";
  tool_name: string;
  exit_code?: number | null;
  stdout?: string | null;
  stderr?: string | null;
  error?: string | null;
  detail?: string | null;
}

export interface PendingReceipt {
  timestamp: string;
  proposed_action: string;
  gate_status: string;
  governing_rules: string[];
  rules_source: string;
  explanation: string;
  tool_executed: boolean;
  pdm_version: string;
  openclaw_version: string;
  tool_name: string;
}

export interface AfterToolCallEvent {
  toolName: string;
  params?: Record<string, unknown>;
  result?: unknown;
  error?: string;
  durationMs?: number;
}

const MAX_TEXT = 2000;

function clip(text: string, maxLen = MAX_TEXT): string {
  return text.length <= maxLen ? text : `${text.slice(0, maxLen - 1)}…`;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (value == null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function readNumber(record: Record<string, unknown>, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
  }
  return null;
}

function readString(record: Record<string, unknown>, ...keys: string[]): string | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string") {
      return value;
    }
  }
  return null;
}

/** Summarize an exec/write tool outcome into resulting_state fields. */
export function summarizeToolOutcome(
  toolName: string,
  result?: unknown,
  error?: string,
): { state: ResultingState; toolResult: unknown } {
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
        detail: "tool reported error after gate allowed execution",
      },
      toolResult: null,
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
          detail: "exec side-effect observed",
        },
        toolResult: result,
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
        detail: "tool completed with empty result",
      },
      toolResult: null,
    };
  }

  const serialized =
    typeof result === "string" ? clip(result) : clip(JSON.stringify(result));

  return {
    state: {
      executed: true,
      side_effects: "observed",
      tool_name: toolName,
      exit_code: null,
      stdout: null,
      stderr: null,
      error: null,
      detail: serialized,
    },
    toolResult: result,
  };
}

/** State when the gate blocked the tool — no host execution. */
export function blockedResultingState(toolName: string): ResultingState {
  return {
    executed: false,
    side_effects: "none",
    tool_name: toolName,
    exit_code: null,
    stdout: null,
    stderr: null,
    error: null,
    detail: "tool call stopped at before_tool_call; host did not execute",
  };
}

export function buildBlockedReceipt(
  pending: Omit<PendingReceipt, "tool_name"> & { tool_name: string },
): Receipt {
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
  };
}

export function buildCompletedReceipt(
  pending: PendingReceipt,
  event: AfterToolCallEvent,
): Receipt {
  const { state, toolResult } = summarizeToolOutcome(
    event.toolName,
    event.result,
    event.error,
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
  };
}

export function logReceipt(receipt: Receipt): void {
  console.log(`[PDM GUARD] RECEIPT: ${JSON.stringify(receipt)}`);
}
