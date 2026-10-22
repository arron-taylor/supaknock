-- Explicit administrator adoption of a reviewed draft; originals remain independent.
CREATE TABLE ai_section_publications (
 section_id TEXT NOT NULL REFERENCES ai_sections(id), section_version INTEGER NOT NULL,
 source_id TEXT NOT NULL UNIQUE REFERENCES ai_sources(id), approved_by TEXT NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL, PRIMARY KEY(section_id,section_version),
 FOREIGN KEY(section_id,section_version) REFERENCES ai_section_versions(section_id,version)
);
CREATE TRIGGER ai_section_publications_immutable BEFORE UPDATE ON ai_section_publications
 BEGIN SELECT RAISE(ABORT,'Knowledge approval provenance is immutable'); END;
