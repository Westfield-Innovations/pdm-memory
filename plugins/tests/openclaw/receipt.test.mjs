import assert from "node:assert/strict";
import test from "node:test";

import {
  blockedResultingState,
  buildBlockedReceipt,
  buildCompletedReceipt,
  summarizeToolOutcome,
} from "../../openclaw/receipt.ts";

test("blocked resulting_state shows no side effects", () => {
  const state = blockedResultingState("exec");
  assert.equal(state.executed, false);
  assert.equal(state.side_effects, "none");
  assert.equal(state.tool_name, "exec");
  assert.equal(state.exit_code, null);
});

test("buildBlockedReceipt includes resulting_state", () => {
  const receipt = buildBlockedReceipt({
    timestamp: "2026-08-20T07:56:42.546Z",
    proposed_action: "exec (command=curl http://localhost:8080/api/health)",
    gate_status: "TORSION",
    governing_rules: ["never hardcode localhost"],
    rules_source: "/tmp/rules.json",
    explanation: "blocked",
    tool_executed: false,
    pdm_version: "0.2.4",
    openclaw_version: "2026.7.1-2",
    tool_name: "exec",
  });

  assert.equal(receipt.tool_executed, false);
  assert.equal(receipt.resulting_state.executed, false);
  assert.equal(receipt.resulting_state.side_effects, "none");
});

test("summarizeToolOutcome captures exec stdout/stderr/exit code", () => {
  const { state } = summarizeToolOutcome("exec", {
    exitCode: 7,
    stdout: "",
    stderr: "curl: (7) Failed to connect to localhost port 8080",
  });

  assert.equal(state.executed, true);
  assert.equal(state.side_effects, "observed");
  assert.equal(state.exit_code, 7);
  assert.match(state.stderr ?? "", /Failed to connect/);
});

test("buildCompletedReceipt merges pending gate decision with tool outcome", () => {
  const receipt = buildCompletedReceipt(
    {
      timestamp: "2026-08-20T08:13:42.414Z",
      proposed_action: "exec (command=curl http://localhost:8080/api/health)",
      gate_status: "ALIGNED",
      governing_rules: ["never write lorem ipsum"],
      rules_source: "/tmp/rules.json",
      explanation: "Proceed.",
      tool_executed: true,
      pdm_version: "0.2.4",
      openclaw_version: "2026.7.1-2",
      tool_name: "exec",
    },
    {
      toolName: "exec",
      result: {
        exitCode: 7,
        stdout: "",
        stderr: "curl: (7) Failed to connect",
      },
      durationMs: 42,
    },
  );

  assert.equal(receipt.gate_status, "ALIGNED");
  assert.equal(receipt.tool_executed, true);
  assert.equal(receipt.duration_ms, 42);
  assert.equal(receipt.resulting_state.exit_code, 7);
  assert.equal(receipt.resulting_state.executed, true);
});
