CREATE TABLE chat_business_hours (
    scope TEXT PRIMARY KEY,
    project_id TEXT UNIQUE REFERENCES projects(id),
    mode TEXT NOT NULL CHECK(mode IN ('inherit','override','off')),
    schedule TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    updated_at TEXT NOT NULL
);
CREATE TABLE chat_business_hours_audit (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES users(id),
    version INTEGER NOT NULL,
    occurred_at TEXT NOT NULL
);
ALTER TABLE chat_conversations ADD COLUMN follow_up_email TEXT;
ALTER TABLE chat_conversations ADD COLUMN contact_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chat_messages ADD COLUMN follow_up_email_hash TEXT;
