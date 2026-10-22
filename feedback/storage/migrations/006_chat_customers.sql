-- Customer identity outlives every transport credential. Anonymous identities
-- retain their existing private session; there is deliberately no account linking.
CREATE TABLE chat_customers (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    user_id TEXT REFERENCES users(id),
    kind TEXT NOT NULL CHECK(kind IN ('anonymous','external','native')),
    display_name TEXT CHECK(length(display_name)<=100),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id,user_id),
    CHECK((kind='anonymous' AND user_id IS NULL) OR (kind!='anonymous' AND user_id IS NOT NULL))
);
CREATE TRIGGER chat_customer_identity_immutable BEFORE UPDATE OF project_id,user_id,kind ON chat_customers
WHEN NEW.project_id IS NOT OLD.project_id OR NEW.user_id IS NOT OLD.user_id OR NEW.kind IS NOT OLD.kind
BEGIN SELECT RAISE(ABORT,'Customer identity is immutable'); END;
-- Existing application-generated conversation UUIDs are stable and unique, so
-- they also identify the backfilled, otherwise unrecoverable anonymous customer.
INSERT INTO chat_customers(id,project_id,kind,created_at,updated_at)
SELECT id,project_id,'anonymous',created_at,updated_at FROM chat_conversations;
ALTER TABLE chat_conversations ADD COLUMN customer_id TEXT REFERENCES chat_customers(id);
ALTER TABLE chat_conversations ADD COLUMN client_conversation_id TEXT;
UPDATE chat_conversations SET customer_id=id;
CREATE UNIQUE INDEX chat_conversation_client ON chat_conversations(customer_id,client_conversation_id);
CREATE INDEX chat_customer_conversations ON chat_conversations(customer_id,updated_at,id);
CREATE TRIGGER chat_conversation_customer_insert BEFORE INSERT ON chat_conversations
WHEN NOT EXISTS(SELECT 1 FROM chat_customers WHERE id=NEW.customer_id AND project_id=NEW.project_id)
BEGIN SELECT RAISE(ABORT,'Conversation customer must belong to its project'); END;
CREATE TRIGGER chat_conversation_customer_immutable BEFORE UPDATE OF customer_id,project_id ON chat_conversations
WHEN NEW.customer_id IS NOT OLD.customer_id OR NEW.project_id IS NOT OLD.project_id
BEGIN SELECT RAISE(ABORT,'Conversation ownership is immutable'); END;
CREATE TABLE chat_customer_reads (
    customer_id TEXT NOT NULL REFERENCES chat_customers(id),
    conversation_id TEXT NOT NULL REFERENCES chat_conversations(id),
    read_sequence INTEGER NOT NULL DEFAULT 0 CHECK(read_sequence>=0),
    PRIMARY KEY(customer_id,conversation_id)
);
INSERT INTO chat_customer_reads(customer_id,conversation_id,read_sequence)
SELECT cv.customer_id,cv.id,MAX(s.read_sequence) FROM chat_conversations cv
JOIN chat_sessions s ON s.conversation_id=cv.id GROUP BY cv.id;
-- A retry from a renewed credential must identify the same sender.
UPDATE chat_messages SET sender_id=(SELECT customer_id FROM chat_conversations WHERE id=chat_messages.conversation_id)
WHERE sender='visitor';
CREATE TABLE chat_customer_sessions (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES chat_customers(id),
    token_hash TEXT NOT NULL UNIQUE,
    origin TEXT,
    issuing_key_id TEXT REFERENCES api_keys(id),
    -- Native sessions are garbage collected after expiry. A missing parent
    -- invalidates this credential, without deleting the customer's history.
    parent_session_id TEXT,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at TEXT,
    CHECK((issuing_key_id IS NULL) != (parent_session_id IS NULL))
);
CREATE INDEX chat_customer_sessions_customer ON chat_customer_sessions(customer_id);
CREATE INDEX chat_customer_sessions_expiry ON chat_customer_sessions(expires_at);
ALTER TABLE chat_tickets RENAME TO chat_tickets_before_customers;
CREATE TABLE chat_tickets (
    token_hash TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES chat_conversations(id),
    kind TEXT NOT NULL CHECK(kind IN ('visitor','customer','admin')),
    session_id TEXT NOT NULL,
    user_id TEXT,
    origin TEXT,
    expires_at REAL NOT NULL
);
INSERT INTO chat_tickets SELECT * FROM chat_tickets_before_customers;
DROP TABLE chat_tickets_before_customers;
CREATE INDEX chat_tickets_expiry ON chat_tickets(expires_at);
