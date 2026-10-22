-- PostgreSQL baseline, equivalent to SQLite migrations 001 through 021.
-- Dedicated private supaknock schema; no public REST exposure or extension required.
-- Timestamps and JSON retain their existing application wire representation.

CREATE TABLE users (id TEXT PRIMARY KEY, display_name TEXT, role TEXT NOT NULL CHECK(role IN ('participant','admin')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE local_credentials (user_id TEXT PRIMARY KEY , email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL , token_hash TEXT NOT NULL UNIQUE, csrf_hash TEXT NOT NULL, created_at TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL, revoked_at TEXT, transport TEXT NOT NULL DEFAULT 'cookie' CHECK(transport IN ('cookie','native')));

CREATE TABLE installation (id BIGINT PRIMARY KEY CHECK(id=1), setup_completed_at TEXT NOT NULL);

CREATE TABLE projects (id TEXT PRIMARY KEY, identifier TEXT NOT NULL UNIQUE, name TEXT NOT NULL, description TEXT NOT NULL, webhook_url TEXT, webhook_enabled BIGINT NOT NULL DEFAULT 0, webhook_events TEXT NOT NULL DEFAULT '[]', webhook_secret TEXT, created_by TEXT NOT NULL , created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version BIGINT NOT NULL DEFAULT 1, archived_at TEXT);

CREATE TABLE boards (id TEXT PRIMARY KEY, project_id TEXT NOT NULL , identifier TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version BIGINT NOT NULL DEFAULT 1, archived_at TEXT, UNIQUE(project_id,identifier));

CREATE TABLE external_identities (id TEXT PRIMARY KEY, project_id TEXT NOT NULL , external_user_id BYTEA NOT NULL, user_id TEXT NOT NULL , created_at TEXT NOT NULL, UNIQUE(project_id,external_user_id));

CREATE TABLE requests (id TEXT PRIMARY KEY, board_id TEXT NOT NULL , creator_id TEXT NOT NULL , title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200), description TEXT NOT NULL CHECK(length(description)<=20000), moderation TEXT NOT NULL CHECK(moderation IN ('pending','accepted','rejected')), status TEXT CHECK(status IN ('under_consideration','planned','in_progress','shipped')), accepted_at TEXT, accepted_by TEXT, rejection_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version BIGINT NOT NULL DEFAULT 1, submitted_ip_address TEXT, CHECK((moderation='accepted' AND status IS NOT NULL AND accepted_at IS NOT NULL) OR (moderation!='accepted' AND status IS NULL AND accepted_at IS NULL)));

CREATE TABLE votes (request_id TEXT NOT NULL , user_id TEXT NOT NULL , created_at TEXT NOT NULL, PRIMARY KEY(request_id,user_id));

CREATE TABLE api_keys (id TEXT PRIMARY KEY, project_id TEXT NOT NULL , name TEXT NOT NULL, secret_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL, permissions TEXT NOT NULL, created_by TEXT NOT NULL , created_at TEXT NOT NULL, revoked_at TEXT);

CREATE TABLE request_events (id TEXT PRIMARY KEY, type TEXT NOT NULL, schema_version BIGINT NOT NULL, project_id TEXT NOT NULL , board_id TEXT, request_id TEXT, actor TEXT NOT NULL, occurred_at TEXT NOT NULL, payload TEXT NOT NULL, ip_address TEXT);

CREATE TABLE webhook_deliveries (id TEXT PRIMARY KEY, event_id TEXT NOT NULL , destination TEXT NOT NULL, signing_secret TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('queued','delivering','succeeded','failed')), attempt_count BIGINT NOT NULL DEFAULT 0, next_attempt_at DOUBLE PRECISION NOT NULL, lease_until DOUBLE PRECISION, lease_token TEXT, last_attempt_at TEXT, last_http_status BIGINT, error TEXT, delivered_at TEXT);

CREATE TABLE instance_identity (singleton BIGINT PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL UNIQUE);

CREATE TABLE devices (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL , session_id TEXT NOT NULL ,
 token TEXT NOT NULL, token_hash TEXT NOT NULL, environment TEXT NOT NULL CHECK(environment IN ('sandbox','production')),
 name TEXT NOT NULL, enabled BIGINT NOT NULL DEFAULT 1,
 request_notifications BIGINT NOT NULL DEFAULT 1, chat_notifications BIGINT NOT NULL DEFAULT 1,
 generation BIGINT NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
 UNIQUE(token_hash,environment)
);

CREATE TABLE push_settings (
 singleton BIGINT PRIMARY KEY CHECK(singleton=1),
 enabled BIGINT NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 relay_url TEXT NOT NULL,
 enrollment_proof TEXT NOT NULL,
 credential TEXT,
 last_error TEXT,
 version BIGINT NOT NULL DEFAULT 1
);

CREATE TABLE push_deliveries (
 id TEXT PRIMARY KEY,
 event_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('request.created','chat.message')),
 resource_id TEXT NOT NULL,
 device_id TEXT NOT NULL,
 generation BIGINT NOT NULL,
 token TEXT NOT NULL,
 environment TEXT NOT NULL,
 relay_id TEXT,
 state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','sending','awaiting','succeeded','failed','cancelled','expired')),
 attempt_count BIGINT NOT NULL DEFAULT 0,
 retry_count BIGINT NOT NULL DEFAULT 0,
 manual_retry BIGINT NOT NULL DEFAULT 0,
 next_attempt_at DOUBLE PRECISION NOT NULL,
 created_at DOUBLE PRECISION NOT NULL,
 expires_at DOUBLE PRECISION NOT NULL,
 lease_until DOUBLE PRECISION,
 lease_token TEXT,
 error TEXT, submitted BIGINT NOT NULL DEFAULT 0, failure_count BIGINT NOT NULL DEFAULT 0, cancel_requested BIGINT NOT NULL DEFAULT 0,
 UNIQUE(event_id,device_id)
);

CREATE TABLE chat_settings (project_id TEXT PRIMARY KEY , enabled BIGINT NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)), origins TEXT NOT NULL DEFAULT '[]', version BIGINT NOT NULL DEFAULT 1);

CREATE TABLE chat_conversations (id TEXT PRIMARY KEY, project_id TEXT NOT NULL , state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','closed')), version BIGINT NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_sequence BIGINT NOT NULL DEFAULT 0, first_message_at TEXT, first_reply_at TEXT, last_alert_at DOUBLE PRECISION, customer_id TEXT , client_conversation_id TEXT, initial_ip_address TEXT, last_customer_ip_address TEXT, support_mode TEXT NOT NULL DEFAULT 'human' CHECK(support_mode IN ('ai','waiting_human','human')), mode_version BIGINT NOT NULL DEFAULT 1, follow_up_email TEXT, contact_version BIGINT NOT NULL DEFAULT 0);

CREATE TABLE chat_sessions (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL , token_hash TEXT NOT NULL UNIQUE, origin TEXT NOT NULL, created_at TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL, revoked_at TEXT, read_sequence BIGINT NOT NULL DEFAULT 0);

CREATE TABLE chat_reads (conversation_id TEXT NOT NULL , user_id TEXT NOT NULL , read_sequence BIGINT NOT NULL DEFAULT 0, PRIMARY KEY(conversation_id,user_id));

CREATE TABLE chat_customers (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL ,
    user_id TEXT ,
    kind TEXT NOT NULL CHECK(kind IN ('anonymous','external','native')),
    display_name TEXT CHECK(length(display_name)<=100),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id,user_id),
    CHECK((kind='anonymous' AND user_id IS NULL) OR (kind!='anonymous' AND user_id IS NOT NULL))
);

