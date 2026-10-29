# Plugin tests

Node tests for runtime plugins under `plugins/<name>/`.

## Run

From repo root:

```bash
node --experimental-strip-types --test plugins/tests/openclaw/*.test.mjs
```

Unit tests only (no Python bridge):

```bash
node --experimental-strip-types --test \
  plugins/tests/openclaw/receipt.test.mjs \
  plugins/tests/openclaw/rules-store.test.mjs
```

`e2e-guard-doors.test.mjs` spawns `plugins/openclaw/verify_bridge.py` and calls
`pdm_memory.verify()` in-process. Needs a Python with `pdm-memory` importable
(default `python3`, override with `PDM_GUARD_PYTHON`).

`registered-hooks.test.mjs` loads the shipped JavaScript bundle, stubs only the
host's plugin-entry helper, and invokes the registered before/after hooks and
rule interfaces. It uses a local fake checker process, covering default blocks,
malformed/inconsistent responses, explicit unprotected compatibility mode,
owner-only rule changes, and concurrent result correlation. It needs Node >= 24
for the module-loader test hook, but no real Python or external service.

These tests do not establish operating-system containment, durable receipts,
or successful deployment on every OpenClaw version.

No companion_api / HTTP sidecar required.

## Layout

```
plugins/
  openclaw/          # plugin source + runtime bundle + verify_bridge.py
  tests/
    openclaw/        # tests for the OpenClaw plugin
```
