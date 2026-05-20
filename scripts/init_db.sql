-- AI Data Agent - PostgreSQL Schema Initialization
-- This script runs automatically on first docker-compose up

-- Sessions table (conversation tracking)
CREATE TABLE IF NOT EXISTS sessions (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(64) UNIQUE NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    query TEXT NOT NULL,
    response TEXT,
    sql_generated TEXT,
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tasks table (query execution tracking)
CREATE TABLE IF NOT EXISTS tasks (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(64) REFERENCES sessions(session_id),
    task_type VARCHAR(32) NOT NULL,
    input_data JSONB,
    output_data JSONB,
    status VARCHAR(20) DEFAULT 'pending',
    error_message TEXT,
    execution_time_ms INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Audit logs (security & compliance)
CREATE TABLE IF NOT EXISTS audit_logs (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    action VARCHAR(32) NOT NULL,
    resource TEXT,
    sql_query TEXT,
    result VARCHAR(20),
    ip_address VARCHAR(45),
    metadata JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Patterns (learned query patterns for self-improvement)
CREATE TABLE IF NOT EXISTS patterns (
    id SERIAL PRIMARY KEY,
    pattern_type VARCHAR(32) NOT NULL,
    query_pattern TEXT NOT NULL,
    sql_template TEXT,
    success_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    last_used_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Lessons (experience from errors for self-healing)
CREATE TABLE IF NOT EXISTS lessons (
    id SERIAL PRIMARY KEY,
    error_type VARCHAR(64) NOT NULL,
    error_message TEXT NOT NULL,
    fix_applied TEXT,
    original_sql TEXT,
    fixed_sql TEXT,
    learned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON sessions(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_patterns_type ON patterns(pattern_type);