CREATE TABLE chat_customer_reads (
    customer_id TEXT NOT NULL ,
    conversation_id TEXT NOT NULL ,
    read_sequence BIGINT NOT NULL DEFAULT 0 CHECK(read_sequence>=0),
    PRIMARY KEY(customer_id,conversation_id)
);

CREATE TABLE chat_customer_sessions (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL ,
    token_hash TEXT NOT NULL UNIQUE,
    origin TEXT,
    issuing_key_id TEXT ,
    -- Native sessions are garbage collected after expiry. A missing parent
    -- invalidates this credential, without deleting the customer's history.
    parent_session_id TEXT,
    created_at TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    revoked_at TEXT,
    CHECK((issuing_key_id IS NULL) != (parent_session_id IS NULL))
);

CREATE TABLE chat_tickets (
    token_hash TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL ,
    kind TEXT NOT NULL CHECK(kind IN ('visitor','customer','admin')),
    session_id TEXT NOT NULL,
    user_id TEXT,
    origin TEXT,
    expires_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE account_activity (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL ,
    event_type TEXT NOT NULL CHECK(event_type IN ('signup','login','account_created','setup')),
    transport TEXT CHECK(transport IN ('cookie','native')),
    ip_address TEXT,
    occurred_at TEXT NOT NULL
);

CREATE TABLE ai_settings (
 project_id TEXT PRIMARY KEY , settings TEXT NOT NULL,
 version BIGINT NOT NULL DEFAULT 1, credential_version BIGINT NOT NULL DEFAULT 0,
 protected_key TEXT, catalogue TEXT NOT NULL DEFAULT '[]', capabilities TEXT NOT NULL DEFAULT '{}',
 capabilities_model TEXT, capabilities_credential_version BIGINT, updated_at TEXT NOT NULL
);

CREATE TABLE ai_runtime (
 singleton BIGINT PRIMARY KEY CHECK(singleton=1), version BIGINT NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'not_downloaded' CHECK(state IN ('not_downloaded','downloading','verifying','ready','failed','removing')),
 active_manifest TEXT, operation_id TEXT, action TEXT CHECK(action IN ('download','remove')),
 protected_token TEXT, authority TEXT, progress_bytes BIGINT NOT NULL DEFAULT 0, total_bytes BIGINT NOT NULL DEFAULT 0,
 lease_token TEXT, lease_until DOUBLE PRECISION, attempt_count BIGINT NOT NULL DEFAULT 0,
 error TEXT, updated_at TEXT NOT NULL
, active_cache_id TEXT);

CREATE TABLE ai_knowledge_state (
 project_id TEXT PRIMARY KEY , version BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE ai_sources (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL ,
 kind TEXT NOT NULL CHECK(kind IN ('markdown','website')), name TEXT NOT NULL,
 location TEXT, approved BIGINT NOT NULL DEFAULT 0 CHECK(approved IN (0,1)),
 active BIGINT NOT NULL DEFAULT 1 CHECK(active IN (0,1)), deleted_at TEXT,
 version BIGINT NOT NULL DEFAULT 1, active_revision_id TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, latest_document_id TEXT , pending_job_id TEXT , UNIQUE(id,project_id)
);

CREATE TABLE ai_revisions (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, project_id TEXT NOT NULL,
 source_version BIGINT NOT NULL, state TEXT NOT NULL CHECK(state IN ('staging','active','retired','discarded')),
 title TEXT NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL,
 profile TEXT NOT NULL, expected_chunks BIGINT NOT NULL CHECK(expected_chunks BETWEEN 1 AND 10000),
 job_id TEXT ,
 created_at TEXT NOT NULL, document_id TEXT , 
 UNIQUE(id,project_id)
);

CREATE TABLE ai_chunks (
 id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, project_id TEXT NOT NULL,
 ordinal BIGINT NOT NULL CHECK(ordinal>=0), title TEXT NOT NULL, content TEXT NOT NULL,
 location TEXT, vector BYTEA NOT NULL CHECK(length(vector)=3072), page_id TEXT ,
  UNIQUE(revision_id,ordinal)
, rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE);

CREATE TABLE "chat_messages" (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL , sequence BIGINT NOT NULL,
 client_message_id TEXT NOT NULL, sender TEXT NOT NULL CHECK(sender IN ('visitor','admin','ai')),
 sender_id TEXT NOT NULL, body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 4000), created_at TEXT NOT NULL, ip_address TEXT, citations TEXT NOT NULL DEFAULT '[]', follow_up_email_hash TEXT,
 UNIQUE(conversation_id,sequence), UNIQUE(conversation_id,sender_id,client_message_id)
);

CREATE TABLE ai_jobs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL ,
 kind TEXT NOT NULL CHECK(kind IN ('ingest','crawl','sections','preview','turn')),
 resource_id TEXT NOT NULL, dedupe_key TEXT NOT NULL, config_version BIGINT NOT NULL,
 authority TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','succeeded','failed','cancelled')),
 attempt_count BIGINT NOT NULL DEFAULT 0, max_attempts BIGINT NOT NULL DEFAULT 3,
 lease_token TEXT, lease_until DOUBLE PRECISION, next_attempt DOUBLE PRECISION NOT NULL,
 error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(project_id,kind,dedupe_key)
);

CREATE TABLE ai_usage (
 project_id TEXT NOT NULL , day TEXT NOT NULL,
 reserved_requests BIGINT NOT NULL DEFAULT 0, reserved_tokens BIGINT NOT NULL DEFAULT 0,
 reported_input_tokens BIGINT NOT NULL DEFAULT 0, reported_output_tokens BIGINT NOT NULL DEFAULT 0,
 PRIMARY KEY(project_id,day)
);

CREATE TABLE ai_usage_reservations (
 job_id TEXT NOT NULL , attempt BIGINT NOT NULL, call_index BIGINT NOT NULL DEFAULT 0, project_id TEXT NOT NULL ,
 day TEXT NOT NULL, requests BIGINT NOT NULL CHECK(requests>0), tokens BIGINT NOT NULL CHECK(tokens>0),
 dispatched BIGINT NOT NULL DEFAULT 0 CHECK(dispatched IN (0,1)),
 reported BIGINT NOT NULL DEFAULT 0 CHECK(reported IN (0,1)), provider_key_id TEXT, provider_key_version BIGINT, provider_health_epoch BIGINT, provider_group_id TEXT, provider_group_epoch BIGINT, provider_outcome TEXT, PRIMARY KEY(job_id,attempt,call_index)
);

