import assert from "node:assert/strict";
import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { registerHooks } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

// Load the shipped bundle, replacing only the absent host's entry helper.
// All registered hooks, subprocess checks and receipt writes are the real plugin.
const loader = registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "openclaw/plugin-sdk/plugin-entry") {
      return {
        url: "data:text/javascript,export const definePluginEntry = entry => entry;",
        shortCircuit: true,
      };
    }
    return nextResolve(specifier, context);
  },
});
const plugin = (await import("../../openclaw/pdm-guard.js")).default;
loader.deregister();

const aligned = {
  status: "ALIGNED", is_safe_to_act: true,
  explanation: "Aligned for the test operation.", conflicting_goals: [],
};

async function fixture(t, { response = aligned, raw, exitCode = 0, hang = false,
  goals = ["never ignore errors"], failureMode, missingPython = false,
  malformedRules = false, timeoutMs = 1000, ownerSenderIds } = {}) {
  const dir = await mkdtemp(join(tmpdir(), "pdm-registered-hooks-"));
  const rulesFile = join(dir, "rules.json");
  const receiptFile = join(dir, "receipts.jsonl");
  const pythonBin = join(dir, "fake-python");
  if (malformedRules) await writeFile(rulesFile, "{");
  if (!missingPython) {
    await writeFile(pythonBin, `#!${process.execPath}\n` +
      `process.stdin.resume(); process.stdin.on("end", () => {\n` +
      (hang ? `setInterval(() => {}, 1000);\n` :
        `process.stdout.write(${JSON.stringify(raw ?? JSON.stringify(response))}); process.exit(${exitCode});\n`) +
      `});\n`);
    await chmod(pythonBin, 0o700);
  }
  const oldLog = process.env.PDM_GUARD_RECEIPT_LOG;
  const oldGoals = process.env.PDM_GUARD_GOALS;
  process.env.PDM_GUARD_RECEIPT_LOG = receiptFile;
  process.env.PDM_GUARD_GOALS = "";
  t.after(async () => {
    if (oldLog === undefined) delete process.env.PDM_GUARD_RECEIPT_LOG;
    else process.env.PDM_GUARD_RECEIPT_LOG = oldLog;
    if (oldGoals === undefined) delete process.env.PDM_GUARD_GOALS;
    else process.env.PDM_GUARD_GOALS = oldGoals;
    await rm(dir, { recursive: true, force: true });
  });
  const hooks = new Map();
  let tool, command;
  plugin.register({
    pluginConfig: { rulesFile, pythonBin, goals, failureMode, timeoutMs, ownerSenderIds },
    on(name, handler) { hooks.set(name, handler); },
    registerTool(definition) { tool = definition; },
    registerCommand(definition) { command = definition; },
  });
  return {
    before: (event, ctx = {}) => hooks.get("before_tool_call")(event, ctx),
    after: (event) => hooks.get("after_tool_call")(event, {}),
    tool, command, rulesFile,
    async rows() {
      try {
        return (await readFile(receiptFile, "utf8")).trim().split("\n").filter(Boolean).map(JSON.parse);
      } catch (error) {
        if (error.code === "ENOENT") return [];
        throw error;
      }
    },
  };
}

const request = { toolName: "exec", params: { command: "echo hello" }, toolCallId: "call-1" };

test("registered hook permits a valid aligned report and matches its completion", async (t) => {
  const f = await fixture(t);
  assert.equal(await f.before(request), undefined);
  await f.after({ ...request, result: { stdout: "hello", exitCode: 0 } });
  const [row] = await f.rows();
  assert.equal(row.status, "ALIGNED");
  assert.equal(row.executed, true);
  assert.equal(row.tool_call_id, "call-1");
  assert.match(row.params_digest, /^[a-f0-9]{64}$/);
});

for (const status of ["CONFLICT", "TORSION"]) {
  test(`registered hook blocks a ${status} report`, async (t) => {
    const f = await fixture(t, { response: {
      status, is_safe_to_act: false, explanation: "Refused.", conflicting_goals: ["owner rule"],
    } });
    assert.equal((await f.before(request)).block, true);
    const [row] = await f.rows();
    assert.equal(row.status, status);
    assert.equal(row.executed, false);
  });
}

