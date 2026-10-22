-- Separate, default-off policy and role-scoped capability evidence.
ALTER TABLE ai_provider_keys ADD COLUMN purpose TEXT NOT NULL DEFAULT 'generation'
 CHECK(purpose IN ('generation','decision','both'));
CREATE TABLE ai_topic_policies (
 project_id TEXT PRIMARY KEY REFERENCES projects(id),
 version INTEGER NOT NULL DEFAULT 0, policy TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE ai_decision_keys (
 key_id TEXT PRIMARY KEY REFERENCES ai_provider_keys(id),
 project_id TEXT NOT NULL REFERENCES projects(id),
 catalogue TEXT NOT NULL DEFAULT '[]', model TEXT, key_version INTEGER,
 verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1))
);
CREATE TABLE ai_topic_receipts (
 job_id TEXT NOT NULL REFERENCES ai_jobs(id), attempt INTEGER NOT NULL,
 lease_token TEXT NOT NULL, policy_version INTEGER NOT NULL,
 preset_version TEXT NOT NULL, context_hash TEXT NOT NULL, trigger_sequence INTEGER,
 outcome TEXT NOT NULL CHECK(outcome IN ('allow','refuse','clarify','greeting','unavailable')),
 probability REAL CHECK(probability>=0 AND probability<=1), model TEXT,
 key_id TEXT, key_version INTEGER, health_epoch INTEGER, group_epoch INTEGER,
 body TEXT, error TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(job_id,attempt)
);
CREATE TABLE ai_decision_attempts (
 job_id TEXT NOT NULL REFERENCES ai_jobs(id), attempt INTEGER NOT NULL,
 lease_token TEXT NOT NULL, key_id TEXT NOT NULL REFERENCES ai_provider_keys(id),
 key_version INTEGER NOT NULL, health_epoch INTEGER NOT NULL, group_epoch INTEGER NOT NULL,
 PRIMARY KEY(job_id,attempt)
);
CREATE TABLE ai_decision_checks (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 policy_version INTEGER NOT NULL, key_id TEXT NOT NULL REFERENCES ai_provider_keys(id),
 key_version INTEGER NOT NULL, health_epoch INTEGER NOT NULL, group_epoch INTEGER NOT NULL,
 principal TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('running','succeeded','failed')),
 lease_until REAL NOT NULL, model TEXT NOT NULL, day TEXT NOT NULL,
 reserved_tokens INTEGER NOT NULL, reported INTEGER NOT NULL DEFAULT 0,
 outcome TEXT, probability REAL, error TEXT, created_at TEXT NOT NULL
);
