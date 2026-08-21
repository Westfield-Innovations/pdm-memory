import assert from "node:assert/strict";
import test from "node:test";

import { buildIntentText } from "../../openclaw/intent.ts";

test("write: path=basename only, no file= label", () => {
  const huge = "DB_HOST=x\n".repeat(200);
  const intent = buildIntentText("write", {
    path: "/Users/admin/.openclaw/workspace/config.py",
    content: huge,
  });

  assert.equal(intent.startsWith("write path=config.py content≈"), true);
  assert.equal(intent.includes("file="), false);
  assert.equal(intent.includes("fullpath="), false);
  assert.ok(intent.length <= 1000);
});

test("write: relative path is only path=", () => {
  const intent = buildIntentText("write", {
    path: "config.py",
    content: 'DB_HOST = "127.0.0.1"',
  });
  assert.equal(
    intent,
    'write path=config.py content≈DB_HOST = "127.0.0.1"',
  );
});

test("exec: command is primary", () => {
  const intent = buildIntentText("exec", {
    command: "curl http://localhost:8080/api/health",
    content: "ignored when command present",
  });
  assert.equal(
    intent,
    "exec command=curl http://localhost:8080/api/health",
  );
});

test("empty params → tool name only", () => {
  assert.equal(buildIntentText("read", {}), "read");
});