for (const [name, options, code] of [
  ["missing rules", { goals: [] }, "BLOCKED_NO_RULES"],
  ["malformed rules", { malformedRules: true }, "BLOCKED_RULES_ERROR"],
  ["missing interpreter", { missingPython: true }, "BLOCKED_VERIFY_ERROR"],
  ["nonzero verifier exit", { exitCode: 7 }, "BLOCKED_VERIFY_ERROR"],
  ["verifier timeout", { hang: true, timeoutMs: 60 }, "BLOCKED_VERIFY_ERROR"],
  ["malformed verifier output", { raw: "not-json" }, "BLOCKED_VERIFY_ERROR"],
  ["invalid timeout", { timeoutMs: 0 }, "BLOCKED_CONFIG_ERROR"],
]) {
  test(`registered hook defaults to blocking ${name}`, async (t) => {
    const f = await fixture(t, options);
    assert.equal((await f.before(request)).block, true);
    const [row] = await f.rows();
    assert.equal(row.status, code);
    assert.equal(row.executed, false);
  });
}

for (const [name, response] of [
  ["null", null],
  ["array", []],
  ["missing fields", { status: "ALIGNED", is_safe_to_act: true }],
  ["unknown status", { ...aligned, status: "MAYBE" }],
  ["false aligned flag", { ...aligned, is_safe_to_act: false }],
  ["true refusal flag", { ...aligned, status: "TORSION" }],
  ["nonboolean flag", { ...aligned, is_safe_to_act: "true" }],
  ["invalid goals", { ...aligned, conflicting_goals: [5] }],
  ["empty explanation", { ...aligned, explanation: " " }],
]) {
  test(`registered hook blocks invalid or inconsistent report ${name}`, async (t) => {
    const f = await fixture(t, { response });
    assert.equal((await f.before(request)).block, true);
    assert.equal((await f.rows())[0].status, "BLOCKED_VERIFY_ERROR");
  });
}

test("only registered owner configuration can enable unprotected compatibility", async (t) => {
  const f = await fixture(t, { goals: [] });
  assert.equal((await f.before(request, { pluginConfig: { failureMode: "allow" } })).block, true);
});

for (const [name, options] of [
  ["no rules", { goals: [] }],
  ["checker error", { missingPython: true }],
]) {
  test(`explicit owner failureMode=allow marks ${name} unprotected`, async (t) => {
    const f = await fixture(t, { ...options, failureMode: "allow" });
    assert.equal(await f.before(request), undefined);
    await f.after({ ...request, result: {} });
    const [row] = await f.rows();
    assert.match(row.status, /^UNPROTECTED_/);
    assert.equal(row.executed, true);
  });
}

test("compatibility mode still blocks a valid refusal", async (t) => {
  const f = await fixture(t, { failureMode: "allow", response: {
    status: "CONFLICT", is_safe_to_act: false, explanation: "Refused.", conflicting_goals: [],
  } });
  assert.equal((await f.before(request)).block, true);
});

test("agent cannot change rules through either hook or direct tool execution", async (t) => {
  const f = await fixture(t);
  assert.deepEqual(f.tool.parameters.properties.action.enum, ["list"]);
  for (const action of ["add", "remove"]) {
    const params = { action, rule: "never ignore errors" };
    assert.equal((await f.before({ toolName: "pdm_guard_rule", params })).block, true);
    await assert.rejects(f.tool.execute("call", params), /owner-controlled/);
  }
  assert.equal(await f.before({ toolName: "pdm_guard_rule", params: { action: "list" } }), undefined);
  const listed = await f.tool.execute("list", { action: "list" });
  assert.equal(listed.details.action, "list");
});

test("authenticated owner command retains add/remove access", async (t) => {
  const f = await fixture(t);
  assert.equal(f.command.requireAuth, true);
  const owner = { isAuthorizedSender: true, senderIsOwner: true };
  await f.command.handler({ ...owner, args: "add never touch owner records" });
  assert.equal(JSON.parse(await readFile(f.rulesFile, "utf8")).rules.length, 1);
  await f.command.handler({ ...owner, args: "remove never touch owner records" });
  assert.equal(JSON.parse(await readFile(f.rulesFile, "utf8")).rules.length, 0);
});

