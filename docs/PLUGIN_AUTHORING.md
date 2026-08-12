# Plugin Authoring Guide

Authorized extension surface for **pdm-memory**. Implement `BasePDMPlugin`; do not fork core.

## Quick path (in-process)

```python
from pdm_memory import BasePDMPlugin, Memory

class GuardDog(BasePDMPlugin):
    name = "guard_dog"
    version = "1.0.0"
    priority = 10  # lower runs earlier on hooks
    requires: list[str] = []

    def __init__(self) -> None:
        super().__init__()
        self.hooks = {"pre_save": self._gate}

    def _gate(self, sig):
        if "forbidden" in (sig.text or "").lower():
            return None  # Integrity veto
        return sig

    def on_install(self) -> None:
        # self.mem is a PluginMemoryProxy — not raw Memory
        self.plugin_save("guard dog online", tags=["plugin", "guard", "boot"])

mem = Memory(store="./app.db", autoload_plugins=False)
mem.use(GuardDog())
```

## Capability proxy

After `mem.use(...)`, `plugin.mem` is a **`PluginMemoryProxy`**. Access is gated by
`capabilities` on the plugin class (fail closed):

```python
from pdm_memory.plugins.proxy import (
    CAP_ADMIN_IO,
    CAP_PEER,
    CAP_READ,
    CAP_RECALL,
    CAP_WRITE,
    DEFAULT_CAPABILITIES,
)

class DumpPlugin(BasePDMPlugin):
    name = "dumper"
    # Default = read | write | recall (no dump/inject, no peer calls)
    capabilities = DEFAULT_CAPABILITIES | {CAP_ADMIN_IO, CAP_PEER}
```

| Capability | Unlocks |
|------------|---------|
| `read` | `get`, `list`, `list_drawers`, `count`, `status`, `explain`, … |
| `write` | `save`, `save_many`, `update*`, `delete`, `reinforce`, `penalize`, … |
| `recall` | `recall` |
| `admin_io` | `export_json`, `export_csv`, `import_json` (opt-in) |
| `peer` | call **any** installed plugin via `self.mem.<PeerName>` |
| *(declared `requires`)* | call listed dependency plugins without `peer` |

Always denied: `close`, `_storage`, `use`, `unload`, private `_*`.

There is **no** `unwrap()` on the proxy. Tests/SDK internals may use `as_memory(plugin.mem)`.

Plugin-private data: drawer `plugin:<name>` via `plugin_save` / `plugin_recall` / `plugin_list`.

## External package layout

```
pdm-memory-plugin-echo/
  plugin.json          # required
  echo_plugin.py       # entrypoint module
```

`plugin.json`:

```json
{
  "name": "echo",
  "version": "0.1.0",
  "entrypoint": "echo_plugin:EchoPlugin",
  "entrypoint_sha256": "<sha256 of echo_plugin.py>",
  "requires": [],
  "autoload": true
}
```

Pin `entrypoint_sha256` (64 hex chars, optional `sha256:` prefix). Missing pin → warning;
mismatch → fail fast. Entrypoint is loaded **without** mutating `sys.path` — keep the
plugin in one file or depend on installed packages only.

### Trust (Fail Closed)

External plugins are **arbitrary code**. Default: ignored.

**Recommended** — path allowlist (exact plugin dir or parent):

```python
Memory(
    store="./app.db",
    plugin_allowlist=["/abs/path/to/pdm-memory-plugin-echo"],
)
```

**Deprecated** — `trust_plugins=True` / `PDM_TRUST_PLUGINS=1`:
loads only `pdm-memory-plugin-*` that are **direct children of cwd** (no parent walk).
Emits `DeprecationWarning`. Prefer allowlist.

When both are set, **allowlist is authoritative**.
## Hooks & priority

```python
class Auditor(BasePDMPlugin):
    name = "auditor"
    priority = 100  # default

class GuardDog(BasePDMPlugin):
    name = "guard_dog"
    priority = 10   # runs before Auditor
```

Hook events: `pre_save`, `post_save`, `post_recall`.

- `pre_save`: Integrity veto — return `None`/`False` or raise `IntegrityBlock`. Exceptions propagate.
- `post_save` / `post_recall`: **exceptions are isolated** (logged); later hooks and the
  caller still proceed. Never put required transaction logic only in `post_*`.
- `post_recall` receives :class:`~pdm_memory.types.PostRecallContext`
  (`ctx.query` or `ctx["query"]`; `ctx.source` is `"recall"` or `"surface"`).

## Requirements

```python
class GeoEnricher(BasePDMPlugin):
    name = "geo_enricher"
    requires = ["GeoTagger>=1.2"]
```

Install deps first (`mem.use` fails fast if unmet).

## Status

```python
print(mem.status())
# Source: builtin | external:/abs/path | manual
```

## Scaffold

See `examples/pdm-memory-plugin-echo/` for a copy-paste external plugin.
