CREATE TABLE ai_embedding_settings (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL DEFAULT 0,
 mode TEXT NOT NULL DEFAULT 'local' CHECK(mode IN ('local','external')),
 config TEXT, profile TEXT, protected_key TEXT, updated_at TEXT
);
INSERT INTO ai_embedding_settings(singleton) VALUES(1);
ALTER TABLE ai_chunks DROP CONSTRAINT ai_chunks_vector_check;
ALTER TABLE ai_chunks ADD CONSTRAINT ai_chunks_vector_check CHECK(length(vector) BETWEEN 4 AND 16384 AND length(vector)%4=0);
