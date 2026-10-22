ALTER TABLE sessions ADD COLUMN transport TEXT NOT NULL DEFAULT 'cookie' CHECK(transport IN ('cookie','native'));
CREATE TABLE instance_identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL UNIQUE);
