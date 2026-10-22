CREATE TABLE connected_apps (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    id TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL DEFAULT 1,
    base_url TEXT,
    protected_token TEXT,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    app_instance_id TEXT UNIQUE,
    link_origins TEXT NOT NULL DEFAULT '[]',
    support_linked INTEGER NOT NULL DEFAULT 0 CHECK(support_linked IN (0,1)),
    updated_at TEXT NOT NULL
);
CREATE TABLE connected_app_audit (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    version INTEGER NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE connected_app_preferences (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL,
    timezone TEXT NOT NULL
);
INSERT INTO connected_app_preferences VALUES(1,1,'UTC');
