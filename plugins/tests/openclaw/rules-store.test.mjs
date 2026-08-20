import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import test from "node:test";

import {
  addGuardRule,
  goalTextsFromFile,
  invalidateRulesCache,
  loadGoalTexts,
  removeGuardRule,
} from "../../openclaw/rules-store.ts";

test("add dedupes identical normalized rules", async () => {
  const dir = await mkdtemp(join(tmpdir(), "pdm-guard-"));
  const filePath = join(dir, "rules.json");
  try {
    const first = await addGuardRule(filePath, "never   hardcode localhost");
    const second = await addGuardRule(filePath, "never hardcode localhost");

    assert.equal(first.created, true);
    assert.equal(first.deduped, false);
    assert.equal(second.created, false);
    assert.equal(second.deduped, true);
    assert.equal(second.file.rules.length, 1);

    const goals = await loadGoalTexts(filePath);
    assert.deepEqual(goals, ["never hardcode localhost"]);
  } finally {
    invalidateRulesCache();
    await rm(dir, { recursive: true, force: true });
  }
});

test("remove by text deletes rule", async () => {
  const dir = await mkdtemp(join(tmpdir(), "pdm-guard-"));
  const filePath = join(dir, "rules.json");
  try {
    await addGuardRule(filePath, "never write lorem ipsum");
    const removed = await removeGuardRule(filePath, "never write lorem ipsum");

    assert.ok(removed.removed);
    assert.equal(removed.file.rules.length, 0);
    assert.deepEqual(goalTextsFromFile(removed.file), []);
  } finally {
    invalidateRulesCache();
    await rm(dir, { recursive: true, force: true });
  }
});