CREATE TABLE ai_guest_owners (
 customer_id TEXT PRIMARY KEY , project_id TEXT NOT NULL ,
 user_id TEXT NOT NULL UNIQUE , created_at TEXT NOT NULL
);

CREATE TABLE ai_proposals (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL , conversation_id TEXT NOT NULL ,
 customer_id TEXT NOT NULL , kind TEXT NOT NULL CHECK(kind IN ('feature','bug')),
 board_id TEXT NOT NULL , title TEXT NOT NULL, description TEXT NOT NULL,
 version BIGINT NOT NULL DEFAULT 1, config_version BIGINT NOT NULL, mode_version BIGINT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','confirmed','cancelled')),
 client_id TEXT NOT NULL, created_at TEXT NOT NULL, authority TEXT, job_id TEXT , tool_call_id TEXT, expires_at DOUBLE PRECISION, draft_hash TEXT, updated_at TEXT, UNIQUE(conversation_id,client_id)
);

CREATE TABLE ai_reports (
 -- No request FK: the durable receipt must survive an allowed creator deletion.
 proposal_id TEXT PRIMARY KEY , request_id TEXT NOT NULL UNIQUE,
 project_id TEXT NOT NULL , conversation_id TEXT NOT NULL ,
 customer_id TEXT NOT NULL , creator_id TEXT NOT NULL , created_at TEXT NOT NULL
, guest_session_id TEXT, guest_origin TEXT);

CREATE TABLE ai_documents (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, project_id TEXT NOT NULL,
 source_version BIGINT NOT NULL, revision_number BIGINT NOT NULL CHECK(revision_number>0),
 filename TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
 content_hash TEXT NOT NULL, byte_count BIGINT NOT NULL CHECK(byte_count BETWEEN 1 AND 1048576),
 created_by TEXT NOT NULL , created_at TEXT NOT NULL,
 client_id TEXT NOT NULL, request_hash TEXT NOT NULL, job_id TEXT NOT NULL UNIQUE , crawl_run_id TEXT ,
 
 UNIQUE(source_id,revision_number), UNIQUE(project_id,client_id)
);

CREATE TABLE ai_crawl_runs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL , source_id TEXT NOT NULL,
 source_version BIGINT NOT NULL, version BIGINT NOT NULL DEFAULT 1,
 config TEXT NOT NULL, client_id TEXT NOT NULL, request_hash TEXT NOT NULL,
 job_id TEXT NOT NULL UNIQUE , document_id TEXT ,
 created_at TEXT NOT NULL, finished_at TEXT, stop_reason TEXT
    CHECK (stop_reason IS NULL OR stop_reason = 'page_limit'),
  UNIQUE(project_id,client_id)
, rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE);

CREATE TABLE ai_crawl_pages (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL , attempt BIGINT NOT NULL,
 url TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('fetched','failed','skipped')), error TEXT, created_at TEXT NOT NULL,
 UNIQUE(run_id,attempt,url)
);

CREATE TABLE ai_turns (
 job_id TEXT PRIMARY KEY , conversation_id TEXT NOT NULL ,
 trigger_message_id TEXT NOT NULL UNIQUE ,
 reply_message_id TEXT UNIQUE , reply_lease_token TEXT, reply_hash TEXT
);

CREATE TABLE ai_authoring_runs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL , job_id TEXT NOT NULL UNIQUE ,
 kind TEXT NOT NULL CHECK(kind IN ('preview','sections')), client_id TEXT NOT NULL, request_hash TEXT NOT NULL,
 config_version BIGINT NOT NULL, knowledge_version BIGINT NOT NULL,
 source_id TEXT , source_version BIGINT, query TEXT,
 version BIGINT NOT NULL DEFAULT 1, result TEXT, coverage TEXT,
 created_at TEXT NOT NULL, finished_at TEXT, UNIQUE(project_id,client_id)
);

CREATE TABLE ai_sections (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL , project_id TEXT NOT NULL ,
 kind TEXT NOT NULL CHECK(kind IN ('section','faq')), title TEXT NOT NULL, body TEXT NOT NULL, category TEXT NOT NULL,
 citation_ids TEXT NOT NULL, version BIGINT NOT NULL DEFAULT 1, deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE ai_section_versions (
 section_id TEXT NOT NULL , version BIGINT NOT NULL,
 title TEXT NOT NULL, body TEXT NOT NULL, category TEXT NOT NULL, citation_ids TEXT NOT NULL, deleted_at TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(section_id,version)
);

CREATE TABLE "admin_notifications" (
 sequence BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, id TEXT NOT NULL UNIQUE,
 event_id TEXT NOT NULL UNIQUE ,
 type TEXT NOT NULL CHECK(type IN ('request.created','request.resubmitted','chat.message_created','chat.support_mode_changed')),
 project_id TEXT NOT NULL , title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
 resource_kind TEXT NOT NULL CHECK(resource_kind IN ('request','conversation')), resource_id TEXT NOT NULL,
 created_at TEXT NOT NULL, read_at TEXT,
 CHECK((type IN ('request.created','request.resubmitted') AND resource_kind='request') OR (type IN ('chat.message_created','chat.support_mode_changed') AND resource_kind='conversation'))
);

CREATE TABLE ai_report_confirmations (
 customer_id TEXT NOT NULL , client_id TEXT NOT NULL,
 proposal_id TEXT NOT NULL , version BIGINT NOT NULL,
 draft_hash TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(customer_id,client_id)
);

CREATE TABLE ai_report_deletions (
 proposal_id TEXT PRIMARY KEY , request_id TEXT NOT NULL UNIQUE,
 version BIGINT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, deleted_at TEXT NOT NULL
);

CREATE TABLE ai_provider_quota_groups (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, label TEXT NOT NULL,
 health_epoch BIGINT NOT NULL DEFAULT 0, reset_epoch BIGINT NOT NULL DEFAULT 0, cooldown_until DOUBLE PRECISION NOT NULL DEFAULT 0,
 failure_count BIGINT NOT NULL DEFAULT 0, recovery_required BIGINT NOT NULL DEFAULT 0 CHECK(recovery_required IN (0,1)),
 reason TEXT, trial_job_id TEXT, trial_attempt BIGINT, trial_token TEXT, trial_until DOUBLE PRECISION,
 UNIQUE(provider,label)
);

CREATE TABLE ai_provider_attempts (
 job_id TEXT NOT NULL , attempt BIGINT NOT NULL,
 key_id TEXT NOT NULL , key_version BIGINT NOT NULL,
 health_epoch BIGINT NOT NULL, quota_group_id TEXT NOT NULL ,
 group_epoch BIGINT NOT NULL, lease_token TEXT NOT NULL,
 PRIMARY KEY(job_id,attempt)
);

CREATE TABLE ai_topic_policies (
 project_id TEXT PRIMARY KEY ,
 version BIGINT NOT NULL DEFAULT 0, policy TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE ai_topic_receipts (
 job_id TEXT NOT NULL , attempt BIGINT NOT NULL,
 lease_token TEXT NOT NULL, policy_version BIGINT NOT NULL,
 preset_version TEXT NOT NULL, context_hash TEXT NOT NULL, trigger_sequence BIGINT,
 outcome TEXT NOT NULL CHECK(outcome IN ('allow','refuse','clarify','greeting','unavailable')),
 probability DOUBLE PRECISION CHECK(probability>=0 AND probability<=1), model TEXT,
 key_id TEXT, key_version BIGINT, health_epoch BIGINT, group_epoch BIGINT,
 body TEXT, error TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(job_id,attempt)
);

CREATE TABLE ai_decision_attempts (
 job_id TEXT NOT NULL , attempt BIGINT NOT NULL,
 lease_token TEXT NOT NULL, key_id TEXT NOT NULL ,
 key_version BIGINT NOT NULL, health_epoch BIGINT NOT NULL, group_epoch BIGINT NOT NULL,
 PRIMARY KEY(job_id,attempt)
);

CREATE TABLE ai_decision_checks (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL ,
 policy_version BIGINT NOT NULL, key_id TEXT NOT NULL ,
 key_version BIGINT NOT NULL, health_epoch BIGINT NOT NULL, group_epoch BIGINT NOT NULL,
 principal TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('running','succeeded','failed')),
 lease_until DOUBLE PRECISION NOT NULL, model TEXT NOT NULL, day TEXT NOT NULL,
 reserved_tokens BIGINT NOT NULL, reported BIGINT NOT NULL DEFAULT 0,
 outcome TEXT, probability DOUBLE PRECISION, error TEXT, created_at TEXT NOT NULL
);

CREATE TABLE "ai_provider_keys" (
 id TEXT PRIMARY KEY, project_id TEXT , provider TEXT NOT NULL,
 label TEXT NOT NULL, quota_group_id TEXT NOT NULL ,
 version BIGINT NOT NULL DEFAULT 1, enabled BIGINT NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 is_default BIGINT NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)), protected_key TEXT,
 catalogue TEXT NOT NULL DEFAULT '[]', capabilities TEXT NOT NULL DEFAULT '{}',
 capabilities_model TEXT, capabilities_key_version BIGINT,
 blocked_reason TEXT, reason TEXT, health_epoch BIGINT NOT NULL DEFAULT 0,
 last_selected BIGINT NOT NULL DEFAULT 0, deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 purpose TEXT NOT NULL DEFAULT 'generation' CHECK(purpose IN ('generation','decision','both'))
);

