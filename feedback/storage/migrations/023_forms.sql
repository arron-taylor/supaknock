CREATE TABLE forms (
 id TEXT PRIMARY KEY,
 project_id TEXT NOT NULL REFERENCES projects(id),
 name TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '',
 enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 fields TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX forms_project ON forms(project_id,created_at,id);
CREATE TABLE form_submissions (
 id TEXT PRIMARY KEY,
 form_id TEXT NOT NULL REFERENCES forms(id),
 client_request_id TEXT NOT NULL,
 answers TEXT NOT NULL,
 answer_hash TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'new' CHECK(state IN ('new','reviewed','archived')),
 version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 deleted_at TEXT,
 UNIQUE(form_id,client_request_id)
);
CREATE INDEX form_submissions_inbox ON form_submissions(form_id,state,created_at,id) WHERE deleted_at IS NULL;
ALTER TABLE devices ADD COLUMN form_notifications INTEGER NOT NULL DEFAULT 0 CHECK(form_notifications IN (0,1));

CREATE TABLE admin_notifications_forms (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 event_id TEXT NOT NULL UNIQUE REFERENCES request_events(id),
 type TEXT NOT NULL CHECK(type IN ('request.created','request.resubmitted','chat.message_created','chat.support_mode_changed','form.submission_created')),
 project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
 resource_kind TEXT NOT NULL CHECK(resource_kind IN ('request','conversation','form_submission')), resource_id TEXT NOT NULL,
 created_at TEXT NOT NULL, read_at TEXT,
 CHECK((type IN ('request.created','request.resubmitted') AND resource_kind='request') OR (type IN ('chat.message_created','chat.support_mode_changed') AND resource_kind='conversation') OR (type='form.submission_created' AND resource_kind='form_submission'))
);
INSERT INTO admin_notifications_forms SELECT * FROM admin_notifications;
DROP TABLE admin_notifications;
ALTER TABLE admin_notifications_forms RENAME TO admin_notifications;
CREATE INDEX admin_notifications_unread ON admin_notifications(sequence) WHERE read_at IS NULL;
CREATE TRIGGER admin_notification_event_immutable
BEFORE UPDATE OF sequence,id,event_id,type,project_id,title,resource_kind,resource_id,created_at ON admin_notifications
WHEN NEW.sequence IS NOT OLD.sequence OR NEW.id IS NOT OLD.id OR NEW.event_id IS NOT OLD.event_id
 OR NEW.type IS NOT OLD.type OR NEW.project_id IS NOT OLD.project_id OR NEW.title IS NOT OLD.title
 OR NEW.resource_kind IS NOT OLD.resource_kind OR NEW.resource_id IS NOT OLD.resource_id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'Notification event is immutable'); END;

CREATE TABLE push_deliveries_forms (
 id TEXT PRIMARY KEY,
 event_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('request.created','chat.message','form.submission_created')),
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

ALTER TABLE push_deliveries_forms ADD COLUMN submitted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE push_deliveries_forms ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE push_deliveries_forms ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;

INSERT INTO push_deliveries_forms SELECT * FROM push_deliveries;
DROP TABLE push_deliveries;
ALTER TABLE push_deliveries_forms RENAME TO push_deliveries;
CREATE INDEX push_due ON push_deliveries(state,next_attempt_at,lease_until);
