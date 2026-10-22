-- Durable optional AI foundations. No model/network side effects on migration.
CREATE TABLE ai_settings (
 project_id TEXT PRIMARY KEY REFERENCES projects(id), settings TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, credential_version INTEGER NOT NULL DEFAULT 0,
 protected_key TEXT, catalogue TEXT NOT NULL DEFAULT '[]', capabilities TEXT NOT NULL DEFAULT '{}',
 capabilities_model TEXT, capabilities_credential_version INTEGER, updated_at TEXT NOT NULL
);
CREATE TABLE ai_runtime (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'not_downloaded' CHECK(state IN ('not_downloaded','downloading','verifying','ready','failed','removing')),
 active_manifest TEXT, operation_id TEXT, action TEXT CHECK(action IN ('download','remove')),
 protected_token TEXT, authority TEXT, progress_bytes INTEGER NOT NULL DEFAULT 0, total_bytes INTEGER NOT NULL DEFAULT 0,
 lease_token TEXT, lease_until REAL, attempt_count INTEGER NOT NULL DEFAULT 0,
 error TEXT, updated_at TEXT NOT NULL
);
INSERT INTO ai_runtime(singleton,updated_at) VALUES(1,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TABLE ai_knowledge_state (
 project_id TEXT PRIMARY KEY REFERENCES projects(id), version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE ai_sources (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL CHECK(kind IN ('markdown','website')), name TEXT NOT NULL,
 location TEXT, approved INTEGER NOT NULL DEFAULT 0 CHECK(approved IN (0,1)),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), deleted_at TEXT,
 version INTEGER NOT NULL DEFAULT 1, active_revision_id TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(id,project_id)
);
CREATE INDEX ai_sources_project ON ai_sources(project_id,created_at,id);
CREATE TRIGGER ai_source_scope_immutable BEFORE UPDATE ON ai_sources
 WHEN NEW.id!=OLD.id OR NEW.project_id!=OLD.project_id OR NEW.kind!=OLD.kind
 BEGIN SELECT RAISE(ABORT,'Knowledge source scope is immutable'); END;
CREATE TABLE ai_revisions (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, project_id TEXT NOT NULL,
 source_version INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('staging','active','retired','discarded')),
 title TEXT NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL,
 profile TEXT NOT NULL, expected_chunks INTEGER NOT NULL CHECK(expected_chunks BETWEEN 1 AND 10000),
 job_id TEXT REFERENCES ai_jobs(id),
 created_at TEXT NOT NULL, FOREIGN KEY(source_id,project_id) REFERENCES ai_sources(id,project_id),
 UNIQUE(id,project_id)
);
CREATE UNIQUE INDEX ai_revision_active ON ai_revisions(source_id) WHERE state='active';
CREATE TRIGGER ai_revision_content_immutable BEFORE UPDATE ON ai_revisions
 WHEN NEW.id!=OLD.id OR NEW.source_id!=OLD.source_id OR NEW.project_id!=OLD.project_id
 OR NEW.source_version!=OLD.source_version OR NEW.title!=OLD.title OR NEW.content!=OLD.content
 OR NEW.content_hash!=OLD.content_hash OR NEW.profile!=OLD.profile OR NEW.expected_chunks!=OLD.expected_chunks
 OR NEW.job_id IS NOT OLD.job_id
 BEGIN SELECT RAISE(ABORT,'Knowledge revision content is immutable'); END;
CREATE TABLE ai_chunks (
 id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, project_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL CHECK(ordinal>=0), title TEXT NOT NULL, content TEXT NOT NULL,
 location TEXT, vector BLOB NOT NULL CHECK(length(vector)=3072),
 FOREIGN KEY(revision_id,project_id) REFERENCES ai_revisions(id,project_id), UNIQUE(revision_id,ordinal)
);
CREATE INDEX ai_chunks_revision ON ai_chunks(revision_id,ordinal);
CREATE VIRTUAL TABLE ai_chunks_fts USING fts5(title,content,content='ai_chunks',content_rowid='rowid',tokenize='unicode61');
CREATE TRIGGER ai_chunks_insert AFTER INSERT ON ai_chunks BEGIN
 INSERT INTO ai_chunks_fts(rowid,title,content) VALUES(NEW.rowid,NEW.title,NEW.content);
END;
CREATE TRIGGER ai_chunks_delete AFTER DELETE ON ai_chunks BEGIN
 INSERT INTO ai_chunks_fts(ai_chunks_fts,rowid,title,content) VALUES('delete',OLD.rowid,OLD.title,OLD.content);
