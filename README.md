# PDM — Memory for AI Apps That Works Like Memory

[![PyPI](https://img.shields.io/pypi/v/pdm-memory)](https://pypi.org/project/pdm-memory/)
[![Python](https://img.shields.io/pypi/pyversions/pdm-memory)](https://pypi.org/project/pdm-memory/)
[![License: Proprietary](https://img.shields.io/badge/License-Proprietary%20ELv2-blue)](LICENSE)
[![Documentation](https://img.shields.io/badge/Docs-azus.ai%2Fsupport-blue)](https://azus.ai/support)

Your LLM forgets everything between conversations. The standard fix — stuff a vector database into the context window — is expensive, slow, and retrieves **what matches words, not what matters**.

**PDM stores meaning signatures instead of raw text.** Memories that get used grow stronger. Memories that don't, fade. Retrieval works by resonance: the question itself surfaces what's relevant, instead of a keyword search digging for it.

- 🔑 **Your API key.** Works with your existing Anthropic/OpenAI account.
- 🗄️ **Your storage.** One local file. Your data never leaves your machine. Check the source — there's no phone-home in it.
- 🛡️ **Guarded agents.** Blocks actions that contradict stored goals. Catches contradicting facts automatically.
- ⚡ **Ten minutes.** `pip install pdm-memory` → three lines → persistent memory.

📖 **Documentation:** [azus.ai/support](https://azus.ai/support)

**Benchmarks vs standard RAG:** PDM wins **7 of 8** metrics — **15×** fewer tokens, **35×** less storage. One loss published. [See full methodology →](https://azus.ai/pdm/benchmarks)

---

## Table of Contents

0. [Documentation](https://azus.ai/support)
1. [Privacy Mode](#-privacy-mode-local-sqlite)
2. [Ecosystem Mode](#-ecosystem-mode-azus-cloud)
3. [LLM Adapters](#-llm-adapters)
4. [Guarded Agents (GAA & Torsion)](#-guarded-agents-gaa--torsion)
5. [Data Ingestion](#-data-ingestion)
6. [Developer Tools](#-developer-tools)
7. [API Reference](#-api-reference)
8. [License](#-license)

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
         tags=["units", "formatting", "preferences"], p_magnitude=85)

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

Connect to the **AZUS Companion API** so memories sync across devices and the companion app.

**Cloud endpoint:** default base URL is `https://api.azus.ai` (not the marketing site `https://azus.ai`). Staging: `https://staging.azus.ai`.

CloudDriver expects a Companion build that exposes the PDM SDK routes (list, batch, by-hash / by-idempotency-key, soft-delete). Older deploys still support single-row `ingest` / get / patch / hard delete only.

### Getting your JWT access token

AZUS Cloud uses standard JWT auth. If you already have an AZUS account, sign in and use the returned `access` token as `token="..."`.

If you do not have an account yet, create one here:

- Web signup: [azus.ai/auth/register](https://azus.ai/auth/register)
- API signup: `POST https://api.azus.ai/api/v1/accounts/auth/register/`
- API login: `POST https://api.azus.ai/api/v1/accounts/auth/login/`
- Token refresh: `POST https://api.azus.ai/api/v1/accounts/token/refresh/`

Example registration flow:

```python
import requests

r = requests.post(
    "https://api.azus.ai/api/v1/accounts/auth/register/",
    json={
        "email": "user@example.com",
        "username": "user123",
        "password": "SecurePass123!",
        "profile": {"first_name": "John", "last_name": "Doe"},
    },
)
data = r.json()

access_token = data["tokens"]["access"]
refresh_token = data["tokens"]["refresh"]
```

Use the returned JWTs directly with `Memory(store="cloud", ...)`:

### Connect to the Cloud

```python
from pdm_memory import Memory

mem = Memory(
    store="cloud",
    token="eyJ...",               # AZUS JWT access token
    refresh_token="eyJ...",       # optional — auto-refresh on 401
    cloud_url="https://api.azus.ai",  # optional; this is the default
    user="your_username",         # ownership scope for storage ops
)

# Writes use POST /api/v1/pdm/ingest; dedupe hits the by-hash API when available.
mem.save(
    "User's team is in Kyiv (UTC+3)",
    source="manual",
    tags=["location", "team", "timezone"],
    p_magnitude=70,
    dedupe=True,
)
# Exact-once create on retries: pass idempotency_key=...
hits = mem.recall("what timezone are they in?")

# Batch write (POST /pdm/ingest/batch when supported)
mem.save_many(
    [
        {"text": "Fact A", "tags": ["a", "b", "c"], "p_magnitude": 70, "source": "manual"},
        {"text": "Fact B", "tags": ["a", "b", "c"], "p_magnitude": 70, "source": "manual"},
    ],
    dedupe=False,
)
```

### Soft delete vs hard delete

- `mem.delete(id)` → soft-delete (`is_deleted`) when the API supports it; cloud list/get hide those rows.
- Permanent removal is storage-level `hard_delete` (CloudDriver), not the default Memory facade API.

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

## 🛡️ Guarded Agents (GAA & Torsion)

PDM is not only retrieval. Before an agent **acts**, `verify_alignment()` scores the proposed intent against stored stewardship goals and returns **ALIGNED**, **CONFLICT**, or **TORSION**. `detect_torsion()` finds contradicting facts already in memory.

Statuses:

- **ALIGNED** — safe to proceed (`report.is_safe_to_act` is `True`)
- **CONFLICT** — soft mismatch / missing anchors (fail-closed by default)
- **TORSION** — hard contradiction — block the ACT

```python
from pdm_memory import Memory

mem = Memory(store="./agent.db")

# Goal signatures live in stewardship / foundational drawers.
mem.save(
    "Core goal: high reliability; never ignore production errors",
    tags=["reliability", "errors", "goal", "integrity"],
    drawer="stewardship",
    p_magnitude=92,
    source="policy",
    metadata={"iaw": 0.90, "role": "goal"},
)
mem.save(
    "Foundational principle: validate before deploy",
    tags=["validation", "deploy", "principle", "quality"],
    drawer="foundational",
    p_magnitude=88,
    source="policy",
    metadata={"iaw": 0.85, "role": "goal"},
)

# Block an action that opposes stored goals.
bad = mem.verify_alignment("ignore errors and bypass validation")
print(bad.status, bad.is_safe_to_act)   # TORSION False

# Allow an action that resonates with those goals.
good = mem.verify_alignment("validate thoroughly then deploy with reliability checks")
print(good.status, good.is_safe_to_act)  # ALIGNED True

def guarded_act(mem, intent, tool_call):
    report = mem.verify_alignment(intent)
    if not report.is_safe_to_act:
        raise PermissionError(f"GAA blocked ACT: {report.status}")
    return tool_call()
```

Catch contradicting facts already in the store:

```python
from datetime import datetime, timezone

mem.save(
    "Project Orion launch date is 2026-08-01",
    tags=["orion", "launch", "deadline", "project"],
    drawer="product",
    p_magnitude=70,
    deadline=datetime(2026, 8, 1, tzinfo=timezone.utc),
    metadata={"cluster_id": "orion-launch"},
)
mem.save(
    "Project Orion launch date is 2026-09-01",
    tags=["orion", "launch", "deadline", "project"],
    drawer="product",
    p_magnitude=72,
    deadline=datetime(2026, 9, 1, tzinfo=timezone.utc),
    metadata={"cluster_id": "orion-launch"},
)

for report in mem.detect_torsion(threshold=0.5):
    print(report.torsion_score, report.conflict_kind, report.explanation)
```

Full walkthroughs: `python -m pdm_memory.examples.guarded_agent_logic` and `python -m pdm_memory.examples.handling_contradictions`.

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

### Quick Start (PyPI install)

Everything below works after `pip install pdm-memory` — no repository clone required.

```bash
pip install pdm-memory
python -m pdm_memory.examples.hello_pdm
```

Inline smoke test:

```bash
python -c "
from pdm_memory import Memory
mem = Memory(store='./demo.db')
mem.save('User prefers metric units and short answers', source='demo',
         tags=['units', 'formatting', 'preferences'], p_magnitude=85)
for h in mem.recall('how should I format the answer?', k=3):
    print(h.text, round(h.pressure, 1))
"
```

Other PyPI-shipped tools:

```bash
python -m pdm_memory.bench --quick   # smoke benchmark (5 scenarios)
pdm-cli stats --store ./demo.db      # inspect the store created above
```

### Example walkthroughs (bundled in PyPI)

All scripts below ship inside the wheel — run them with `python -m`:

```bash
pip install pdm-memory
python -m pdm_memory.examples.hello_pdm                 # save / recall / explain
python -m pdm_memory.examples.guarded_agent_logic       # GAA: TORSION vs ALIGNED
python -m pdm_memory.examples.handling_contradictions     # detect + reconcile torsion
python -m pdm_memory.examples.temporal_recall_demo        # event_at + deadline (PDM-T)
python -m pdm_memory.examples.industrial_safety_gate      # Oil Field: Auto-Discovery + heal
```

See [`pdm_memory/examples/README.md`](pdm_memory/examples/README.md).

From a source checkout, the wrappers in [`examples/`](examples/) delegate to the same modules:

```bash
pip install .
python examples/hello_pdm.py
```

To run the test suite (contributors):

```bash
pip install ".[dev]"
pytest
```

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

### `Memory(store, user, token, refresh_token, cloud_url, store_raw)`

| Method | Description |
|--------|-------------|
| `save(text, source, tags, p_magnitude, t_persistence, drawer, regime, deadline, dedupe=True, idempotency_key=None)` | Store a memory (content dedupe and/or idempotency when storage supports it) |
| `save_many(items, dedupe=True)` → `dict` | Batch save; returns `{saved, skipped, errors}` |
| `recall(query, k, min_pressure, search_cost, drawer, reinforce)` → `List[MemoryHit]` | Retrieve top-k relevant memories |
| `verify_alignment(intent_text, min_pressure, k_goals, torsion_threshold)` → `AlignmentReport` | GAA gate before an agent ACT (`ALIGNED` / `CONFLICT` / `TORSION`) |
| `detect_torsion(drawer, threshold)` → `List[TorsionReport]` | Find contradicting facts (Reverse Resonance) |
| `reinforce(memory_id, coupling_score)` | Manually raise a memory's pressure (and V-counters where supported) |
| `delete(memory_id)` → `bool` | Soft-delete when storage supports it |
| `decay(dry_run)` → `dict` | Trigger decay pass (runs automatically on recall) |
| `explain(memory_id, query)` → `ExplainReport` | Show why a memory has its current pressure |
| `list(limit, min_pressure, drawer, cursor_id)` → `MemoryListPage` | Keyset page of memories (storage list API on cloud) |
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
    def save_batch(self, sigs): ...
    def get(self, memory_id, user): ...
    def get_many(self, ids, user): ...
    def update(self, memory_id, **fields): ...
    def update_batch(self, updates, user): ...
    def delete(self, memory_id, user): ...       # soft-delete when supported
    def hard_delete(self, memory_id, user): ...
    def list(self, user, limit, min_pressure, drawer, cursor_id=None, include_deleted=False): ...
    def list_drawers(self, user): ...
    def find_by_idempotency_key(self, key, user): ...
    def find_by_hash(self, text_hash, user): ...
    def ping(self): ...

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
