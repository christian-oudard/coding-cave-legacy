CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    project TEXT NOT NULL,
    session_id TEXT UNIQUE,
    host_pid INTEGER,
    cave_pid INTEGER,
    started_at REAL,
    exited_at REAL,
    extra_projects TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    slug TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    shared_branch TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    before_sha TEXT NOT NULL,
    after_sha TEXT NOT NULL,
    approved_at REAL NOT NULL
);

-- Permanent record of every session. Rows are never deleted, and agent_id is a
-- plain column rather than a foreign key so a record outlives the agent that
-- created it. The transcript path is not stored: it's always
-- `config::session_transcript(session_id)`, and storing the original live
-- path would just go stale once the agent that wrote it is collected.
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    extra_projects TEXT,
    agent_id TEXT,
    branch TEXT,
    started_at REAL,
    ended_at REAL
);
