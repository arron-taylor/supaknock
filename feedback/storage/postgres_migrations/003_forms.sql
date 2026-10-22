CREATE TABLE forms (
 id TEXT PRIMARY KEY,
 project_id TEXT NOT NULL REFERENCES projects(id),
 name TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '',
 enabled BIGINT NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 fields TEXT NOT NULL,
 version BIGINT NOT NULL DEFAULT 1,
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
 version BIGINT NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 deleted_at TEXT,
 UNIQUE(form_id,client_request_id)
);
CREATE INDEX form_submissions_inbox ON form_submissions(form_id,state,created_at,id) WHERE deleted_at IS NULL;
ALTER TABLE devices ADD COLUMN form_notifications BIGINT NOT NULL DEFAULT 0 CHECK(form_notifications IN (0,1));

ALTER TABLE admin_notifications DROP CONSTRAINT admin_notifications_type_check;
ALTER TABLE admin_notifications DROP CONSTRAINT admin_notifications_resource_kind_check;
ALTER TABLE admin_notifications DROP CONSTRAINT admin_notifications_check;
ALTER TABLE admin_notifications ADD CONSTRAINT admin_notifications_type_check CHECK(type IN ('request.created','request.resubmitted','chat.message_created','chat.support_mode_changed','form.submission_created'));
ALTER TABLE admin_notifications ADD CONSTRAINT admin_notifications_resource_kind_check CHECK(resource_kind IN ('request','conversation','form_submission'));
ALTER TABLE admin_notifications ADD CONSTRAINT admin_notifications_check CHECK((type IN ('request.created','request.resubmitted') AND resource_kind='request') OR (type IN ('chat.message_created','chat.support_mode_changed') AND resource_kind='conversation') OR (type='form.submission_created' AND resource_kind='form_submission'));
ALTER TABLE push_deliveries DROP CONSTRAINT push_deliveries_kind_check;
ALTER TABLE push_deliveries ADD CONSTRAINT push_deliveries_kind_check CHECK(kind IN ('request.created','chat.message','form.submission_created'));
