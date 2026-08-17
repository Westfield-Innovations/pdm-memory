# PDM SDK Examples

Walkthrough scripts ship inside the PyPI package.

```bash
pip install pdm-memory
python -m pdm_memory.examples.hello_pdm
python -m pdm_memory.examples.standalone_guard
python -m pdm_memory.examples.guarded_agent_logic
python -m pdm_memory.examples.handling_contradictions
python -m pdm_memory.examples.temporal_recall_demo
python -m pdm_memory.examples.industrial_safety_gate
```

From a source checkout you can also run the wrappers in this directory:

```bash
pip install .
python examples/hello_pdm.py
```

See [`pdm_memory/examples/README.md`](../pdm_memory/examples/README.md) for the full script guide.
