/**
 * E2E for OpenClaw plugin path (Part 2 spike semantics).
 * Rules file + direct pdm_memory.verify() call → block/permit decisions (same as plugin).
 */

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  addGuardRule,
  invalidateRulesCache,
  loadGoalTexts,
  removeGuardRule,
} from "../../openclaw/rules-store.ts";

const PYTHON_BIN = process.env.PDM_GUARD_PYTHON || "python3";
const VERIFY_BRIDGE_SCRIPT = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../openclaw/verify_bridge.py",
);

async function verifyIntent(intent, goals) {
  const data = await new Promise((resolve, reject) => {
    const child = spawn(PYTHON_BIN, [VERIFY_BRIDGE_SCRIPT], {
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(`verify_bridge.py exited with ${code}: ${stderr}`));
        return;
      }
      resolve(JSON.parse(stdout));
    });
    child.stdin.write(JSON.stringify({ intent, goals }));
    child.stdin.end();
  });
  return {
    allowed: data.is_safe_to_act === true,
    gate_status: data.status,
  };
}

test("openclaw e2e: rules file add/remove/list", async () => {
  const dir = await mkdtemp(join(tmpdir(), "openclaw-e2e-"));
  const filePath = join(dir, "rules.json");
  try {
    await addGuardRule(filePath, "never hardcode localhost");
    let goals = await loadGoalTexts(filePath);
    assert.deepEqual(goals, ["never hardcode localhost"]);

    await removeGuardRule(filePath, "never hardcode localhost");
    goals = await loadGoalTexts(filePath);
    assert.deepEqual(goals, []);
  } finally {
    invalidateRulesCache();
    await rm(dir, { recursive: true, force: true });
  }
});

test("openclaw e2e: block localhost / permit unrelated rule (direct verify() call)", async () => {
  const intent = "exec (command=curl http://localhost:8080/api/health)";

  const withLocalhost = await verifyIntent(intent, ["never hardcode localhost"]);
  assert.equal(withLocalhost.allowed, false);
  assert.equal(withLocalhost.gate_status, "TORSION");

  const unrelatedOnly = await verifyIntent(intent, ["never write lorem ipsum"]);
  assert.equal(unrelatedOnly.allowed, true);
  assert.equal(unrelatedOnly.gate_status, "ALIGNED");
});
