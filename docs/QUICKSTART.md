# Quickstart

This is the fastest way to run Closed Claw locally.

## 1) Setup

```bash
conda create -n closed_claw python=3.11 -y
conda activate closed_claw
cd /Users/yashagrawal/Documents/closed_claw/closed_claw
pip install -r requirements.txt
cp .env.example .env
```

## 2) Initialize

```bash
python -m closed_claw.cli init
python -m closed_claw.cli doctor
```

Expected: `doctor` should report `langgraph_ok: true` and ideally `sqlite_vec_ok: true`.

## 3) Run a Task

Interactive mode:

```bash
python -m closed_claw.cli run "please use paid_api for analysis"
```

Interactive launcher:

```bash
python -m closed_claw.cli
```

This opens the main menu (with startup art shown once) with setup/init/doctor/run/list options.
You can also delete an agent from the menu.

Setup wizard directly:

```bash
python -m closed_claw.cli setup
```

Wizard validates provider connectivity before saving.

Non-interactive mode (useful for scripts/CI):

```bash
python -m closed_claw.cli run "please use paid_api for analysis" \
  --create-approval-mode approve \
  --api-approval-mode approve
```

Use an LLM reranker provider (optional):

```bash
export OPENAI_API_KEY=\"<your_key>\"
python -m closed_claw.cli run \"please use paid_api for analysis\" \
  --llm-provider openai \
  --llm-model gpt-4o-mini
```

## 4) Inspect Results

```bash
python -m closed_claw.cli agents --limit 20
python -m closed_claw.cli runs --limit 20
python -m closed_claw.cli audit --limit 20
python -m closed_claw.cli runlog <run_id> --tail 200
python -m closed_claw.cli tools
python -m closed_claw.cli tools --agent-id <agent_id>
python -m closed_claw.cli agent <agent_id>
python -m closed_claw.cli delete-agent <agent_id>
python -m closed_claw.cli delete-all-agents
```

## 5) Run Tests

```bash
pytest -q
```

## Common Fixes

If sqlite-vec fails:

```bash
export SQLITE_VEC_PATH="$(python -c 'import sqlite_vec; print(sqlite_vec.loadable_path())')"
python -m closed_claw.cli doctor
```

If you want fully local/offline deterministic embeddings:

```bash
export CLOSED_CLAW_ENABLE_SENTENCE_TRANSFORMERS=false
```
