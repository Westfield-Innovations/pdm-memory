# PDM — Memory for AI Apps That Works Like Memory

[![PyPI](https://img.shields.io/pypi/v/pdm-memory)](https://pypi.org/project/pdm-memory/)
[![Python](https://img.shields.io/pypi/pyversions/pdm-memory)](https://pypi.org/project/pdm-memory/)
[![CI](https://github.com/westfield-innovations/pdm-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/westfield-innovations/pdm-memory/actions)
[![License: Proprietary](https://img.shields.io/badge/License-Proprietary%20ELv2-blue)](LICENSE)
[![Documentation](https://img.shields.io/badge/Docs-azus.ai%2Fsupport-blue)](https://azus.ai/support)

Your LLM forgets everything between conversations. The standard fix — stuff a vector database into the context window — is expensive, slow, and retrieves **what matches words, not what matters**.

**PDM stores meaning signatures instead of raw text.** Memories that get used grow stronger. Memories that don't, fade. Retrieval works by resonance: the question itself surfaces what's relevant, instead of a keyword search digging for it.

- 🔑 **Your API key.** Works with your existing Anthropic/OpenAI account.
- 🗄️ **Your storage.** One local file. Your data never leaves your machine. Check the source — there's no phone-home in it.
- ⚡ **Ten minutes.** `pip install pdm-memory` → three lines → persistent memory.

📖 **Documentation:** [azus.ai/support](https://azus.ai/support)

**Benchmarks vs standard RAG:** [azus.ai/pdm/benchmarks](https://azus.ai/pdm/benchmarks)

---

## Table of Contents

0. [Documentation](https://azus.ai/support)
1. [Privacy Mode](#-privacy-mode-local-sqlite)
2. [Ecosystem Mode](#-ecosystem-mode-azus-cloud)
3. [LLM Adapters](#-llm-adapters)
4. [Data Ingestion](#-data-ingestion)
5. [Developer Tools](#-developer-tools)
6. [API Reference](#-api-reference)
7. [License](#-license)

---

## 🔒 Privacy Mode (Local SQLite)

Zero setup. No network calls. Your data stays in a single file on your machine.

### Install

```bash
pip install pdm-memory
# With OpenAI support:
pip install "pdm-memory[openai]"
# With Anthropic support:
pip install "pdm-memory[anthropic]"
# Everything:
pip install "pdm-memory[all]"
```

### Quick Start

```python
from pdm_memory import Memory

# One line to start. The .db file is created automatically.
mem = Memory(store="./my_app_memory.db")

# Write: PDM assigns pressure and stores a signature.
mem.save("User prefers metric units and short answers", source="chat",
         tags=["units", "formatting", "preferences"])

# Read: resonance retrieval — surfaces what's relevant, not just what matches.
hits = mem.recall("how should I format the answer?", k=5)

for h in hits:
    print(h.text, h.pressure, h.last_reinforced)

# Reinforce a memory manually (recall() does this automatically).
mem.reinforce(hits[0].id)

# Inspect why a memory surfaced.
report = mem.explain(hits[0].id, query="how should I format the answer?")
print(report.render())

# Decay runs automatically on each recall(). Manual trigger:
counts = mem.decay()
print(f"Decayed: {counts['decayed']}, Deleted: {counts['deleted']}")
```

### Privacy-First Mode

Store only SHA-256 hashes of memory text — the content never touches disk:

```python
mem = Memory(store="./private.db", store_raw=False)
```

---

## ☁️ Ecosystem Mode (AZUS Cloud)

Connect to the AZUS Companion API to sync memories across devices and share them with the AI companion.

### Connect to the Cloud

```python
from pdm_memory import Memory

mem = Memory(
    store="cloud",
    token="eyJ...",               # Your AZUS JWT access token
    cloud_url="https://api.azus.ai",
)

# All save/recall operations go to the cloud.
mem.save("User's team is in Kyiv (UTC+3)", tags=["location", "team", "timezone"])
hits = mem.recall("what timezone are they in?")
```

### Sync Local ↔ Cloud

```python
# Start with a local store
local_mem = Memory(store="./local.db")
local_mem.save("Local preference", tags=["pref", "local", "test"])

# Push local memories to cloud
report = local_mem.sync(
    direction="push",
    token="eyJ...",
    cloud_url="https://api.azus.ai",
)
print(report)   # SyncReport(pushed=1, pulled=0, conflicts=0, errors=0)

# Pull cloud memories to local
report = local_mem.sync(direction="pull", token="eyJ...")

# Two-way sync (higher pressure wins on conflict)
report = local_mem.sync(direction="bidirectional", token="eyJ...")
```

### JWT Token Handling

```python
from pdm_memory.auth import JWTAuth

# Tokens are refreshed automatically when they expire
auth = JWTAuth(
    token="eyJ...",
    refresh_token="eyJ...",
    refresh_url="https://api.azus.ai/api/v1/accounts/token/refresh/",
)
```

---

## 🤖 LLM Adapters

The wrapper is the demo; the primitives are the product. Most developers start here.

### OpenAI

```python
from pdm_memory import Memory
from pdm_memory.integrations import wrap_openai

mem = Memory(store="./my_app.db")
client = wrap_openai(api_key="sk-...", memory=mem)

# Memory is handled completely invisibly:
# - Before the call: relevant memories are injected into the system prompt
# - After the call: user message + AI reply are saved to memory
reply = client.chat("What units should I use?")
print(reply)
```

### Anthropic

```python
from pdm_memory.integrations import wrap_anthropic

client = wrap_anthropic(api_key="sk-ant-...", memory=mem)
reply = client.chat("What units should I use?")
```

### Manual Control

```python
from pdm_memory.integrations import ContextWindowManager

# Control exactly what goes into context
manager = ContextWindowManager(max_tokens=1500, model="gpt-4o")
hits = mem.recall("user's formatting preferences", k=10)
trimmed = manager.fit(hits)                    # Drop lowest-pressure memories first
system_block = manager.format_for_prompt(trimmed)
print(system_block)
```

---

## 📥 Data Ingestion

### Import Legacy Data

```python
# From a list of dicts
mem.ingest(
    data_source=[
        {"text": "User hates Comic Sans", "importance": 85},
        {"content": "Team deploys on Fridays — bad idea", "labels": "devops,process,risk"},
    ],
    mapping={"text": "compressed_fact", "importance": "p_magnitude"},
)

# From a CSV file (auto-detects common column names)
mem.ingest("./old_chat_logs.csv")

# With progress tracking
def on_progress(processed, total):
    print(f"{processed}/{total} records processed")

mem.ingest("./large_dataset.csv", on_progress=on_progress)
```

### Auto-Generate Signatures with an LLM

```python
import openai
client = openai.OpenAI(api_key="sk-...")

# LLM will compress raw text → compressed_fact + 3 tags + p_magnitude
mem.ingest(
    data_source=["User complains about slow API responses every Monday morning"],
    llm_client=client,
)
```

### Batch Processing (Large Datasets)

```python
# 10,000 records processed in batches of 50, with rate limiting
mem.ingest(
    data_source="./10k_records.csv",
    batch_size=50,
)
```

---

## 🛠️ Developer Tools

### Examples (DX walkthrough)

```bash
pip install .
python examples/hello_pdm.py                 # save / recall / explain
python examples/guarded_agent_logic.py       # GAA: TORSION vs ALIGNED
python examples/handling_contradictions.py   # detect + reconcile torsion
python examples/temporal_recall_demo.py      # deadline (PDM-T) + search_cost
```

See [`examples/README.md`](examples/README.md).

### The explain Method

```python
report = mem.explain(memory_id, query="how should I format this?")
print(report.render())
```

```
╔══════════════════════════════════════════════════════
║  PDM Memory Explain Report
╠══════════════════════════════════════════════════════
║  ID:              abc12345-...
║  Fact:            User prefers metric units and short answers
║  Tags:            units, formatting, preferences
╠──────────────────────────────────────────────────────
║  Pressure Components:
║    p_magnitude:    80.00
║    V coefficient:  0.8333  (4 retrievals)
║    Decay factor:   0.0231  (1.0d since retrieved, T½=30d)
║    Intent weight:  1.0000
║    Quality:        0.80
║    ─────────────────────────────
║    P_effective:    55.28
╠──────────────────────────────────────────────────────
║  Resonance (TAS coupling):
║    coupling_score:     0.8750
║    tag_overlap:        1.0000
║    domain_match:       1.0000
╚══════════════════════════════════════════════════════
```

### Benchmark Harness

```bash
# Run full benchmark (PDM vs keyword+recency baseline)
python -m pdm_memory.bench

# Quick smoke test (5 scenarios)
python -m pdm_memory.bench --quick

# Save results as JSON
python -m pdm_memory.bench --output results.json
```

### CLI Tool

```bash
# List all memories
pdm-cli list-memories --store ./my_app.db

# Filter by pressure
pdm-cli list-memories --store ./my_app.db --min-pressure 60

# Explain a specific memory
pdm-cli explain abc12345 --store ./my_app.db --query "formatting"

# Trigger a decay pass (dry run first)
pdm-cli decay --store ./my_app.db --dry-run
pdm-cli decay --store ./my_app.db

# Show stats
pdm-cli stats --store ./my_app.db

# List drawers (categories)
pdm-cli drawers --store ./my_app.db

# Sync to cloud
pdm-cli sync --store ./my_app.db --token eyJ... --direction push

# Launch visual dashboard (requires: pip install "pdm-memory[ui]")
pdm-cli ui --store ./my_app.db --port 8080
```

### PDM Explorer (Visual Dashboard)

```bash
pip install "pdm-memory[ui]"
pdm-cli ui --store ./local.db --port 8080
```

Opens `http://localhost:8080` with a D3 force graph:

- **Node size** ∝ live `P_effective` (decay made visible)
- **Edges** = high tag resonance
- **Red glow** = torsion conflict on that signature

API endpoints used by the UI: `GET /api/v1/memory-map`, `GET /api/v1/torsion`.

---

## 📖 API Reference

### `Memory(store, user, token, cloud_url, store_raw)`

| Method | Description |
|--------|-------------|
| `save(text, source, tags, p_magnitude, t_persistence, drawer, regime, deadline)` | Store a new memory |
| `recall(query, k, min_pressure, search_cost, drawer, reinforce)` → `List[MemoryHit]` | Retrieve top-k relevant memories |
| `reinforce(memory_id, coupling_score)` | Manually raise a memory's pressure |
| `decay(dry_run)` → `dict` | Trigger decay pass (runs automatically on recall) |
| `explain(memory_id, query)` → `ExplainReport` | Show why a memory has its current pressure |
| `sync(direction, token, cloud_url)` → `SyncReport` | Sync local ↔ cloud |
| `ingest(data_source, mapping, llm_client, batch_size)` → `dict` | Import legacy data |
| `list_drawers()` → `List[DrawerInfo]` | List memory categories |
| `count()` → `int` | Total memory count |
| `close()` | Release storage connections |

### `MemoryHit`

| Field | Description |
|-------|-------------|
| `id` | UUID |
| `text` | Memory content |
| `pressure` | Live P_effective at retrieval time |
| `p_raw` | Stored p_magnitude |
| `intent_tags` | Classification tags |
| `coupling_score` | TAS resonance score (0–1) |
| `last_reinforced` | Last retrieval datetime |

---

## 🔬 How PDM Works

**Pressure** — every memory has a p_magnitude (0–100). Important, frequently-used memories stay strong. Unused ones decay. You control the baseline; the system adjusts dynamically.

**Decay** — computed at recall time based on elapsed days vs. domain-specific half-lives. No scheduler required (Celery-free). Market signals decay in 1 day; core facts persist for a year.

**Retrieval (TAS)** — Threshold-Adjustment Search lowers the pressure threshold based on query uncertainty (search_cost). Then coupling scores rank memories by tag overlap, domain, regime, and pressure proximity. The most resonant memories surface first.

**Validation Coefficient (V)** — Laplace-smoothed accuracy tracker. Memories that prove predictively useful grow stronger; ones that mislead decay faster.

---

## 🏗️ Custom Storage Backend

Implement `BaseStorage` to add your own backend (Postgres, Redis, DynamoDB…):

```python
from pdm_memory.storage.base import BaseStorage

class MyPostgresStorage(BaseStorage):
    def save(self, sig): ...
    def get(self, memory_id, user): ...
    def update(self, memory_id, **fields): ...
    def delete(self, memory_id, user): ...
    def list(self, user, limit, min_pressure, drawer): ...
    def list_drawers(self, user): ...

mem = Memory.__new__(Memory)
mem._storage = MyPostgresStorage(...)
mem._user = "alice"
mem._engine = RetrievalEngine()
```

---

## 📄 License

Free to use **as-shipped** under a custom [Elastic License 2.0 (ELv2)](LICENSE)
base from **Westfield Innovations LLC**.

- Use the SDK as distributed: yes.
- Modify / fork / redistribute altered **core** logic: **no**, without a
  commercial license from Westfield Innovations LLC.
- Extensions via defined plugin interfaces (e.g. `BaseStorage`): **permitted**.

**Patent Pending** — U.S. App. No. **19/739,419** · **63/953,563** · **63/953,842**.

If this software makes you money, send Carl a birthday card. He collects them.

Built by **Westfield Innovations LLC** · [azus.ai](https://azus.ai) · [getdeepsignals.com](https://getdeepsignals.com)
