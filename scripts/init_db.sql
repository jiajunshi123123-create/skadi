-- Skadi 经验库建表脚本（全新安装）
--
-- 本文件以当前 Python 代码为准，覆盖：
--   session_store_pg.py / core.task_log / core.audit / tools.pg_tool.py
--   learning.pattern_store / permission_manager.py / utils.context_compactor.py
--   user_identities（渠道外部 ID → 内部 user_id → user_permissions.staff_id）
--
-- docker-compose 只在 Postgres 数据卷为空时执行本脚本。
-- 已有数据卷请改跑 scripts/migrate.sql，说明见 scripts/MIGRATION.md。
-- 不要在这里写入真实员工、密钥或业务数据。

-- ---------------------------------------------------------------------------
-- 会话：session_store_pg.py 按 user_id 做 UPSERT
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    user_id     VARCHAR(128) PRIMARY KEY,
    user_name   VARCHAR(256),
    plan        JSONB,
    question    TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- 已完成的查询任务：core.task_log.save_task
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id              SERIAL PRIMARY KEY,
    task_id         VARCHAR(64) UNIQUE,
    user_id         VARCHAR(128) NOT NULL,
    user_name       VARCHAR(256),
    user_question   TEXT,
    plan_json       TEXT,
    execution       TEXT,
    result_summary  TEXT,
    tags            TEXT[],
    status          VARCHAR(32) DEFAULT 'completed',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);

-- ---------------------------------------------------------------------------
-- 审计：core.audit.write_audit_log 与 tools.pg_tool.log_audit 共用一张表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
    id            SERIAL PRIMARY KEY,
    user_id       VARCHAR(128) NOT NULL,
    user_name     VARCHAR(256),
    query         TEXT,
    response      TEXT,
    source        VARCHAR(64),
    duration_ms   INTEGER,
    tokens        INTEGER,
    status        VARCHAR(32),
    sql_executed  TEXT,
    agent_path    VARCHAR(128),
    action        VARCHAR(32) DEFAULT 'query',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at);

-- ---------------------------------------------------------------------------
-- 查询模式：learning.pattern_store（按 query_pattern 去重并累加 success_count）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS patterns (
    id                  SERIAL PRIMARY KEY,
    query_pattern       TEXT NOT NULL,
    sql_template        TEXT,
    analysis_template   TEXT,
    success_count       INTEGER DEFAULT 1,
    last_used           TIMESTAMP,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_patterns_query_pattern ON patterns(query_pattern);

-- ---------------------------------------------------------------------------
-- 经验教训：learning.pattern_store.save_lesson / get_recent_lessons
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lessons (
    id              SERIAL PRIMARY KEY,
    lesson_type     VARCHAR(64) NOT NULL,
    original_query  TEXT,
    problem         TEXT,
    solution        TEXT,
    table_name      VARCHAR(128),
    error_type      VARCHAR(64),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_lessons_type_created ON lessons(lesson_type, created_at DESC);

-- ---------------------------------------------------------------------------
-- 角色与人员权限：permission_manager.py
-- allowed_tables / mandatory_filters 用 JSONB，psycopg2 会还原成 list / dict
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS role_definitions (
    role_id             VARCHAR(64) PRIMARY KEY,
    role_name           VARCHAR(128) NOT NULL,
    allowed_tables      JSONB NOT NULL DEFAULT '[]'::jsonb,
    mandatory_filters   JSONB NOT NULL DEFAULT '{}'::jsonb,
    max_query_days      INTEGER NOT NULL DEFAULT 30,
    allow_user_detail   BOOLEAN NOT NULL DEFAULT FALSE,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_permissions (
    staff_id     VARCHAR(128) PRIMARY KEY,
    staff_name   VARCHAR(256),
    role_id      VARCHAR(64) NOT NULL REFERENCES role_definitions(role_id),
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at   TIMESTAMP,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- tables_accessed 由代码以 Python list 写入，对应 TEXT[]
CREATE TABLE IF NOT EXISTS permission_audit_log (
    id                  SERIAL PRIMARY KEY,
    staff_id            VARCHAR(128),
    staff_name          VARCHAR(256),
    role_id             VARCHAR(64),
    action              VARCHAR(32),
    original_sql        TEXT,
    enforced_sql        TEXT,
    denied_reason       TEXT,
    tables_accessed     TEXT[],
    query_result_rows   INTEGER DEFAULT 0,
    duration_ms         INTEGER DEFAULT 0,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_permission_audit_staff ON permission_audit_log(staff_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 渠道身份：external_id（钉钉 staffId、飞书 open_id 等）→ user_permissions.staff_id
-- 未出现在本表、或对应权限未生效的用户，机器人与后续渠道一律拒绝。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_identities (
    id            SERIAL PRIMARY KEY,
    channel       VARCHAR(32) NOT NULL,
    external_id   VARCHAR(256) NOT NULL,
    user_id       VARCHAR(128) NOT NULL,
    display_name  VARCHAR(256),
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (channel, external_id)
);

CREATE INDEX IF NOT EXISTS idx_user_identities_user ON user_identities(user_id);

COMMENT ON COLUMN user_identities.user_id IS '内部用户 ID，等于 user_permissions.staff_id';
COMMENT ON COLUMN user_identities.channel IS 'dingtalk / feishu / web 等渠道名';
COMMENT ON COLUMN user_identities.external_id IS '渠道侧用户 ID，例如钉钉 senderStaffId、飞书 open_id';

-- ---------------------------------------------------------------------------
-- 上下文压缩锚点：utils.context_compactor
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS context_anchors (
    id               SERIAL PRIMARY KEY,
    session_id       VARCHAR(128) NOT NULL,
    anchor_text      TEXT NOT NULL,
    anchor_tokens    INTEGER,
    original_tokens  INTEGER,
    compressed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_context_anchors_session
    ON context_anchors(session_id, compressed_at DESC);

-- ---------------------------------------------------------------------------
-- 查询复用。命中后只复用 SQL 模板，执行时重新取数。
-- status: candidate 未验证 / verified 可复用 / disabled 停用
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS query_cache (
    id                SERIAL PRIMARY KEY,
    normalized_q      TEXT NOT NULL,
    params_template   JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding         JSONB,
    sql_template      TEXT NOT NULL,
    role_scope        VARCHAR(64) NOT NULL,
    tables            TEXT[] NOT NULL DEFAULT '{}',
    success_cnt       INTEGER NOT NULL DEFAULT 0,
    up_cnt            INTEGER NOT NULL DEFAULT 0,
    down_cnt          INTEGER NOT NULL DEFAULT 0,
    status            VARCHAR(16) NOT NULL DEFAULT 'candidate',
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT query_cache_status_check
        CHECK (status IN ('candidate', 'verified', 'disabled'))
);

CREATE INDEX IF NOT EXISTS idx_query_cache_role_status
    ON query_cache(role_scope, status);

-- 同一角色、同一归一化问题只保留一行。并发首次成功用 INSERT ... ON CONFLICT 并入这一行。
CREATE UNIQUE INDEX IF NOT EXISTS uq_query_cache_role_normalized_q
    ON query_cache(role_scope, normalized_q);
