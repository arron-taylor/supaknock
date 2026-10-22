CREATE TABLE ai_embedding_settings (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL DEFAULT 0,
 mode TEXT NOT NULL DEFAULT 'local' CHECK(mode IN ('local','external')),
 config TEXT, profile TEXT, protected_key TEXT, updated_at TEXT
);
INSERT INTO ai_embedding_settings(singleton) VALUES(1);
DROP TRIGGER ai_chunks_insert;
DROP TRIGGER ai_chunks_delete;
DROP TRIGGER ai_chunks_immutable;
CREATE TABLE ai_chunks_new (
 id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, project_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL CHECK(ordinal>=0), title TEXT NOT NULL, content TEXT NOT NULL,
 location TEXT, vector BLOB NOT NULL CHECK(length(vector) BETWEEN 4 AND 16384 AND length(vector)%4=0),
 page_id TEXT REFERENCES ai_crawl_pages(id),
 FOREIGN KEY(revision_id,project_id) REFERENCES ai_revisions(id,project_id), UNIQUE(revision_id,ordinal)
);
INSERT INTO ai_chunks_new(rowid,id,revision_id,project_id,ordinal,title,content,location,vector,page_id)
 SELECT rowid,id,revision_id,project_id,ordinal,title,content,location,vector,page_id FROM ai_chunks;
DROP TABLE ai_chunks;
ALTER TABLE ai_chunks_new RENAME TO ai_chunks;
CREATE INDEX ai_chunks_revision ON ai_chunks(revision_id,ordinal);
CREATE INDEX ai_chunks_project ON ai_chunks(project_id,revision_id);
CREATE TRIGGER ai_chunks_insert AFTER INSERT ON ai_chunks BEGIN
 INSERT INTO ai_chunks_fts(rowid,title,content) VALUES(NEW.rowid,NEW.title,NEW.content);
END;
CREATE TRIGGER ai_chunks_delete AFTER DELETE ON ai_chunks BEGIN
 INSERT INTO ai_chunks_fts(ai_chunks_fts,rowid,title,content) VALUES('delete',OLD.rowid,OLD.title,OLD.content);
END;
CREATE TRIGGER ai_chunks_immutable BEFORE UPDATE ON ai_chunks BEGIN SELECT RAISE(ABORT,'Knowledge chunks are immutable'); END;
