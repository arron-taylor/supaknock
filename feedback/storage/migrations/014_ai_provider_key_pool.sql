-- Encrypted provider pools and account-level recovery. No network side effects.
CREATE TABLE ai_provider_quota_groups (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, label TEXT NOT NULL,
 health_epoch INTEGER NOT NULL DEFAULT 0, reset_epoch INTEGER NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0,
 failure_count INTEGER NOT NULL DEFAULT 0, recovery_required INTEGER NOT NULL DEFAULT 0 CHECK(recovery_required IN (0,1)),
 reason TEXT, trial_job_id TEXT, trial_attempt INTEGER, trial_token TEXT, trial_until REAL,
 UNIQUE(provider,label)
);
CREATE TABLE ai_provider_keys (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), provider TEXT NOT NULL,
 label TEXT NOT NULL, quota_group_id TEXT NOT NULL REFERENCES ai_provider_quota_groups(id),
 version INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)), protected_key TEXT,
 catalogue TEXT NOT NULL DEFAULT '[]', capabilities TEXT NOT NULL DEFAULT '{}',
 capabilities_model TEXT, capabilities_key_version INTEGER,
 blocked_reason TEXT, reason TEXT, health_epoch INTEGER NOT NULL DEFAULT 0,
 last_selected INTEGER NOT NULL DEFAULT 0, deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(id,project_id)
);
CREATE UNIQUE INDEX ai_provider_key_default ON ai_provider_keys(project_id) WHERE is_default=1 AND deleted_at IS NULL;
CREATE INDEX ai_provider_key_pool ON ai_provider_keys(project_id,provider,deleted_at);
CREATE TABLE ai_provider_attempts (
 job_id TEXT NOT NULL REFERENCES ai_jobs(id), attempt INTEGER NOT NULL,
 key_id TEXT NOT NULL REFERENCES ai_provider_keys(id), key_version INTEGER NOT NULL,
 health_epoch INTEGER NOT NULL, quota_group_id TEXT NOT NULL REFERENCES ai_provider_quota_groups(id),
 group_epoch INTEGER NOT NULL, lease_token TEXT NOT NULL,
 PRIMARY KEY(job_id,attempt)
);
ALTER TABLE ai_usage_reservations ADD COLUMN provider_key_id TEXT;
ALTER TABLE ai_usage_reservations ADD COLUMN provider_key_version INTEGER;
ALTER TABLE ai_usage_reservations ADD COLUMN provider_health_epoch INTEGER;
ALTER TABLE ai_usage_reservations ADD COLUMN provider_group_id TEXT;
ALTER TABLE ai_usage_reservations ADD COLUMN provider_group_epoch INTEGER;
ALTER TABLE ai_usage_reservations ADD COLUMN provider_outcome TEXT;
INSERT INTO ai_provider_quota_groups(id,provider,label)
 SELECT lower(hex(randomblob(16))),json_extract(settings,'$.provider'),'Default account'
 FROM ai_settings WHERE protected_key IS NOT NULL GROUP BY json_extract(settings,'$.provider');
INSERT INTO ai_provider_keys(id,project_id,provider,label,quota_group_id,is_default,protected_key,catalogue,capabilities,capabilities_model,capabilities_key_version,created_at,updated_at)
 SELECT lower(hex(randomblob(16))),s.project_id,json_extract(s.settings,'$.provider'),'Default key',g.id,1,s.protected_key,s.catalogue,
 CASE WHEN s.capabilities_credential_version=s.credential_version THEN s.capabilities ELSE '{}' END,
 CASE WHEN s.capabilities_credential_version=s.credential_version THEN s.capabilities_model END,
 CASE WHEN s.capabilities_credential_version=s.credential_version THEN 1 END,s.updated_at,s.updated_at
 FROM ai_settings s JOIN ai_provider_quota_groups g ON g.provider=json_extract(s.settings,'$.provider') AND g.label='Default account'
 WHERE s.protected_key IS NOT NULL;
