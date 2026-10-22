ALTER TABLE projects ADD COLUMN archived_at TEXT;
ALTER TABLE boards ADD COLUMN archived_at TEXT;
CREATE TABLE resource_archive_events (id TEXT PRIMARY KEY, resource_kind TEXT NOT NULL CHECK(resource_kind IN ('project','board')), resource_id TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id), actor_id TEXT NOT NULL REFERENCES users(id), archived INTEGER NOT NULL CHECK(archived IN (0,1)), version INTEGER NOT NULL, occurred_at TEXT NOT NULL);
CREATE TRIGGER resource_archive_events_no_update BEFORE UPDATE ON resource_archive_events BEGIN SELECT RAISE(ABORT,'Archive history is immutable'); END;
CREATE TRIGGER resource_archive_events_no_delete BEFORE DELETE ON resource_archive_events BEGIN SELECT RAISE(ABORT,'Archive history is immutable'); END;