END;
CREATE TRIGGER ai_chunks_immutable BEFORE UPDATE ON ai_chunks BEGIN SELECT RAISE(ABORT,'Knowledge chunks are immutable'); END;
ALTER TABLE chat_conversations ADD COLUMN support_mode TEXT NOT NULL DEFAULT 'human' CHECK(support_mode IN ('ai','waiting_human','human'));
ALTER TABLE chat_conversations ADD COLUMN mode_version INTEGER NOT NULL DEFAULT 1;
-- Preserve the durable customer sender IDs from migration006 and private IPs008.
CREATE TABLE chat_messages_ai (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES chat_conversations(id), sequence INTEGER NOT NULL,
 client_message_id TEXT NOT NULL, sender TEXT NOT NULL CHECK(sender IN ('visitor','admin','ai')),
 sender_id TEXT NOT NULL, body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 4000), created_at TEXT NOT NULL, ip_address TEXT,
 UNIQUE(conversation_id,sequence), UNIQUE(conversation_id,sender_id,client_message_id)
);
INSERT INTO chat_messages_ai SELECT id,conversation_id,sequence,client_message_id,sender,sender_id,body,created_at,ip_address FROM chat_messages;
DROP TABLE chat_messages;
ALTER TABLE chat_messages_ai RENAME TO chat_messages;
CREATE TABLE ai_jobs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL CHECK(kind IN ('ingest','crawl','sections','preview','turn')),
 resource_id TEXT NOT NULL, dedupe_key TEXT NOT NULL, config_version INTEGER NOT NULL,
 authority TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','succeeded','failed','cancelled')),
 attempt_count INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
 lease_token TEXT, lease_until REAL, next_attempt REAL NOT NULL,
 error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(project_id,kind,dedupe_key)
);
CREATE INDEX ai_jobs_due ON ai_jobs(state,next_attempt,lease_until);
CREATE TABLE ai_usage (
 project_id TEXT NOT NULL REFERENCES projects(id), day TEXT NOT NULL,
 reserved_requests INTEGER NOT NULL DEFAULT 0, reserved_tokens INTEGER NOT NULL DEFAULT 0,
 reported_input_tokens INTEGER NOT NULL DEFAULT 0, reported_output_tokens INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(project_id,day)
);
CREATE TABLE ai_usage_reservations (
 job_id TEXT NOT NULL REFERENCES ai_jobs(id), attempt INTEGER NOT NULL, call_index INTEGER NOT NULL DEFAULT 0, project_id TEXT NOT NULL REFERENCES projects(id),
 day TEXT NOT NULL, requests INTEGER NOT NULL CHECK(requests>0), tokens INTEGER NOT NULL CHECK(tokens>0),
 dispatched INTEGER NOT NULL DEFAULT 0 CHECK(dispatched IN (0,1)),
 reported INTEGER NOT NULL DEFAULT 0 CHECK(reported IN (0,1)), PRIMARY KEY(job_id,attempt,call_index)
);
CREATE TABLE ai_guest_owners (
 customer_id TEXT PRIMARY KEY REFERENCES chat_customers(id), project_id TEXT NOT NULL REFERENCES projects(id),
 user_id TEXT NOT NULL UNIQUE REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE TRIGGER ai_guest_owner_insert BEFORE INSERT ON ai_guest_owners WHEN NOT EXISTS(
 SELECT 1 FROM chat_customers WHERE id=NEW.customer_id AND project_id=NEW.project_id AND kind='anonymous' AND user_id IS NULL
) BEGIN SELECT RAISE(ABORT,'Guest owner must belong to anonymous customer project'); END;
CREATE TRIGGER ai_guest_owner_user BEFORE INSERT ON ai_guest_owners WHEN NOT EXISTS(
 SELECT 1 FROM users WHERE id=NEW.user_id AND role='participant'
) OR EXISTS(SELECT 1 FROM local_credentials WHERE user_id=NEW.user_id)
 OR EXISTS(SELECT 1 FROM external_identities WHERE user_id=NEW.user_id)
BEGIN SELECT RAISE(ABORT,'Guest owner must be credentialless'); END;
CREATE TRIGGER ai_guest_owner_immutable BEFORE UPDATE ON ai_guest_owners BEGIN SELECT RAISE(ABORT,'Guest ownership is immutable'); END;
CREATE TABLE ai_proposals (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), conversation_id TEXT NOT NULL REFERENCES chat_conversations(id),
 customer_id TEXT NOT NULL REFERENCES chat_customers(id), kind TEXT NOT NULL CHECK(kind IN ('feature','bug')),
 board_id TEXT NOT NULL REFERENCES boards(id), title TEXT NOT NULL, description TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, config_version INTEGER NOT NULL, mode_version INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','confirmed','cancelled')),
 client_id TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(conversation_id,client_id)
);
CREATE TABLE ai_reports (
 -- No request FK: the durable receipt must survive an allowed creator deletion.
 proposal_id TEXT PRIMARY KEY REFERENCES ai_proposals(id), request_id TEXT NOT NULL UNIQUE,
 project_id TEXT NOT NULL REFERENCES projects(id), conversation_id TEXT NOT NULL REFERENCES chat_conversations(id),
 customer_id TEXT NOT NULL REFERENCES chat_customers(id), creator_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE TRIGGER ai_proposal_scope BEFORE INSERT ON ai_proposals WHEN NOT EXISTS(
 SELECT 1 FROM chat_conversations cv JOIN boards b ON b.id=NEW.board_id
 WHERE cv.id=NEW.conversation_id AND cv.project_id=NEW.project_id
 AND cv.customer_id=NEW.customer_id AND b.project_id=NEW.project_id
) BEGIN SELECT RAISE(ABORT,'Proposal scope mismatch'); END;
CREATE TRIGGER ai_proposal_scope_immutable BEFORE UPDATE ON ai_proposals
 WHEN NEW.id!=OLD.id OR NEW.project_id!=OLD.project_id OR NEW.conversation_id!=OLD.conversation_id
 OR NEW.customer_id!=OLD.customer_id OR NEW.kind!=OLD.kind OR NEW.board_id!=OLD.board_id
 BEGIN SELECT RAISE(ABORT,'Proposal ownership is immutable'); END;
CREATE TRIGGER ai_report_scope BEFORE INSERT ON ai_reports WHEN NOT EXISTS(
 SELECT 1 FROM ai_proposals p JOIN requests r ON r.id=NEW.request_id
 WHERE p.id=NEW.proposal_id AND p.project_id=NEW.project_id AND p.conversation_id=NEW.conversation_id
 AND p.customer_id=NEW.customer_id AND r.board_id=p.board_id AND r.creator_id=NEW.creator_id
) BEGIN SELECT RAISE(ABORT,'Report scope mismatch'); END;
CREATE TRIGGER ai_report_immutable BEFORE UPDATE ON ai_reports
 BEGIN SELECT RAISE(ABORT,'Report ownership and receipt are immutable'); END;
