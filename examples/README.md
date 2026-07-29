# PDM SDK Examples

Standalone scripts that teach Westfield Pressure, Resonance, and Torsion.

```bash
pip install .
python examples/hello_pdm.py
python examples/guarded_agent_logic.py
python examples/handling_contradictions.py
python examples/temporal_recall_demo.py
```

| Script | Teaches |
|--------|---------|
| `hello_pdm.py` | `save` / `recall` / `explain` — Pressure + Resonance |
| `guarded_agent_logic.py` | `verify_alignment` (GAA) — TORSION vs ALIGNED before ACT |
| `handling_contradictions.py` | `detect_torsion` / `reconcile_torsion` — self-healing |
| `temporal_recall_demo.py` | `deadline` (PDM-T) + `search_cost` window |

Each script uses `with Memory(...) as mem:` and a fresh temp SQLite file.
