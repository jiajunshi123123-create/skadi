-- 把已有经验库补齐到 scripts/init_db.sql 的结构。可重复执行。
-- 适用：docker-compose 的 Postgres 数据卷在旧版 init 之后已经写过数据，
--       入口脚本不会再次运行。
--
-- 用法（容器名以 docker-compose.yml 为准）：
--   docker exec -i agent-postgres psql -U agent_user -d agent_experience -v ON_ERROR_STOP=1 < scripts/migrate.sql
--
-- 策略：
--   * 代码已经读不了的旧表改名为 *_legacy，原数据保留，再按新结构建表。
--   * 审计表只加列、放宽 NOT NULL，不丢历史。
--   * 已有 user_permissions.staff_id 会回填为钉钉身份，避免升级后全员被拒绝。
-- 全新安装直接用 init_db.sql，不必先跑本文件。详见 scripts/MIGRATION.md。

BEGIN;

-- sessions：旧表以 session_id 为主键，代码按 user_id UPSERT plan/question。
DO $$
BEGIN
    IF to_regclass('public.sessions') IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'sessions' AND column_name = 'session_id'
       )
       AND NOT EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'sessions' AND column_name = 'plan'
       )
    THEN
        ALTER TABLE sessions RENAME TO sessions_legacy;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS sessions (
    user_id     VARCHAR(128) PRIMARY KEY,
    user_name   VARCHAR(256),
    plan        JSONB,
    question    TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- tasks：旧表是 task_type/input_data，代码写入 task_id/user_question/plan_json。
DO $$
BEGIN
    IF to_regclass('public.tasks') IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'tasks' AND column_name = 'task_type'
       )
       AND NOT EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'tasks' AND column_name = 'task_id'
       )
    THEN
        ALTER TABLE tasks RENAME TO tasks_legacy;
    END IF;
END $$;

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

-- audit_logs：保留旧行，补上代码实际插入的列。
CREATE TABLE IF NOT EXISTS audit_logs (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(128) NOT NULL
);

ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS user_name VARCHAR(256);
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS query TEXT;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS response TEXT;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS source VARCHAR(64);
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS duration_ms INTEGER;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS tokens INTEGER;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS status VARCHAR(32);
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS sql_executed TEXT;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS agent_path VARCHAR(128);
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS action VARCHAR(32) DEFAULT 'query';
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE audit_logs ALTER COLUMN action DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at);

-- patterns：补 analysis_template / last_used，放开旧的 pattern_type NOT NULL。
CREATE TABLE IF NOT EXISTS patterns (
    id SERIAL PRIMARY KEY,
    query_pattern TEXT NOT NULL
);

ALTER TABLE patterns ADD COLUMN IF NOT EXISTS sql_template TEXT;
ALTER TABLE patterns ADD COLUMN IF NOT EXISTS analysis_template TEXT;
ALTER TABLE patterns ADD COLUMN IF NOT EXISTS success_count INTEGER DEFAULT 1;
ALTER TABLE patterns ADD COLUMN IF NOT EXISTS last_used TIMESTAMP;
ALTER TABLE patterns ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE patterns ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMP;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'patterns' AND column_name = 'pattern_type'
    ) THEN
        ALTER TABLE patterns ALTER COLUMN pattern_type DROP NOT NULL;
    END IF;
END $$;

UPDATE patterns
SET last_used = last_used_at
WHERE last_used IS NULL AND last_used_at IS NOT NULL;

-- 重复的 query_pattern 会让唯一索引失败。这里记下原因并继续，避免整次迁移回滚。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_patterns_query_pattern'
    ) THEN
        BEGIN
            CREATE UNIQUE INDEX idx_patterns_query_pattern ON patterns(query_pattern);
        EXCEPTION
            WHEN OTHERS THEN
                RAISE NOTICE
                    '未能创建 patterns(query_pattern) 唯一索引。请先合并重复的 query_pattern 再重跑: %',
                    SQLERRM;
        END;
    END IF;
END $$;

-- lessons：补代码使用的列，放开旧的 error_type/error_message NOT NULL。
CREATE TABLE IF NOT EXISTS lessons (
    id SERIAL PRIMARY KEY
);

ALTER TABLE lessons ADD COLUMN IF NOT EXISTS lesson_type VARCHAR(64);
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS original_query TEXT;
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS problem TEXT;
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS solution TEXT;
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'lessons'
          AND column_name = 'error_type' AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE lessons ALTER COLUMN error_type DROP NOT NULL;
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'lessons'
          AND column_name = 'error_message' AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE lessons ALTER COLUMN error_message DROP NOT NULL;
    END IF;
END $$;

ALTER TABLE lessons ADD COLUMN IF NOT EXISTS table_name VARCHAR(128);
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS error_type VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_lessons_type_created ON lessons(lesson_type, created_at DESC);

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

-- 并发首次成功可能已经插入了多行。唯一索引要求每个 (role_scope, normalized_q) 只留一行：
-- verified 优先，其次 success_cnt 高，其次 id 小。可以重复执行。
DELETE FROM query_cache
WHERE id IN (
    SELECT id FROM (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY role_scope, normalized_q
                   ORDER BY
                       CASE status
                           WHEN 'verified' THEN 0
                           WHEN 'candidate' THEN 1
                           ELSE 2
                       END,
                       success_cnt DESC,
                       id ASC
               ) AS rn
        FROM query_cache
    ) ranked
    WHERE rn > 1
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_query_cache_role_normalized_q
    ON query_cache(role_scope, normalized_q);

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

-- 已授权的钉钉用户：外部 ID 与 staff_id 相同，升级后仍能通过身份表。
INSERT INTO user_identities (channel, external_id, user_id, display_name)
SELECT 'dingtalk', staff_id, staff_id, staff_name
FROM user_permissions
ON CONFLICT (channel, external_id) DO NOTHING;

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

CREATE TABLE IF NOT EXISTS report_schedules (
    id              SERIAL PRIMARY KEY,
    owner_user_id   VARCHAR(128) NOT NULL,
    channel         VARCHAR(32) NOT NULL,
    target          VARCHAR(256) NOT NULL,
    cron            VARCHAR(64) NOT NULL,
    report_spec     TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at     TIMESTAMP,
    last_error      TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_report_schedules_owner
    ON report_schedules(owner_user_id, enabled);

COMMIT;
