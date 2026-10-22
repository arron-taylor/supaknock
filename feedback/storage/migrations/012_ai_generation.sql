-- Immutable generation receipts and derived authoring results; no model work.
ALTER TABLE chat_messages ADD COLUMN citations TEXT NOT NULL DEFAULT '[]';
CREATE TABLE ai_turns (
 job_id TEXT PRIMARY KEY REFERENCES ai_jobs(id), conversation_id TEXT NOT NULL REFERENCES chat_conversations(id),
 trigger_message_id TEXT NOT NULL UNIQUE REFERENCES chat_messages(id),
 reply_message_id TEXT UNIQUE REFERENCES chat_messages(id), reply_lease_token TEXT, reply_hash TEXT
);
CREATE INDEX ai_turns_conversation ON ai_turns(conversation_id,job_id);
CREATE TABLE ai_authoring_runs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), job_id TEXT NOT NULL UNIQUE REFERENCES ai_jobs(id),
 kind TEXT NOT NULL CHECK(kind IN ('preview','sections')), client_id TEXT NOT NULL, request_hash TEXT NOT NULL,
 config_version INTEGER NOT NULL, knowledge_version INTEGER NOT NULL,
 source_id TEXT REFERENCES ai_sources(id), source_version INTEGER, query TEXT,
 version INTEGER NOT NULL DEFAULT 1, result TEXT, coverage TEXT,
 created_at TEXT NOT NULL, finished_at TEXT, UNIQUE(project_id,client_id)
);
CREATE TABLE ai_sections (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ai_authoring_runs(id), project_id TEXT NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL CHECK(kind IN ('section','faq')), title TEXT NOT NULL, body TEXT NOT NULL, category TEXT NOT NULL,
 citation_ids TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE ai_section_versions (
 section_id TEXT NOT NULL REFERENCES ai_sections(id), version INTEGER NOT NULL,
 title TEXT NOT NULL, body TEXT NOT NULL, category TEXT NOT NULL, citation_ids TEXT NOT NULL, deleted_at TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(section_id,version)
);
CREATE TRIGGER ai_section_history_immutable BEFORE UPDATE ON ai_section_versions
 BEGIN SELECT RAISE(ABORT,'Derived section history is immutable'); END;
-- Retain the shared inbox watermark and read state while allowing handoffs.
CREATE TABLE admin_notifications_ai (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 event_id TEXT NOT NULL UNIQUE REFERENCES request_events(id),
 type TEXT NOT NULL CHECK(type IN ('request.created','request.resubmitted','chat.message_created','chat.support_mode_changed')),
 project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
 resource_kind TEXT NOT NULL CHECK(resource_kind IN ('request','conversation')), resource_id TEXT NOT NULL,
 created_at TEXT NOT NULL, read_at TEXT,
 CHECK((type IN ('request.created','request.resubmitted') AND resource_kind='request') OR (type IN ('chat.message_created','chat.support_mode_changed') AND resource_kind='conversation'))
);
INSERT INTO admin_notifications_ai SELECT * FROM admin_notifications;
DROP TABLE admin_notifications;
ALTER TABLE admin_notifications_ai RENAME TO admin_notifications;
CREATE INDEX admin_notifications_unread ON admin_notifications(sequence) WHERE read_at IS NULL;
CREATE TRIGGER admin_notification_event_immutable
BEFORE UPDATE OF sequence,id,event_id,type,project_id,title,resource_kind,resource_id,created_at ON admin_notifications
WHEN NEW.sequence IS NOT OLD.sequence OR NEW.id IS NOT OLD.id OR NEW.event_id IS NOT OLD.event_id
 OR NEW.type IS NOT OLD.type OR NEW.project_id IS NOT OLD.project_id OR NEW.title IS NOT OLD.title
 OR NEW.resource_kind IS NOT OLD.resource_kind OR NEW.resource_id IS NOT OLD.resource_id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'Notification event is immutable'); END;
