import assert from "node:assert/strict";
import test from "node:test";

import {
  blockedResultingState,
  buildBlockedReceipt,
  buildCompletedReceipt,
  summarizeToolOutcome,
  toReceiptLog,
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

test("toReceiptLog keeps a short demo-friendly line", () => {
  const receipt = buildBlockedReceipt({
    timestamp: "2026-08-20T07:56:42.546Z",
    proposed_action:
      'write path=config.py content≈DB_HOST = "127.0.0.1"\nDB_NAME = "app"',
    gate_status: "TORSION",
    governing_rules: ["never create a file named config.py"],
    rules_source: "/tmp/rules.json",
    explanation:
      "This intent is dangerous for system integrity: write path=config.py contradicts Goal Anchor 'never create a file named config.py'.",
    tool_executed: false,
    pdm_version: "0.2.4",
    openclaw_version: "2026.7.1-2",
    tool_name: "write",
  });

  const line = toReceiptLog(receipt);
  assert.equal(line.time, "2026-08-20T07:56:42.546Z");
  assert.equal(line.status, "TORSION");
  assert.equal(line.tool, "write");
  assert.equal(line.executed, false);
  assert.equal(line.action, "write path=config.py");
  assert.deepEqual(line.rules, ["never create a file named config.py"]);
  assert.ok(String(line.why).length <= 100);
  assert.equal(line.pdm_version, undefined);
  assert.equal(line.tool_result, undefined);
  assert.equal(line.resulting_state, undefined);
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
