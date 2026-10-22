CREATE TABLE ai_crawl_runs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), source_id TEXT NOT NULL,
 source_version INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 config TEXT NOT NULL, client_id TEXT NOT NULL, request_hash TEXT NOT NULL,
 job_id TEXT NOT NULL UNIQUE REFERENCES ai_jobs(id), document_id TEXT REFERENCES ai_documents(id),
 created_at TEXT NOT NULL, finished_at TEXT,
 FOREIGN KEY(source_id,project_id) REFERENCES ai_sources(id,project_id), UNIQUE(project_id,client_id)
);
CREATE TABLE ai_crawl_pages (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ai_crawl_runs(id), attempt INTEGER NOT NULL,
 url TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('fetched','failed','skipped')), error TEXT, created_at TEXT NOT NULL,
 UNIQUE(run_id,attempt,url)
);
CREATE TRIGGER ai_crawl_page_immutable BEFORE UPDATE ON ai_crawl_pages
 BEGIN SELECT RAISE(ABORT,'Crawled page originals are immutable'); END;
ALTER TABLE ai_documents ADD COLUMN crawl_run_id TEXT REFERENCES ai_crawl_runs(id);
ALTER TABLE ai_chunks ADD COLUMN page_id TEXT REFERENCES ai_crawl_pages(id);
CREATE INDEX ai_crawl_pages_run ON ai_crawl_pages(run_id,attempt,status);
CREATE INDEX ai_chunks_project ON ai_chunks(project_id,revision_id);
