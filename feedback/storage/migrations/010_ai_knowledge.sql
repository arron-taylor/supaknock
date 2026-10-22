-- Model cache is replaceable; this ID selects one verified installation directory.
ALTER TABLE ai_runtime ADD COLUMN active_cache_id TEXT;
-- Preserve uploaded original text independently of an unfinished embedding index.
CREATE TABLE ai_documents (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, project_id TEXT NOT NULL,
 source_version INTEGER NOT NULL, revision_number INTEGER NOT NULL CHECK(revision_number>0),
 filename TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
 content_hash TEXT NOT NULL, byte_count INTEGER NOT NULL CHECK(byte_count BETWEEN 1 AND 1048576),
 created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
 client_id TEXT NOT NULL, request_hash TEXT NOT NULL, job_id TEXT NOT NULL UNIQUE REFERENCES ai_jobs(id),
 FOREIGN KEY(source_id,project_id) REFERENCES ai_sources(id,project_id),
 UNIQUE(source_id,revision_number), UNIQUE(project_id,client_id)
);
CREATE TRIGGER ai_document_immutable BEFORE UPDATE ON ai_documents
 BEGIN SELECT RAISE(ABORT,'Original knowledge documents are immutable'); END;
ALTER TABLE ai_sources ADD COLUMN latest_document_id TEXT REFERENCES ai_documents(id);
ALTER TABLE ai_sources ADD COLUMN pending_job_id TEXT REFERENCES ai_jobs(id);
ALTER TABLE ai_revisions ADD COLUMN document_id TEXT REFERENCES ai_documents(id);
CREATE TRIGGER ai_revision_document_immutable BEFORE UPDATE ON ai_revisions
 WHEN NEW.document_id IS NOT OLD.document_id
 BEGIN SELECT RAISE(ABORT,'Revision document provenance is immutable'); END;
