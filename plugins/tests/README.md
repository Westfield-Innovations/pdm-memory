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

No companion_api / HTTP sidecar required.

## Layout

```
plugins/
  openclaw/          # plugin source + runtime bundle + verify_bridge.py
  tests/
    openclaw/        # tests for the OpenClaw plugin
```