test("authorized nonowners and missing owner evidence cannot mutate rules", async (t) => {
  const f = await fixture(t);
  for (const identity of [{}, { isAuthorizedSender: true, senderIsOwner: false }]) {
    const result = await f.command.handler({ ...identity, args: "add never touch owner records" });
    assert.match(result.text, /verified owner/);
  }
  await assert.rejects(readFile(f.rulesFile, "utf8"), { code: "ENOENT" });
});

test("an explicit owner sender identity supports older authorized hosts", async (t) => {
  const f = await fixture(t, { ownerSenderIds: ["owner-42"] });
  const refused = await f.command.handler({ senderId: "owner-42", args: "add first owner rule" });
  assert.match(refused.text, /verified owner/);
  await f.command.handler({ senderId: "owner-42", isAuthorizedSender: true, args: "add first owner rule" });
  assert.equal(JSON.parse(await readFile(f.rulesFile, "utf8")).rules[0].text, "first owner rule");
});

test("host revocation check can refuse a formerly admitted owner", async (t) => {
  const f = await fixture(t);
  const result = await f.command.handler({
    isAuthorizedSender: true, senderIsOwner: true,
    assertOwnerCurrent() { throw new Error("Owner standing revoked."); },
    args: "add never touch owner records",
  });
  assert.match(result.text, /revoked/);
  await assert.rejects(readFile(f.rulesFile, "utf8"), { code: "ENOENT" });
});

test("concurrent out-of-order completions match host call identities", async (t) => {
  const f = await fixture(t);
  const a = { toolName: "exec", toolCallId: "a", params: { command: "first" } };
  const b = { toolName: "exec", toolCallId: "b", params: { command: "second" } };
  await Promise.all([f.before(a), f.before(b)]);
  await f.after({ ...b, result: "second done" });
  await f.after({ ...a, result: "first done" });
  const rows = await f.rows();
  assert.deepEqual(rows.map((r) => r.tool_call_id), ["b", "a"]);
  assert.match(rows[0].action, /second/);
  assert.match(rows[1].action, /first/);
});

test("without host IDs even distinct concurrent parameters remain uncertain", async (t) => {
  const f = await fixture(t);
  const a = { toolName: "exec", params: { command: "first" } };
  const b = { toolName: "exec", params: { command: "second" } };
  await Promise.all([f.before(a), f.before(b)]);
  await f.after({ ...b, result: "done" });
  await f.after({ ...a, result: "done" });
  const rows = await f.rows();
  assert.equal(rows.length, 2);
  assert.ok(rows.every((r) => r.status === "UNCERTAIN_CORRELATION" && r.executed === null));
});

test("a host call ID with contradictory final parameters remains uncertain", async (t) => {
  const f = await fixture(t);
  await f.before(request);
  await f.after({ ...request, params: { command: "changed by another hook" }, result: "done" });
  const [row] = await f.rows();
  assert.equal(row.status, "UNCERTAIN_CORRELATION");
  assert.equal(row.executed, null);
});

test("indistinguishable concurrent calls preserve uncertainty instead of FIFO results", async (t) => {
  const f = await fixture(t);
  const event = { toolName: "exec", params: { command: "identical" } };
  await Promise.all([f.before(event), f.before(event)]);
  await f.after({ ...event, result: "cannot know which call" });
  const rows = await f.rows();
  assert.equal(rows.length, 2);
  assert.ok(rows.every((r) => r.status === "UNCERTAIN_CORRELATION"));
  assert.ok(rows.every((r) => r.executed === null && r.outcome_status === "UNCERTAIN"));
});

test("a completion without identity or parameters is not called observed", async (t) => {
  const f = await fixture(t);
  await f.before({ toolName: "exec", params: { command: "first" } });
  await f.after({ toolName: "exec", result: "uncorrelated" });
  const [row] = await f.rows();
  assert.equal(row.status, "UNCERTAIN_CORRELATION");
  assert.equal(row.executed, null);
});
