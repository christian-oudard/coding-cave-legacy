CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    session_id TEXT UNIQUE,
    host_pid INTEGER,
    cave_pid INTEGER,
    started_at REAL,
    exited_at REAL,
    extra_projects TEXT,
    host_ports TEXT
);

CREATE TABLE projects (
    slug TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE
);

CREATE TABLE approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    shared_branch TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    before_sha TEXT NOT NULL,
    after_sha TEXT NOT NULL,
    approved_at REAL NOT NULL
);
