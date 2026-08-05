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

After `mem.use(...)`, `plugin.mem` is a **`PluginMemoryProxy`**:

| Allowed | Denied |
|---------|--------|
| `save`, `recall`, `get`, `list`, `delete`, … | `close`, `_storage`, `use`, `unload` |
| peer plugins (`mem.GeoTagger`) | private attrs (`_*`) |

Escape hatch for tests only: `plugin.mem.unwrap()`.

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
  "requires": [],
  "autoload": true
}
```

Place the folder as a sibling of your app (or any parent of cwd). Discovery walks cwd → root.

### Trust (Fail Closed)

External plugins are **arbitrary code**. Defaults:

```python
Memory(store="./app.db")  # trust_plugins=False → external dirs ignored
```

Opt in:

```python
Memory(store="./app.db", trust_plugins=True)

# or path allowlist (absolute or parent of the plugin dir)
Memory(
    store="./app.db",
    plugin_allowlist=["/abs/path/to/pdm-memory-plugin-echo"],
)
```

Env override (same as `trust_plugins=True`): `PDM_TRUST_PLUGINS=1`.

## Hooks & priority

```python
class Auditor(BasePDMPlugin):
    name = "auditor"
    priority = 100  # default

class GuardDog(BasePDMPlugin):
    name = "guard_dog"
    priority = 10   # runs before Auditor
```

Hook events: `pre_save`, `post_save`, `post_recall`. Integrity veto: return `None`/`False` or raise `IntegrityBlock`.

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
