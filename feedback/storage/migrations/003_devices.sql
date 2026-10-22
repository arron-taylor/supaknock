CREATE TABLE devices (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 token TEXT NOT NULL, token_hash TEXT NOT NULL, environment TEXT NOT NULL CHECK(environment IN ('sandbox','production')),
 name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 request_notifications INTEGER NOT NULL DEFAULT 1, chat_notifications INTEGER NOT NULL DEFAULT 1,
 generation INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
 UNIQUE(token_hash,environment)
);
CREATE INDEX devices_owner ON devices(user_id,id);
