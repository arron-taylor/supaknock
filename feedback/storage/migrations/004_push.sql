CREATE TABLE push_settings (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 relay_url TEXT NOT NULL,
 enrollment_proof TEXT NOT NULL,
 credential TEXT,
 last_error TEXT,
 version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE push_deliveries (
 id TEXT PRIMARY KEY,
 event_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('request.created','chat.message')),
 resource_id TEXT NOT NULL,
 device_id TEXT NOT NULL,
 generation INTEGER NOT NULL,
 token TEXT NOT NULL,
 environment TEXT NOT NULL,
 relay_id TEXT,
 state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','sending','awaiting','succeeded','failed','cancelled','expired')),
 attempt_count INTEGER NOT NULL DEFAULT 0,
 retry_count INTEGER NOT NULL DEFAULT 0,
 manual_retry INTEGER NOT NULL DEFAULT 0,
 next_attempt_at REAL NOT NULL,
 created_at REAL NOT NULL,
 expires_at REAL NOT NULL,
 lease_until REAL,
 lease_token TEXT,
 error TEXT,
 UNIQUE(event_id,device_id)
);
CREATE INDEX push_due ON push_deliveries(state,next_attempt_at,lease_until);
ALTER TABLE push_deliveries ADD COLUMN submitted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE push_deliveries ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE push_deliveries ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
