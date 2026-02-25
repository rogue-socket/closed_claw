# Quickstart

Fastest path to running Closed Claw locally.

---

## 1) Environment Setup

The venv is named `closed_claw` and lives inside the workspace root (alongside the source package of the same name).

```powershell
# Windows (PowerShell) — venv already created
.\closed_claw\Scripts\Activate.ps1

# If you need to recreate it:
py -3.11 -m venv closed_claw --without-pip
.\closed_claw\Scripts\python.exe -m ensurepip --upgrade
```

```bash
# macOS / Linux
python3.11 -m venv closed_claw
source closed_claw/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## 2) Configure

```powershell
copy .env.example .env   # Windows
# cp .env.example .env  # macOS/Linux
```

Edit `.env` and set `SIEMENS_API_KEY` (the default provider is `siemens`). For other providers, change `CLOSED_CLAW_LLM_PROVIDER` and set the matching API key.

Or use the interactive setup wizard:

```bash
python -m closed_claw.cli setup
```

The wizard prompts for provider → model → API key → runs a live verification → saves `.env`.

---

## 3) Initialize

```bash
python -m closed_claw.cli init
python -m closed_claw.cli doctor
```

`doctor` should report `langgraph_ok: true`. `sqlite_vec_ok: true` is optional but enables semantic agent search.

---

## 4) Run a Task

### Interactive menu (recommended for first use)

```bash
python -m closed_claw.cli
```

Opens a Rich menu with setup / init / doctor / run / inspect options.

### Direct run

```bash
python -m closed_claw.cli run "summarize the files in this folder"
```

### Non-interactive (scripts / CI)

```bash
python -m closed_claw.cli run "your task here" \
  --create-approval-mode approve \
  --api-approval-mode approve
```

### With explicit provider / model override

```powershell
# Siemens (default)
python -m closed_claw.cli run "your task here" `
  --create-approval-mode approve `
  --api-approval-mode approve `
  --llm-provider siemens `
  --llm-model qwen3-30b-a3b-instruct-2507

# OpenAI
python -m closed_claw.cli run "your task here" `
  --create-approval-mode approve `
  --api-approval-mode approve `
  --llm-provider openai `
  --llm-model gpt-4o-mini
```

---

## 5) Inspect Results

```bash
# List agents created so far
python -m closed_claw.cli agents --limit 20

# Full detail on one agent (manifest + skill.md + memory)
python -m closed_claw.cli agent <agent_id>

# Run history
python -m closed_claw.cli runs --limit 20

# Audit trail
python -m closed_claw.cli audit --limit 20

# Live JSONL events for a specific run
python -m closed_claw.cli runlog <run_id> --tail 200

# Supported tools
python -m closed_claw.cli tools

# Tools for a specific agent
python -m closed_claw.cli tools --agent-id <agent_id>
```

---

## 6) Manage Agents

```bash
# Delete one agent
python -m closed_claw.cli delete-agent <agent_id>
python -m closed_claw.cli delete-agent <agent_id> --yes   # skip confirmation

# Delete all agents (reset)
python -m closed_claw.cli delete-all-agents
python -m closed_claw.cli delete-all-agents --yes
```

---

## 7) Run Tests

```bash
pytest -q
```

---

## Common Fixes

### sqlite-vec not loading

```bash
export SQLITE_VEC_PATH="$(python -c 'import sqlite_vec; print(sqlite_vec.loadable_path())')"
python -m closed_claw.cli doctor
```

Or disable the requirement entirely:

```bash
CLOSED_CLAW_REQUIRE_SQLITE_VEC=false python -m closed_claw.cli doctor
```

### Slow startup (sentence-transformers model download)

Disable neural embeddings (uses zero-vector instead — still works, no semantic search):

```bash
export CLOSED_CLAW_ENABLE_SENTENCE_TRANSFORMERS=false
```

### Interactive prompt blocks automation

Override approval policy:

```bash
--create-approval-mode approve --api-approval-mode approve
```

### Organizing files in a folder

```bash
python -m closed_claw.cli run "organize files in /absolute/path/to/folder by file type" \
  --create-approval-mode approve \
  --api-approval-mode approve
```

The agent executes this using `file_io` and `terminal` tools from its `tools_allowlist`.

---

## Key Env Vars Reference

| Variable | Default | Purpose |
|----------|---------|--------|
| `CLOSED_CLAW_LLM_PROVIDER` | `siemens` | `siemens \| openai \| gemini \| claude` |
| `CLOSED_CLAW_LLM_MODEL` | `qwen3-30b-a3b-instruct-2507` | Model ID string |
| `CLOSED_CLAW_CREATE_APPROVAL_MODE` | `interactive` | `interactive \| approve \| deny` |
| `CLOSED_CLAW_API_APPROVAL_MODE` | `interactive` | `interactive \| approve \| deny` |
| `CLOSED_CLAW_DB_PATH` | `.closed_claw/registry.db` | SQLite registry path |
| `CLOSED_CLAW_AGENTS_DIR` | `agents` | Agent capsule root dir |
| `CLOSED_CLAW_EXTRA_ALLOWED_PATHS` | _(empty)_ | Comma-separated absolute paths for tool sandboxing |
| `SIEMENS_API_KEY` | _(empty)_ | Siemens LLM key (default provider) |
| `OPENAI_API_KEY` | _(empty)_ | OpenAI key |
| `GEMINI_API_KEY` | _(empty)_ | Gemini key |
| `ANTHROPIC_API_KEY` | _(empty)_ | Anthropic key |

Full list in `.env.example` and `closed_claw/config.py`.
