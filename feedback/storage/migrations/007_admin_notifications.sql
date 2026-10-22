-- One durable inbox is shared by every native administrator in this installation.
-- Resource IDs deliberately have no foreign key: deleted requests retain their
-- notification, while event and project references preserve their provenance.
CREATE TABLE admin_notifications (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL UNIQUE REFERENCES request_events(id),
    type TEXT NOT NULL CHECK(type IN ('request.created','request.resubmitted','chat.message_created')),
    project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
    resource_kind TEXT NOT NULL CHECK(resource_kind IN ('request','conversation')),
    resource_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at TEXT,
    CHECK((type IN ('request.created','request.resubmitted') AND resource_kind='request') OR (type='chat.message_created' AND resource_kind='conversation'))
);
CREATE INDEX admin_notifications_unread ON admin_notifications(sequence) WHERE read_at IS NULL;
CREATE TRIGGER admin_notification_event_immutable
BEFORE UPDATE OF sequence,id,event_id,type,project_id,title,resource_kind,resource_id,created_at ON admin_notifications
WHEN NEW.sequence IS NOT OLD.sequence OR NEW.id IS NOT OLD.id OR NEW.event_id IS NOT OLD.event_id
    OR NEW.type IS NOT OLD.type OR NEW.project_id IS NOT OLD.project_id OR NEW.title IS NOT OLD.title
    OR NEW.resource_kind IS NOT OLD.resource_kind OR NEW.resource_id IS NOT OLD.resource_id
    OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'Notification event is immutable'); END;

-- Surface outstanding work on upgrade without flooding the inbox with all
-- historical events. IDs reuse application-generated event UUIDs. New writes
-- retain every eligible event, including messages from the same conversation.
INSERT INTO admin_notifications(id,event_id,type,project_id,title,resource_kind,resource_id,created_at)
WITH request_candidates AS (
    SELECT e.id,e.type,e.project_id,json_extract(e.payload,'$.request.title') title,
           e.request_id resource_id,e.occurred_at created_at,
           row_number() OVER (PARTITION BY e.request_id ORDER BY e.occurred_at DESC,e.rowid DESC) rank
    FROM request_events e JOIN requests r ON r.id=e.request_id
    WHERE r.moderation='pending' AND e.type IN ('request.created','request.resubmitted')
), outstanding AS (
    SELECT id,type,project_id,title,'request' resource_kind,resource_id,created_at
    FROM request_candidates WHERE rank=1
    UNION ALL
    SELECT e.id,e.type,e.project_id,'New customer message','conversation',cv.id,e.occurred_at
    FROM chat_conversations cv
    JOIN chat_messages m ON m.conversation_id=cv.id AND m.sequence=cv.last_sequence
    JOIN request_events e ON e.id=m.id AND e.type='chat.message_created'
    WHERE cv.state='open' AND m.sender='visitor'
)
SELECT id,id,type,project_id,title,resource_kind,resource_id,created_at
FROM outstanding ORDER BY created_at,id;
