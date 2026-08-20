# PDM Guard — OpenClaw Plugin

GAA gate for OpenClaw via `before_tool_call`. Calls `pdm-memory verify()` through `companion_api` before each tool execution.

## Files

- `pdm-guard.ts` — source
- `pdm-guard.js` — runtime file loaded by OpenClaw (rebuild with esbuild after editing `.ts`)
- `openclaw.plugin.json` — manifest (`activation.onCapabilities: ["hook"]` required)
- `SPIKE_DEBUG_POSTMORTEM.md` — debugging notes from spike

## Config

`~/.openclaw/openclaw.json`:

```json5
{
  plugins: {
    load: {
      paths: ["/path/to/pdm-memory/plugins/openclaw/pdm-guard.js"]
    },
    entries: {
      "pdm-guard": {
        enabled: true,
        hooks: {
          allowConversationAccess: true
        },
        config: {
          verifyUrl: "http://localhost:8000/api/v1/pdm/gaa/verify/",
          goals: [
            "never write lorem ipsum",
            "never hardcode localhost",
            "never leave TODO unresolved"
          ],
          timeoutMs: 8000
        }
      }
    }
  }
}
```

Optional env overrides: `PDM_GUARD_URL`, `PDM_GUARD_GOALS` (pipe-separated).

## Behavior

- **Fail-open** if `goals` are empty
- **Fail-open** if `verifyUrl` is unreachable
- **Block** when `verify()` returns `is_safe_to_act: false`

Receipts are logged as `[PDM GUARD] RECEIPT: {...}` in the OpenClaw gateway log.

## Rebuild `.js` from `.ts`

```bash
cd pdm-memory/plugins/openclaw
npx esbuild pdm-guard.ts --bundle --platform=node --format=esm \
  --outfile=pdm-guard.js --external:openclaw --external:"openclaw/*"
openclaw daemon restart
```

## Notes

Intent strings prefer rich tool payload fields (`command`, `input`, `content`, `text`, `patch`, `script`) so exec/write calls carry enough context for `verify()`.

Plugin config is read from `api.pluginConfig` at `register()` time (typed hooks do not populate `ctx.pluginConfig`).
