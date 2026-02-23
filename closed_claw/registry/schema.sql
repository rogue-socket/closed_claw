CREATE TABLE IF NOT EXISTS agents (
  agent_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  embedding_model TEXT NOT NULL,
  embedding_dim INTEGER NOT NULL,
  embedding_json TEXT NOT NULL,
  tools_allowlist_json TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  api_capabilities_json TEXT NOT NULL,
  requires_approval_for_json TEXT NOT NULL,
  version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_used_at TEXT,
  usage_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0,
  failure_count INTEGER NOT NULL DEFAULT 0,
  success_rate REAL NOT NULL DEFAULT 0,
  avg_latency_ms REAL,
  status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  agent_id TEXT,
  task TEXT NOT NULL,
  status TEXT NOT NULL,
  latency_ms REAL,
  error_message TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_compositions (
  agent_a TEXT NOT NULL,
  agent_b TEXT NOT NULL,
  co_run_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (agent_a, agent_b)
);

CREATE TABLE IF NOT EXISTS provider_circuit_breakers (
  provider TEXT PRIMARY KEY,
  failure_count INTEGER NOT NULL DEFAULT 0,
  opened_at TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS agent_vectors USING vec0(
  agent_id TEXT,
  embedding float[384]
);
