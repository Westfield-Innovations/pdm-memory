# PDM SDK Examples

Standalone scripts that teach Westfield Pressure, Resonance, and Torsion.

```bash
pip install .
python examples/hello_pdm.py
python examples/guarded_agent_logic.py
python examples/handling_contradictions.py
python examples/temporal_recall_demo.py
python examples/industrial_safety_gate.py
python examples/proactive_alert_demo.py
```

| Script | Teaches |
|--------|---------|
| `hello_pdm.py` | `save` / `recall` / `explain` — Pressure + Resonance |
| `guarded_agent_logic.py` | `verify_alignment` (GAA) — TORSION vs ALIGNED before ACT |
| `handling_contradictions.py` | `detect_torsion` / `reconcile_torsion` — self-healing |
| `temporal_recall_demo.py` | `event_at` / `deadline` (PDM-T) + `search_cost` window |
| `industrial_safety_gate.py` | Oil Field Blueprint — Auto-Discovery, GAA block, `audit_and_heal` |
| `proactive_alert_demo.py` | Observer plugin — high-P / hot-tag alerts on `post_save` |
| `pdm-memory-plugin-echo/` | External plugin scaffold (`plugin.json` + entrypoint) |

Plugin authoring: [docs/PLUGIN_AUTHORING.md](../docs/PLUGIN_AUTHORING.md).

External plugins need `plugin_allowlist=[...]` (recommended) — default is Fail Closed.
`trust_plugins=True` is deprecated (cwd children only).

Each script uses `with Memory(...) as mem:` and a fresh temp SQLite file.
