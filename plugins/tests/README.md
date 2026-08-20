# Plugin tests

Node tests for runtime plugins under `plugins/<name>/`.

## Run

From repo root:

```bash
node --test plugins/tests/openclaw/*.test.mjs
```

Unit tests only (no live verify sidecar):

```bash
node --test plugins/tests/openclaw/receipt.test.mjs plugins/tests/openclaw/rules-store.test.mjs
```

`e2e-guard-doors.test.mjs` expects a running verify endpoint at
`http://localhost:8000/api/v1/pdm/gaa/verify/` (companion_api or local bridge).

## Layout

```
plugins/
  openclaw/          # plugin source + runtime bundle
  tests/
    openclaw/        # tests for the OpenClaw plugin
```
