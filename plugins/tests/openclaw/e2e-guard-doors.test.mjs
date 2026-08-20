/**
 * E2E for OpenClaw plugin path (Part 2 spike semantics).
 * Rules file + verify sidecar → block/permit decisions (same as plugin).
 */

import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import test from "node:test";

import {
  addGuardRule,
  invalidateRulesCache,
  loadGoalTexts,
  removeGuardRule,
} from "../../openclaw/rules-store.ts";

const VERIFY_URL = "http://localhost:8000/api/v1/pdm/gaa/verify/";

async function verifyIntent(intent, goals) {
  const resp = await fetch(VERIFY_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ intent, goals }),
  });
  assert.equal(resp.status, 200);
  const data = await resp.json();
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

test("openclaw e2e: block localhost / permit unrelated rule (verify API)", async () => {
  const intent = "exec (command=curl http://localhost:8080/api/health)";

  const withLocalhost = await verifyIntent(intent, ["never hardcode localhost"]);
  assert.equal(withLocalhost.allowed, false);
  assert.equal(withLocalhost.gate_status, "TORSION");

  const unrelatedOnly = await verifyIntent(intent, ["never write lorem ipsum"]);
  assert.equal(unrelatedOnly.allowed, true);
  assert.equal(unrelatedOnly.gate_status, "ALIGNED");
});
