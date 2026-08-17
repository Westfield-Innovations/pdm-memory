# PDM SDK Examples

Standalone scripts that teach Westfield Pressure, Resonance, and Torsion. They ship inside the PyPI wheel.

```bash
pip install pdm-memory
python -m pdm_memory.examples.hello_pdm
python -m pdm_memory.examples.standalone_guard
python -m pdm_memory.examples.guarded_agent_logic
python -m pdm_memory.examples.handling_contradictions
python -m pdm_memory.examples.temporal_recall_demo
python -m pdm_memory.examples.industrial_safety_gate
```

From a source checkout you can also use the repo wrappers:

```bash
pip install .
python examples/hello_pdm.py
```

| Script | Teaches |
|--------|---------|
| `hello_pdm.py` | `save` / `recall` / `explain` — Pressure + Resonance |
| `standalone_guard.py` | `verify(intent, goals)` — GAA with no store |
| `guarded_agent_logic.py` | `verify_alignment` (GAA) — TORSION vs ALIGNED before ACT |
| `handling_contradictions.py` | `detect_torsion` / `reconcile_torsion` — self-healing |
| `temporal_recall_demo.py` | `event_at` / `deadline` (PDM-T) + `search_cost` window |
| `industrial_safety_gate.py` | Oil Field Blueprint — Auto-Discovery, GAA block, `audit_and_heal` |

`standalone_guard.py` takes a plain list of rules. The other scripts use `with Memory(...) as mem:` and a fresh temp SQLite file.
