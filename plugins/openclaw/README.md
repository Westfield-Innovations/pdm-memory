# PDM Guard - OpenClaw Plugin

GAA gate for OpenClaw via `before_tool_call`.
Calls `pdm-memory verify()` through `companion_api` before each tool execution.

## Files

- `pdm-guard.ts` - source version
- `pdm-guard.js` - runtime file loaded by OpenClaw
- `openclaw.plugin.json` - plugin metadata and config schema

## Config

`~/.openclaw/openclaw.json`

```json5
{
  plugins: {
    load: {
      paths: ["/Users/admin/azus/pdm-memory/plugins/openclaw/pdm-guard.js"]
    },
    entries: {
      "pdm-guard": {
        enabled: true,
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

## Behavior

- fail-open if `goals` are empty
- fail-open if `verifyUrl` is unreachable
- block when `verify()` returns:
  - `TORSION`
  - `CONFLICT` with non-empty `conflicting_goals`

## Notes

The plugin builds a richer intent string for tools like `exec`, `apply_patch`,
and other payload-heavy calls by preferring fields such as `command`, `input`,
`content`, `text`, `patch`, and `script`.
