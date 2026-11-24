PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 1. Accounts (6 Ultra + expandable Pro/Plus/Free)
CREATE TABLE IF NOT EXISTS accounts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    alias                 TEXT NOT NULL UNIQUE,
    email                 TEXT NOT NULL UNIQUE,
    tier                  TEXT NOT NULL DEFAULT 'ultra'
                          CHECK (tier IN ('ultra', 'pro', 'plus', 'free')),
    nlm_profile           TEXT NOT NULL UNIQUE,
    chrome_profile_dir    TEXT NOT NULL,
    cdp_port              INTEGER,
    is_hub_master         INTEGER NOT NULL DEFAULT 0,
    drive_shared_ready    INTEGER NOT NULL DEFAULT 0,
    nlm_auth_status       TEXT NOT NULL DEFAULT 'unauthenticated'
                          CHECK (nlm_auth_status IN ('ok', 'expiring', 'expired', 'unauthenticated', 'error')),
    official_oauth_status TEXT NOT NULL DEFAULT 'not_configured'
                          CHECK (official_oauth_status IN ('ok', 'expired', 'not_configured', 'error')),
    notebook_limit        INTEGER DEFAULT 500,
    source_limit          INTEGER DEFAULT 600,
    expire_date           TEXT,
    enabled               INTEGER NOT NULL DEFAULT 1,
    note                  TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 2. Quota Snapshots (5h & weekly compute limits + per-action costs)
CREATE TABLE IF NOT EXISTS quota_snapshots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id              INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    status                  TEXT NOT NULL,
    five_hour_used_pct      REAL,
    five_hour_remaining_pct REAL,
    five_hour_resets_at     TEXT,
    weekly_used_pct         REAL,
    weekly_remaining_pct    REAL,
    weekly_resets_at        TEXT,
    is_exhausted            INTEGER NOT NULL DEFAULT 0,
    active_window           TEXT,
    actions_json            TEXT NOT NULL DEFAULT '[]',
    error_msg               TEXT,
    captured_at             TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_quota_account_time
    ON quota_snapshots(account_id, captured_at DESC, id DESC);

-- 3. Unified Topics (decoupled from individual accounts via Drive Hub)
CREATE TABLE IF NOT EXISTS topics (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL UNIQUE,
    drive_folder_path TEXT NOT NULL,
    drive_folder_id   TEXT,
    master_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    coupling_mode     TEXT NOT NULL DEFAULT 'shared_with_drive_fallback'
                      CHECK (coupling_mode IN ('shared', 'drive_mirror', 'shared_with_drive_fallback')),
    note              TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 4. Notebooks inventory across accounts
CREATE TABLE IF NOT EXISTS notebooks (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id              INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    account_id            INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    remote_notebook_id    TEXT NOT NULL,
    title                 TEXT NOT NULL,
    is_master             INTEGER NOT NULL DEFAULT 0,
    role                  TEXT NOT NULL DEFAULT 'owner',
    source_count          INTEGER NOT NULL DEFAULT 0,
    artifact_count        INTEGER NOT NULL DEFAULT 0,
    shared_emails_json    TEXT NOT NULL DEFAULT '[]',
    studio_stats_json     TEXT NOT NULL DEFAULT '{}',
    remote_artifacts_json TEXT NOT NULL DEFAULT '[]',
    remote_sources_json   TEXT NOT NULL DEFAULT '[]',
    studio_synced_at      TEXT,
    synced_at             TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(account_id, remote_notebook_id)
);

-- 5. Scheduled & Batch Tasks
CREATE TABLE IF NOT EXISTS tasks (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id             TEXT,
    topic_id             INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    notebook_id          INTEGER REFERENCES notebooks(id) ON DELETE SET NULL,
    task_type            TEXT NOT NULL,
    payload_json         TEXT NOT NULL DEFAULT '{}',
    preferred_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    assigned_account_id  INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    tier_requirement     TEXT NOT NULL DEFAULT 'any'
                         CHECK (tier_requirement IN ('any', 'ultra', 'pro_plus', 'free_ok')),
    priority             INTEGER NOT NULL DEFAULT 50,
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'waiting_quota', 'running', 'deferred_later', 'done', 'failed', 'canceled')),
    scheduled_after      TEXT,
    est_cost_pct         REAL,
    actual_delta_5h_pct  REAL,
    remote_task_id       TEXT,
    channel_used         TEXT,
    result_artifact_path TEXT,
    result_text          TEXT,
    error_msg            TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    started_at           TEXT,
    finished_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_queue
    ON tasks(status, priority, created_at, id);

-- 6. Collected Artifacts archived back to Google Drive Hub
CREATE TABLE IF NOT EXISTS artifacts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id           INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    task_id            INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    account_id         INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    notebook_id        INTEGER REFERENCES notebooks(id) ON DELETE SET NULL,
    remote_artifact_id TEXT,
    kind               TEXT NOT NULL,
    title              TEXT NOT NULL,
    local_drive_path   TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 7. Key-value Application Settings
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 8. Bidirectional Cloud Bridge Jobs (Baidu Netdisk <-> Google Drive)
CREATE TABLE IF NOT EXISTS cloud_bridge_jobs (
    job_id                TEXT PRIMARY KEY,
    direction             TEXT NOT NULL CHECK (direction IN ('baidu_to_gdrive', 'gdrive_to_baidu')),
    status                TEXT NOT NULL,
    source_paths_json     TEXT NOT NULL,
    target_dir            TEXT NOT NULL,
    preserve_tree         INTEGER NOT NULL DEFAULT 1,
    skip_existing         INTEGER NOT NULL DEFAULT 1,
    delete_after_sync     INTEGER NOT NULL DEFAULT 0,
    auto_ingest_topic_id  INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    total_files           INTEGER NOT NULL DEFAULT 0,
    completed_files       INTEGER NOT NULL DEFAULT 0,
    skipped_files         INTEGER NOT NULL DEFAULT 0,
    failed_files          INTEGER NOT NULL DEFAULT 0,
    total_bytes           INTEGER NOT NULL DEFAULT 0,
    transferred_bytes     INTEGER NOT NULL DEFAULT 0,
    error_message         TEXT NOT NULL DEFAULT '',
    created_at            REAL NOT NULL,
    updated_at            REAL NOT NULL,
    finished_at           REAL
);

-- 9. Individual File Transfer Items for Cloud Bridge Jobs
CREATE TABLE IF NOT EXISTS cloud_bridge_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            TEXT NOT NULL REFERENCES cloud_bridge_jobs(job_id) ON DELETE CASCADE,
    source_path       TEXT NOT NULL,
    target_path       TEXT NOT NULL,
    fs_id             INTEGER NOT NULL DEFAULT 0,
    file_size         INTEGER NOT NULL DEFAULT 0,
    remote_md5        TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'pending',
    transferred_bytes INTEGER NOT NULL DEFAULT 0,
    error_message     TEXT NOT NULL DEFAULT '',
    updated_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bridge_items_job_id
    ON cloud_bridge_items(job_id);