CREATE TABLE ai_generation_keys (
 project_id TEXT NOT NULL , key_id TEXT NOT NULL ,
 is_default BIGINT NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)), PRIMARY KEY(project_id,key_id)
);

CREATE TABLE "ai_decision_keys" (
 key_id TEXT NOT NULL , project_id TEXT NOT NULL ,
 catalogue TEXT NOT NULL DEFAULT '[]', model TEXT, key_version BIGINT,
 verified BIGINT NOT NULL DEFAULT 0 CHECK(verified IN (0,1)), PRIMARY KEY(project_id,key_id)
);

CREATE TABLE ai_key_model_evidence (
 key_id TEXT NOT NULL , role TEXT NOT NULL CHECK(role IN ('generation','decision')),
 model TEXT NOT NULL, key_version BIGINT NOT NULL, capabilities TEXT NOT NULL DEFAULT '{}',
 verified BIGINT NOT NULL DEFAULT 0 CHECK(verified IN (0,1)), PRIMARY KEY(key_id,role,model)
);

CREATE TABLE ai_key_decision_catalogues (
 key_id TEXT PRIMARY KEY , catalogue TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE ai_key_library (
 singleton BIGINT PRIMARY KEY CHECK(singleton=1), version BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE ai_provider_admin_checks (
 id TEXT PRIMARY KEY, token TEXT NOT NULL, key_id TEXT NOT NULL ,
 quota_group_id TEXT NOT NULL , lease_until DOUBLE PRECISION NOT NULL
);

CREATE TABLE ai_section_publications (
 section_id TEXT NOT NULL , section_version BIGINT NOT NULL,
 source_id TEXT NOT NULL UNIQUE , approved_by TEXT NOT NULL ,
 created_at TEXT NOT NULL, PRIMARY KEY(section_id,section_version));

CREATE TABLE connected_apps (
    project_id TEXT PRIMARY KEY ,
    id TEXT NOT NULL UNIQUE,
    version BIGINT NOT NULL DEFAULT 1,
    base_url TEXT,
    protected_token TEXT,
    enabled BIGINT NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    app_instance_id TEXT UNIQUE,
    link_origins TEXT NOT NULL DEFAULT '[]',
    support_linked BIGINT NOT NULL DEFAULT 0 CHECK(support_linked IN (0,1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE connected_app_audit (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL ,
    actor_id TEXT NOT NULL ,
    action TEXT NOT NULL,
    version BIGINT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE connected_app_preferences (
    singleton BIGINT PRIMARY KEY CHECK(singleton=1),
    version BIGINT NOT NULL,
    timezone TEXT NOT NULL
);

CREATE TABLE chat_business_hours (
    scope TEXT PRIMARY KEY,
    project_id TEXT UNIQUE ,
    mode TEXT NOT NULL CHECK(mode IN ('inherit','override','off')),
    schedule TEXT NOT NULL,
    version BIGINT NOT NULL CHECK(version >= 1),
    updated_at TEXT NOT NULL
);

CREATE TABLE chat_business_hours_audit (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    actor_id TEXT NOT NULL ,
    version BIGINT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE resource_archive_events (id TEXT PRIMARY KEY, resource_kind TEXT NOT NULL CHECK(resource_kind IN ('project','board')), resource_id TEXT NOT NULL, project_id TEXT NOT NULL , actor_id TEXT NOT NULL , archived BIGINT NOT NULL CHECK(archived IN (0,1)), version BIGINT NOT NULL, occurred_at TEXT NOT NULL);

ALTER TABLE local_credentials ADD CONSTRAINT local_credentials_fk_0 FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE sessions ADD CONSTRAINT sessions_fk_0 FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE projects ADD CONSTRAINT projects_fk_0 FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE boards ADD CONSTRAINT boards_fk_0 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE external_identities ADD CONSTRAINT external_identities_fk_0 FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE external_identities ADD CONSTRAINT external_identities_fk_1 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE requests ADD CONSTRAINT requests_fk_0 FOREIGN KEY (creator_id) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE requests ADD CONSTRAINT requests_fk_1 FOREIGN KEY (board_id) REFERENCES boards(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE votes ADD CONSTRAINT votes_fk_0 FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE votes ADD CONSTRAINT votes_fk_1 FOREIGN KEY (request_id) REFERENCES requests(id) ON DELETE CASCADE ON UPDATE NO ACTION;

ALTER TABLE api_keys ADD CONSTRAINT api_keys_fk_0 FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE api_keys ADD CONSTRAINT api_keys_fk_1 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE request_events ADD CONSTRAINT request_events_fk_0 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE webhook_deliveries ADD CONSTRAINT webhook_deliveries_fk_0 FOREIGN KEY (event_id) REFERENCES request_events(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE devices ADD CONSTRAINT devices_fk_0 FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE ON UPDATE NO ACTION;

ALTER TABLE devices ADD CONSTRAINT devices_fk_1 FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_settings ADD CONSTRAINT chat_settings_fk_0 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_conversations ADD CONSTRAINT chat_conversations_fk_0 FOREIGN KEY (customer_id) REFERENCES chat_customers(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_conversations ADD CONSTRAINT chat_conversations_fk_1 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_sessions ADD CONSTRAINT chat_sessions_fk_0 FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_reads ADD CONSTRAINT chat_reads_fk_0 FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_reads ADD CONSTRAINT chat_reads_fk_1 FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_customers ADD CONSTRAINT chat_customers_fk_0 FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_customers ADD CONSTRAINT chat_customers_fk_1 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_customer_reads ADD CONSTRAINT chat_customer_reads_fk_0 FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_customer_reads ADD CONSTRAINT chat_customer_reads_fk_1 FOREIGN KEY (customer_id) REFERENCES chat_customers(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_customer_sessions ADD CONSTRAINT chat_customer_sessions_fk_0 FOREIGN KEY (issuing_key_id) REFERENCES api_keys(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_customer_sessions ADD CONSTRAINT chat_customer_sessions_fk_1 FOREIGN KEY (customer_id) REFERENCES chat_customers(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_tickets ADD CONSTRAINT chat_tickets_fk_0 FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE account_activity ADD CONSTRAINT account_activity_fk_0 FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_settings ADD CONSTRAINT ai_settings_fk_0 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_knowledge_state ADD CONSTRAINT ai_knowledge_state_fk_0 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_sources ADD CONSTRAINT ai_sources_fk_0 FOREIGN KEY (pending_job_id) REFERENCES ai_jobs(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_sources ADD CONSTRAINT ai_sources_fk_1 FOREIGN KEY (latest_document_id) REFERENCES ai_documents(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_sources ADD CONSTRAINT ai_sources_fk_2 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_revisions ADD CONSTRAINT ai_revisions_fk_0 FOREIGN KEY (source_id,project_id) REFERENCES ai_sources(id,project_id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_revisions ADD CONSTRAINT ai_revisions_fk_1 FOREIGN KEY (document_id) REFERENCES ai_documents(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_revisions ADD CONSTRAINT ai_revisions_fk_2 FOREIGN KEY (job_id) REFERENCES ai_jobs(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_chunks ADD CONSTRAINT ai_chunks_fk_0 FOREIGN KEY (revision_id,project_id) REFERENCES ai_revisions(id,project_id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_chunks ADD CONSTRAINT ai_chunks_fk_1 FOREIGN KEY (page_id) REFERENCES ai_crawl_pages(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_messages ADD CONSTRAINT chat_messages_fk_0 FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_jobs ADD CONSTRAINT ai_jobs_fk_0 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_usage ADD CONSTRAINT ai_usage_fk_0 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_usage_reservations ADD CONSTRAINT ai_usage_reservations_fk_0 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_usage_reservations ADD CONSTRAINT ai_usage_reservations_fk_1 FOREIGN KEY (job_id) REFERENCES ai_jobs(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_guest_owners ADD CONSTRAINT ai_guest_owners_fk_0 FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_guest_owners ADD CONSTRAINT ai_guest_owners_fk_1 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_guest_owners ADD CONSTRAINT ai_guest_owners_fk_2 FOREIGN KEY (customer_id) REFERENCES chat_customers(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_proposals ADD CONSTRAINT ai_proposals_fk_0 FOREIGN KEY (job_id) REFERENCES ai_jobs(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_proposals ADD CONSTRAINT ai_proposals_fk_1 FOREIGN KEY (board_id) REFERENCES boards(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_proposals ADD CONSTRAINT ai_proposals_fk_2 FOREIGN KEY (customer_id) REFERENCES chat_customers(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_proposals ADD CONSTRAINT ai_proposals_fk_3 FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_proposals ADD CONSTRAINT ai_proposals_fk_4 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_reports ADD CONSTRAINT ai_reports_fk_0 FOREIGN KEY (creator_id) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_reports ADD CONSTRAINT ai_reports_fk_1 FOREIGN KEY (customer_id) REFERENCES chat_customers(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_reports ADD CONSTRAINT ai_reports_fk_2 FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_reports ADD CONSTRAINT ai_reports_fk_3 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_reports ADD CONSTRAINT ai_reports_fk_4 FOREIGN KEY (proposal_id) REFERENCES ai_proposals(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_documents ADD CONSTRAINT ai_documents_fk_0 FOREIGN KEY (source_id,project_id) REFERENCES ai_sources(id,project_id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_documents ADD CONSTRAINT ai_documents_fk_1 FOREIGN KEY (crawl_run_id) REFERENCES ai_crawl_runs(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_documents ADD CONSTRAINT ai_documents_fk_2 FOREIGN KEY (job_id) REFERENCES ai_jobs(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_documents ADD CONSTRAINT ai_documents_fk_3 FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_crawl_runs ADD CONSTRAINT ai_crawl_runs_fk_0 FOREIGN KEY (source_id,project_id) REFERENCES ai_sources(id,project_id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_crawl_runs ADD CONSTRAINT ai_crawl_runs_fk_1 FOREIGN KEY (document_id) REFERENCES ai_documents(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_crawl_runs ADD CONSTRAINT ai_crawl_runs_fk_2 FOREIGN KEY (job_id) REFERENCES ai_jobs(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_crawl_runs ADD CONSTRAINT ai_crawl_runs_fk_3 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_crawl_pages ADD CONSTRAINT ai_crawl_pages_fk_0 FOREIGN KEY (run_id) REFERENCES ai_crawl_runs(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_turns ADD CONSTRAINT ai_turns_fk_0 FOREIGN KEY (reply_message_id) REFERENCES chat_messages(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_turns ADD CONSTRAINT ai_turns_fk_1 FOREIGN KEY (trigger_message_id) REFERENCES chat_messages(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_turns ADD CONSTRAINT ai_turns_fk_2 FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_turns ADD CONSTRAINT ai_turns_fk_3 FOREIGN KEY (job_id) REFERENCES ai_jobs(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_authoring_runs ADD CONSTRAINT ai_authoring_runs_fk_0 FOREIGN KEY (source_id) REFERENCES ai_sources(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_authoring_runs ADD CONSTRAINT ai_authoring_runs_fk_1 FOREIGN KEY (job_id) REFERENCES ai_jobs(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_authoring_runs ADD CONSTRAINT ai_authoring_runs_fk_2 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_sections ADD CONSTRAINT ai_sections_fk_0 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_sections ADD CONSTRAINT ai_sections_fk_1 FOREIGN KEY (run_id) REFERENCES ai_authoring_runs(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_section_versions ADD CONSTRAINT ai_section_versions_fk_0 FOREIGN KEY (section_id) REFERENCES ai_sections(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE admin_notifications ADD CONSTRAINT admin_notifications_fk_0 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE admin_notifications ADD CONSTRAINT admin_notifications_fk_1 FOREIGN KEY (event_id) REFERENCES request_events(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_report_confirmations ADD CONSTRAINT ai_report_confirmations_fk_0 FOREIGN KEY (proposal_id) REFERENCES ai_proposals(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_report_confirmations ADD CONSTRAINT ai_report_confirmations_fk_1 FOREIGN KEY (customer_id) REFERENCES chat_customers(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_report_deletions ADD CONSTRAINT ai_report_deletions_fk_0 FOREIGN KEY (proposal_id) REFERENCES ai_reports(proposal_id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_provider_attempts ADD CONSTRAINT ai_provider_attempts_fk_0 FOREIGN KEY (quota_group_id) REFERENCES ai_provider_quota_groups(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_provider_attempts ADD CONSTRAINT ai_provider_attempts_fk_1 FOREIGN KEY (key_id) REFERENCES ai_provider_keys(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_provider_attempts ADD CONSTRAINT ai_provider_attempts_fk_2 FOREIGN KEY (job_id) REFERENCES ai_jobs(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_topic_policies ADD CONSTRAINT ai_topic_policies_fk_0 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_topic_receipts ADD CONSTRAINT ai_topic_receipts_fk_0 FOREIGN KEY (job_id) REFERENCES ai_jobs(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_decision_attempts ADD CONSTRAINT ai_decision_attempts_fk_0 FOREIGN KEY (key_id) REFERENCES ai_provider_keys(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_decision_attempts ADD CONSTRAINT ai_decision_attempts_fk_1 FOREIGN KEY (job_id) REFERENCES ai_jobs(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_decision_checks ADD CONSTRAINT ai_decision_checks_fk_0 FOREIGN KEY (key_id) REFERENCES ai_provider_keys(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_decision_checks ADD CONSTRAINT ai_decision_checks_fk_1 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_provider_keys ADD CONSTRAINT ai_provider_keys_fk_0 FOREIGN KEY (quota_group_id) REFERENCES ai_provider_quota_groups(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_provider_keys ADD CONSTRAINT ai_provider_keys_fk_1 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_generation_keys ADD CONSTRAINT ai_generation_keys_fk_0 FOREIGN KEY (key_id) REFERENCES ai_provider_keys(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_generation_keys ADD CONSTRAINT ai_generation_keys_fk_1 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_decision_keys ADD CONSTRAINT ai_decision_keys_fk_0 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_decision_keys ADD CONSTRAINT ai_decision_keys_fk_1 FOREIGN KEY (key_id) REFERENCES ai_provider_keys(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_key_model_evidence ADD CONSTRAINT ai_key_model_evidence_fk_0 FOREIGN KEY (key_id) REFERENCES ai_provider_keys(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_key_decision_catalogues ADD CONSTRAINT ai_key_decision_catalogues_fk_0 FOREIGN KEY (key_id) REFERENCES ai_provider_keys(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_provider_admin_checks ADD CONSTRAINT ai_provider_admin_checks_fk_0 FOREIGN KEY (quota_group_id) REFERENCES ai_provider_quota_groups(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_provider_admin_checks ADD CONSTRAINT ai_provider_admin_checks_fk_1 FOREIGN KEY (key_id) REFERENCES ai_provider_keys(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_section_publications ADD CONSTRAINT ai_section_publications_fk_0 FOREIGN KEY (section_id,section_version) REFERENCES ai_section_versions(section_id,version) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_section_publications ADD CONSTRAINT ai_section_publications_fk_1 FOREIGN KEY (approved_by) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_section_publications ADD CONSTRAINT ai_section_publications_fk_2 FOREIGN KEY (source_id) REFERENCES ai_sources(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE ai_section_publications ADD CONSTRAINT ai_section_publications_fk_3 FOREIGN KEY (section_id) REFERENCES ai_sections(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE connected_apps ADD CONSTRAINT connected_apps_fk_0 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE connected_app_audit ADD CONSTRAINT connected_app_audit_fk_0 FOREIGN KEY (actor_id) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE connected_app_audit ADD CONSTRAINT connected_app_audit_fk_1 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_business_hours ADD CONSTRAINT chat_business_hours_fk_0 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE chat_business_hours_audit ADD CONSTRAINT chat_business_hours_audit_fk_0 FOREIGN KEY (actor_id) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE resource_archive_events ADD CONSTRAINT resource_archive_events_fk_0 FOREIGN KEY (actor_id) REFERENCES users(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

ALTER TABLE resource_archive_events ADD CONSTRAINT resource_archive_events_fk_1 FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE NO ACTION ON UPDATE NO ACTION;

CREATE INDEX sessions_expiry ON sessions(expires_at);

CREATE INDEX requests_board_moderation ON requests(board_id,moderation,created_at,id);

CREATE INDEX requests_creator ON requests(creator_id,created_at,id);

CREATE INDEX requests_status ON requests(status,created_at,id) WHERE moderation='accepted';

CREATE INDEX votes_user ON votes(user_id,request_id);

CREATE INDEX events_request ON request_events(request_id,occurred_at,id);

CREATE INDEX deliveries_due ON webhook_deliveries(state,next_attempt_at,lease_until);

CREATE INDEX deliveries_event ON webhook_deliveries(event_id);

CREATE INDEX devices_owner ON devices(user_id,id);

CREATE INDEX push_due ON push_deliveries(state,next_attempt_at,lease_until);

CREATE INDEX chat_conversations_project ON chat_conversations(project_id,state,updated_at,id);

CREATE INDEX chat_response_samples ON chat_conversations(project_id,first_message_at);

CREATE INDEX chat_sessions_conversation ON chat_sessions(conversation_id);

CREATE UNIQUE INDEX chat_conversation_client ON chat_conversations(customer_id,client_conversation_id);

CREATE INDEX chat_customer_conversations ON chat_conversations(customer_id,updated_at,id);

CREATE INDEX chat_customer_sessions_customer ON chat_customer_sessions(customer_id);

CREATE INDEX chat_customer_sessions_expiry ON chat_customer_sessions(expires_at);

CREATE INDEX chat_tickets_expiry ON chat_tickets(expires_at);

CREATE INDEX account_activity_time ON account_activity(occurred_at DESC,id DESC);

CREATE INDEX account_activity_user_time ON account_activity(user_id,occurred_at DESC,id DESC);

CREATE INDEX ai_sources_project ON ai_sources(project_id,created_at,id);

CREATE UNIQUE INDEX ai_revision_active ON ai_revisions(source_id) WHERE state='active';

CREATE INDEX ai_chunks_revision ON ai_chunks(revision_id,ordinal);

CREATE INDEX ai_jobs_due ON ai_jobs(state,next_attempt,lease_until);

CREATE INDEX ai_crawl_pages_run ON ai_crawl_pages(run_id,attempt,status);

CREATE INDEX ai_chunks_project ON ai_chunks(project_id,revision_id);

CREATE INDEX ai_turns_conversation ON ai_turns(conversation_id,job_id);

CREATE INDEX admin_notifications_unread ON admin_notifications(sequence) WHERE read_at IS NULL;

CREATE UNIQUE INDEX ai_proposal_turn_kind ON ai_proposals(job_id,kind) WHERE job_id IS NOT NULL;

CREATE INDEX ai_proposal_customer_created ON ai_proposals(customer_id,created_at);

CREATE INDEX ai_report_confirmations_proposal ON ai_report_confirmations(proposal_id);

CREATE INDEX ai_reports_customer_created ON ai_reports(customer_id,created_at);

CREATE UNIQUE INDEX ai_generation_default ON ai_generation_keys(project_id) WHERE is_default=1;

CREATE INDEX ai_account_key_pool ON ai_provider_keys(provider,deleted_at);

CREATE INDEX ai_generation_key_projects ON ai_generation_keys(key_id,project_id);

CREATE INDEX ai_decision_key_projects ON ai_decision_keys(key_id,project_id);

CREATE INDEX ai_admin_check_key ON ai_provider_admin_checks(key_id,lease_until);

CREATE FUNCTION accepted_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.accepted_at IS NOT NULL AND NEW.accepted_at IS DISTINCT FROM OLD.accepted_at THEN
    RAISE EXCEPTION 'accepted_at is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER accepted_immutable BEFORE UPDATE OF accepted_at ON requests
FOR EACH ROW EXECUTE FUNCTION accepted_immutable_guard();

CREATE FUNCTION chat_customer_identity_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.kind IS DISTINCT FROM OLD.kind THEN
    RAISE EXCEPTION 'Customer identity is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER chat_customer_identity_immutable BEFORE UPDATE OF project_id,user_id,kind ON chat_customers
FOR EACH ROW EXECUTE FUNCTION chat_customer_identity_immutable_guard();

CREATE FUNCTION chat_conversation_customer_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS(SELECT 1 FROM chat_customers WHERE id=NEW.customer_id AND project_id=NEW.project_id) THEN
    RAISE EXCEPTION 'Conversation customer must belong to its project' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER chat_conversation_customer_insert BEFORE INSERT ON chat_conversations
FOR EACH ROW EXECUTE FUNCTION chat_conversation_customer_insert_guard();

CREATE FUNCTION chat_conversation_customer_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.customer_id IS DISTINCT FROM OLD.customer_id OR NEW.project_id IS DISTINCT FROM OLD.project_id THEN
    RAISE EXCEPTION 'Conversation ownership is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER chat_conversation_customer_immutable BEFORE UPDATE OF customer_id,project_id ON chat_conversations
FOR EACH ROW EXECUTE FUNCTION chat_conversation_customer_immutable_guard();

CREATE FUNCTION ai_source_scope_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.id!=OLD.id OR NEW.project_id!=OLD.project_id OR NEW.kind!=OLD.kind THEN
    RAISE EXCEPTION 'Knowledge source scope is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_source_scope_immutable BEFORE UPDATE ON ai_sources
FOR EACH ROW EXECUTE FUNCTION ai_source_scope_immutable_guard();

CREATE FUNCTION ai_revision_content_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.id!=OLD.id OR NEW.source_id!=OLD.source_id OR NEW.project_id!=OLD.project_id
 OR NEW.source_version!=OLD.source_version OR NEW.title!=OLD.title OR NEW.content!=OLD.content
 OR NEW.content_hash!=OLD.content_hash OR NEW.profile!=OLD.profile OR NEW.expected_chunks!=OLD.expected_chunks
 OR NEW.job_id IS DISTINCT FROM OLD.job_id THEN
    RAISE EXCEPTION 'Knowledge revision content is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_revision_content_immutable BEFORE UPDATE ON ai_revisions
FOR EACH ROW EXECUTE FUNCTION ai_revision_content_immutable_guard();

CREATE FUNCTION ai_chunks_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Knowledge chunks are immutable' USING ERRCODE = '23514';
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_chunks_immutable BEFORE UPDATE ON ai_chunks
FOR EACH ROW EXECUTE FUNCTION ai_chunks_immutable_guard();

CREATE FUNCTION ai_guest_owner_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS(
 SELECT 1 FROM chat_customers WHERE id=NEW.customer_id AND project_id=NEW.project_id AND kind='anonymous' AND user_id IS NULL
) THEN
    RAISE EXCEPTION 'Guest owner must belong to anonymous customer project' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_guest_owner_insert BEFORE INSERT ON ai_guest_owners
FOR EACH ROW EXECUTE FUNCTION ai_guest_owner_insert_guard();

CREATE FUNCTION ai_guest_owner_user_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS(
 SELECT 1 FROM users WHERE id=NEW.user_id AND role='participant'
) OR EXISTS(SELECT 1 FROM local_credentials WHERE user_id=NEW.user_id)
 OR EXISTS(SELECT 1 FROM external_identities WHERE user_id=NEW.user_id) THEN
    RAISE EXCEPTION 'Guest owner must be credentialless' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_guest_owner_user BEFORE INSERT ON ai_guest_owners
FOR EACH ROW EXECUTE FUNCTION ai_guest_owner_user_guard();

CREATE FUNCTION ai_guest_owner_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Guest ownership is immutable' USING ERRCODE = '23514';
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_guest_owner_immutable BEFORE UPDATE ON ai_guest_owners
FOR EACH ROW EXECUTE FUNCTION ai_guest_owner_immutable_guard();

CREATE FUNCTION ai_proposal_scope_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS(
 SELECT 1 FROM chat_conversations cv JOIN boards b ON b.id=NEW.board_id
 WHERE cv.id=NEW.conversation_id AND cv.project_id=NEW.project_id
 AND cv.customer_id=NEW.customer_id AND b.project_id=NEW.project_id
) THEN
    RAISE EXCEPTION 'Proposal scope mismatch' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_proposal_scope BEFORE INSERT ON ai_proposals
FOR EACH ROW EXECUTE FUNCTION ai_proposal_scope_guard();

CREATE FUNCTION ai_proposal_scope_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.id!=OLD.id OR NEW.project_id!=OLD.project_id OR NEW.conversation_id!=OLD.conversation_id
 OR NEW.customer_id!=OLD.customer_id OR NEW.kind!=OLD.kind OR NEW.board_id!=OLD.board_id THEN
    RAISE EXCEPTION 'Proposal ownership is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_proposal_scope_immutable BEFORE UPDATE ON ai_proposals
FOR EACH ROW EXECUTE FUNCTION ai_proposal_scope_immutable_guard();

CREATE FUNCTION ai_report_scope_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS(
 SELECT 1 FROM ai_proposals p JOIN requests r ON r.id=NEW.request_id
 WHERE p.id=NEW.proposal_id AND p.project_id=NEW.project_id AND p.conversation_id=NEW.conversation_id
 AND p.customer_id=NEW.customer_id AND r.board_id=p.board_id AND r.creator_id=NEW.creator_id
) THEN
    RAISE EXCEPTION 'Report scope mismatch' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_report_scope BEFORE INSERT ON ai_reports
FOR EACH ROW EXECUTE FUNCTION ai_report_scope_guard();

CREATE FUNCTION ai_report_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Report ownership and receipt are immutable' USING ERRCODE = '23514';
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_report_immutable BEFORE UPDATE ON ai_reports
FOR EACH ROW EXECUTE FUNCTION ai_report_immutable_guard();

CREATE FUNCTION ai_document_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Original knowledge documents are immutable' USING ERRCODE = '23514';
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_document_immutable BEFORE UPDATE ON ai_documents
FOR EACH ROW EXECUTE FUNCTION ai_document_immutable_guard();

CREATE FUNCTION ai_revision_document_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.document_id IS DISTINCT FROM OLD.document_id THEN
    RAISE EXCEPTION 'Revision document provenance is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_revision_document_immutable BEFORE UPDATE ON ai_revisions
FOR EACH ROW EXECUTE FUNCTION ai_revision_document_immutable_guard();

CREATE FUNCTION ai_crawl_page_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Crawled page originals are immutable' USING ERRCODE = '23514';
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_crawl_page_immutable BEFORE UPDATE ON ai_crawl_pages
FOR EACH ROW EXECUTE FUNCTION ai_crawl_page_immutable_guard();

CREATE FUNCTION ai_section_history_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Derived section history is immutable' USING ERRCODE = '23514';
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_section_history_immutable BEFORE UPDATE ON ai_section_versions
FOR EACH ROW EXECUTE FUNCTION ai_section_history_immutable_guard();

CREATE FUNCTION admin_notification_event_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.sequence IS DISTINCT FROM OLD.sequence OR NEW.id IS DISTINCT FROM OLD.id OR NEW.event_id IS DISTINCT FROM OLD.event_id
 OR NEW.type IS DISTINCT FROM OLD.type OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.title IS DISTINCT FROM OLD.title
 OR NEW.resource_kind IS DISTINCT FROM OLD.resource_kind OR NEW.resource_id IS DISTINCT FROM OLD.resource_id OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'Notification event is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER admin_notification_event_immutable BEFORE UPDATE OF sequence,id,event_id,type,project_id,title,resource_kind,resource_id,created_at ON admin_notifications
FOR EACH ROW EXECUTE FUNCTION admin_notification_event_immutable_guard();

CREATE FUNCTION ai_report_confirmation_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Report confirmation receipt is immutable' USING ERRCODE = '23514';
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_report_confirmation_immutable BEFORE UPDATE ON ai_report_confirmations
FOR EACH ROW EXECUTE FUNCTION ai_report_confirmation_immutable_guard();

CREATE FUNCTION ai_report_deletion_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Report deletion receipt is immutable' USING ERRCODE = '23514';
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_report_deletion_immutable BEFORE UPDATE ON ai_report_deletions
FOR EACH ROW EXECUTE FUNCTION ai_report_deletion_immutable_guard();

CREATE FUNCTION ai_guest_no_local_credentials_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS(SELECT 1 FROM ai_guest_owners WHERE user_id=NEW.user_id) THEN
    RAISE EXCEPTION 'Guest actors cannot acquire credentials' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_guest_no_local_credentials BEFORE INSERT ON local_credentials
FOR EACH ROW EXECUTE FUNCTION ai_guest_no_local_credentials_guard();

CREATE FUNCTION ai_guest_no_external_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS(SELECT 1 FROM ai_guest_owners WHERE user_id=NEW.user_id) THEN
    RAISE EXCEPTION 'Guest actors cannot acquire external identities' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_guest_no_external_identity BEFORE INSERT ON external_identities
FOR EACH ROW EXECUTE FUNCTION ai_guest_no_external_identity_guard();

CREATE FUNCTION ai_guest_no_native_session_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS(SELECT 1 FROM ai_guest_owners WHERE user_id=NEW.user_id) THEN
    RAISE EXCEPTION 'Guest actors cannot acquire native sessions' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_guest_no_native_session BEFORE INSERT ON sessions
FOR EACH ROW EXECUTE FUNCTION ai_guest_no_native_session_guard();

CREATE FUNCTION ai_guest_no_admin_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.role!='participant' AND EXISTS(SELECT 1 FROM ai_guest_owners WHERE user_id=NEW.id) THEN
    RAISE EXCEPTION 'Guest actors cannot become administrators' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_guest_no_admin BEFORE UPDATE OF role ON users
FOR EACH ROW EXECUTE FUNCTION ai_guest_no_admin_guard();

CREATE FUNCTION ai_guest_no_votes_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS(SELECT 1 FROM ai_guest_owners WHERE user_id=NEW.user_id) THEN
    RAISE EXCEPTION 'Guest actors cannot vote' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_guest_no_votes BEFORE INSERT ON votes
FOR EACH ROW EXECUTE FUNCTION ai_guest_no_votes_guard();

CREATE FUNCTION ai_section_publications_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Knowledge approval provenance is immutable' USING ERRCODE = '23514';
  RETURN NEW;
END;
$$;
CREATE TRIGGER ai_section_publications_immutable BEFORE UPDATE ON ai_section_publications
FOR EACH ROW EXECUTE FUNCTION ai_section_publications_immutable_guard();

CREATE FUNCTION resource_archive_events_no_update_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Archive history is immutable' USING ERRCODE = '23514';
  RETURN NEW;
END;
$$;
CREATE TRIGGER resource_archive_events_no_update BEFORE UPDATE ON resource_archive_events
FOR EACH ROW EXECUTE FUNCTION resource_archive_events_no_update_guard();

CREATE FUNCTION resource_archive_events_no_delete_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Archive history is immutable' USING ERRCODE = '23514';
  RETURN OLD;
END;
$$;
CREATE TRIGGER resource_archive_events_no_delete BEFORE DELETE ON resource_archive_events
FOR EACH ROW EXECUTE FUNCTION resource_archive_events_no_delete_guard();

INSERT INTO ai_runtime(singleton,updated_at) VALUES(1,to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'));
INSERT INTO ai_key_library(singleton) VALUES(1);
INSERT INTO connected_app_preferences VALUES(1,1,'UTC');
