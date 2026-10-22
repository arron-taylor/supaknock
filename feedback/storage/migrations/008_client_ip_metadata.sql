-- Private, transport-observed metadata. Existing records remain unknown; IP
-- addresses do not enter immutable event payloads or outbound delivery bodies.
ALTER TABLE requests ADD COLUMN submitted_ip_address TEXT;
ALTER TABLE request_events ADD COLUMN ip_address TEXT;
ALTER TABLE chat_conversations ADD COLUMN initial_ip_address TEXT;
ALTER TABLE chat_conversations ADD COLUMN last_customer_ip_address TEXT;
ALTER TABLE chat_messages ADD COLUMN ip_address TEXT;

-- Successful native-account activity outlives transport-session expiry.
CREATE TABLE account_activity (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    event_type TEXT NOT NULL CHECK(event_type IN ('signup','login','account_created','setup')),
    transport TEXT CHECK(transport IN ('cookie','native')),
    ip_address TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX account_activity_time ON account_activity(occurred_at DESC,id DESC);
CREATE INDEX account_activity_user_time ON account_activity(user_id,occurred_at DESC,id DESC);
