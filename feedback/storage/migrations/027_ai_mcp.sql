CREATE TABLE ai_mcp_connections (
 id TEXT PRIMARY KEY, label TEXT NOT NULL, url TEXT NOT NULL, protected_token TEXT,
 enabled INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
 tested_version INTEGER, catalogue TEXT NOT NULL DEFAULT '[]', link_origins TEXT NOT NULL DEFAULT '[]',
 error TEXT, tested_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT
);
CREATE TABLE ai_mcp_projects (
 project_id TEXT PRIMARY KEY REFERENCES projects(id), version INTEGER NOT NULL DEFAULT 0,
 source_mode TEXT NOT NULL DEFAULT 'knowledge' CHECK(source_mode IN ('knowledge','tools','combined'))
);
CREATE TABLE ai_mcp_bindings (
 project_id TEXT NOT NULL REFERENCES projects(id), connection_id TEXT NOT NULL REFERENCES ai_mcp_connections(id),
 connection_version INTEGER NOT NULL, tools TEXT NOT NULL, context_kind TEXT,
 PRIMARY KEY(project_id,connection_id)
);
CREATE TABLE ai_mcp_calls (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES ai_jobs(id), lease_token TEXT NOT NULL,
 connection_id TEXT NOT NULL REFERENCES ai_mcp_connections(id), connection_version INTEGER NOT NULL,
 tool_name TEXT NOT NULL, call_id TEXT NOT NULL, state TEXT NOT NULL,
 created_at TEXT NOT NULL, elapsed REAL NOT NULL DEFAULT 0,
 UNIQUE(job_id,lease_token,call_id)
);
CREATE INDEX ai_mcp_calls_job ON ai_mcp_calls(job_id,lease_token);
ALTER TABLE chat_messages ADD COLUMN context TEXT;
ALTER TABLE chat_messages ADD COLUMN tool_sources TEXT NOT NULL DEFAULT '[]';
