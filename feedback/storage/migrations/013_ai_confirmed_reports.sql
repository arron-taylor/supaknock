-- Model-created drafts never create an account or request. Confirmation does.
ALTER TABLE ai_proposals ADD COLUMN authority TEXT;
ALTER TABLE ai_proposals ADD COLUMN job_id TEXT REFERENCES ai_jobs(id);
ALTER TABLE ai_proposals ADD COLUMN tool_call_id TEXT;
ALTER TABLE ai_proposals ADD COLUMN expires_at REAL;
ALTER TABLE ai_proposals ADD COLUMN draft_hash TEXT;
ALTER TABLE ai_proposals ADD COLUMN updated_at TEXT;
CREATE UNIQUE INDEX ai_proposal_turn_kind ON ai_proposals(job_id,kind) WHERE job_id IS NOT NULL;
CREATE INDEX ai_proposal_customer_created ON ai_proposals(customer_id,created_at);
CREATE TABLE ai_report_confirmations (
 customer_id TEXT NOT NULL REFERENCES chat_customers(id), client_id TEXT NOT NULL,
 proposal_id TEXT NOT NULL REFERENCES ai_proposals(id), version INTEGER NOT NULL,
 draft_hash TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(customer_id,client_id)
);
CREATE INDEX ai_report_confirmations_proposal ON ai_report_confirmations(proposal_id);
ALTER TABLE ai_reports ADD COLUMN guest_session_id TEXT;
ALTER TABLE ai_reports ADD COLUMN guest_origin TEXT;
CREATE INDEX ai_reports_customer_created ON ai_reports(customer_id,created_at);
CREATE TABLE ai_report_deletions (
 proposal_id TEXT PRIMARY KEY REFERENCES ai_reports(proposal_id), request_id TEXT NOT NULL UNIQUE,
 version INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, deleted_at TEXT NOT NULL
);
CREATE TRIGGER ai_report_confirmation_immutable BEFORE UPDATE ON ai_report_confirmations
 BEGIN SELECT RAISE(ABORT,'Report confirmation receipt is immutable'); END;
CREATE TRIGGER ai_report_deletion_immutable BEFORE UPDATE ON ai_report_deletions
 BEGIN SELECT RAISE(ABORT,'Report deletion receipt is immutable'); END;
-- The private guest actor must never become an account, external identity or voter.
CREATE TRIGGER ai_guest_no_local_credentials BEFORE INSERT ON local_credentials
 WHEN EXISTS(SELECT 1 FROM ai_guest_owners WHERE user_id=NEW.user_id)
 BEGIN SELECT RAISE(ABORT,'Guest actors cannot acquire credentials'); END;
CREATE TRIGGER ai_guest_no_external_identity BEFORE INSERT ON external_identities
 WHEN EXISTS(SELECT 1 FROM ai_guest_owners WHERE user_id=NEW.user_id)
 BEGIN SELECT RAISE(ABORT,'Guest actors cannot acquire external identities'); END;
CREATE TRIGGER ai_guest_no_native_session BEFORE INSERT ON sessions
 WHEN EXISTS(SELECT 1 FROM ai_guest_owners WHERE user_id=NEW.user_id)
 BEGIN SELECT RAISE(ABORT,'Guest actors cannot acquire native sessions'); END;
CREATE TRIGGER ai_guest_no_admin BEFORE UPDATE OF role ON users
 WHEN NEW.role!='participant' AND EXISTS(SELECT 1 FROM ai_guest_owners WHERE user_id=NEW.id)
 BEGIN SELECT RAISE(ABORT,'Guest actors cannot become administrators'); END;
CREATE TRIGGER ai_guest_no_votes BEFORE INSERT ON votes
 WHEN EXISTS(SELECT 1 FROM ai_guest_owners WHERE user_id=NEW.user_id)
 BEGIN SELECT RAISE(ABORT,'Guest actors cannot vote'); END;
