-- Preserve IDs, ciphertext, history and original selections; no secret deduplication.
-- project_id/is_default remain nullable legacy provenance, never runtime authority.
CREATE TABLE ai_provider_keys_next (
 id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id), provider TEXT NOT NULL,
 label TEXT NOT NULL, quota_group_id TEXT NOT NULL REFERENCES ai_provider_quota_groups(id),
 version INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)), protected_key TEXT,
 catalogue TEXT NOT NULL DEFAULT '[]', capabilities TEXT NOT NULL DEFAULT '{}',
 capabilities_model TEXT, capabilities_key_version INTEGER,
 blocked_reason TEXT, reason TEXT, health_epoch INTEGER NOT NULL DEFAULT 0,
 last_selected INTEGER NOT NULL DEFAULT 0, deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 purpose TEXT NOT NULL DEFAULT 'generation' CHECK(purpose IN ('generation','decision','both'))
);
INSERT INTO ai_provider_keys_next SELECT * FROM ai_provider_keys;
CREATE TABLE ai_generation_keys (
 project_id TEXT NOT NULL REFERENCES projects(id), key_id TEXT NOT NULL REFERENCES ai_provider_keys(id),
 is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)), PRIMARY KEY(project_id,key_id)
);
INSERT INTO ai_generation_keys SELECT project_id,id,is_default FROM ai_provider_keys
 WHERE deleted_at IS NULL AND purpose!='decision';
CREATE UNIQUE INDEX ai_generation_default ON ai_generation_keys(project_id) WHERE is_default=1;
CREATE TABLE ai_decision_keys_next (
 key_id TEXT NOT NULL REFERENCES ai_provider_keys(id), project_id TEXT NOT NULL REFERENCES projects(id),
 catalogue TEXT NOT NULL DEFAULT '[]', model TEXT, key_version INTEGER,
 verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)), PRIMARY KEY(project_id,key_id)
);
INSERT INTO ai_decision_keys_next SELECT * FROM ai_decision_keys;
DROP TABLE ai_decision_keys;
ALTER TABLE ai_decision_keys_next RENAME TO ai_decision_keys;
DROP TABLE ai_provider_keys;
ALTER TABLE ai_provider_keys_next RENAME TO ai_provider_keys;
CREATE INDEX ai_account_key_pool ON ai_provider_keys(provider,deleted_at);
CREATE INDEX ai_generation_key_projects ON ai_generation_keys(key_id,project_id);
CREATE INDEX ai_decision_key_projects ON ai_decision_keys(key_id,project_id);
CREATE TABLE ai_key_model_evidence (
 key_id TEXT NOT NULL REFERENCES ai_provider_keys(id), role TEXT NOT NULL CHECK(role IN ('generation','decision')),
 model TEXT NOT NULL, key_version INTEGER NOT NULL, capabilities TEXT NOT NULL DEFAULT '{}',
 verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)), PRIMARY KEY(key_id,role,model)
);
INSERT INTO ai_key_model_evidence SELECT id,'generation',capabilities_model,capabilities_key_version,capabilities,1
 FROM ai_provider_keys WHERE deleted_at IS NULL AND capabilities_model IS NOT NULL AND capabilities_key_version=version;
INSERT INTO ai_key_model_evidence SELECT d.key_id,'decision',d.model,d.key_version,'{}',d.verified
 FROM ai_decision_keys d JOIN ai_provider_keys k ON k.id=d.key_id
 WHERE d.model IS NOT NULL AND d.key_version=k.version AND k.deleted_at IS NULL;
CREATE TABLE ai_key_decision_catalogues (
 key_id TEXT PRIMARY KEY REFERENCES ai_provider_keys(id), catalogue TEXT NOT NULL DEFAULT '[]'
);
INSERT INTO ai_key_decision_catalogues SELECT key_id,catalogue FROM ai_decision_keys;
CREATE TABLE ai_key_library (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL DEFAULT 0
);
INSERT INTO ai_key_library(singleton) VALUES(1);
-- Canonical secrets now live only in the account library.
UPDATE ai_settings SET protected_key=NULL;
-- Shared, recoverable leases for explicit catalogue/generation checks.
CREATE TABLE ai_provider_admin_checks (
 id TEXT PRIMARY KEY, token TEXT NOT NULL, key_id TEXT NOT NULL REFERENCES ai_provider_keys(id),
 quota_group_id TEXT NOT NULL REFERENCES ai_provider_quota_groups(id), lease_until REAL NOT NULL
);
CREATE INDEX ai_admin_check_key ON ai_provider_admin_checks(key_id,lease_until);
