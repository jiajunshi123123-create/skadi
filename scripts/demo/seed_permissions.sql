-- 只在演示用的 Postgres 里执行。不要并进生产 init。
INSERT INTO role_definitions (
    role_id, role_name, allowed_tables, mandatory_filters, max_query_days, allow_user_detail, is_active
) VALUES (
    'demo_analyst',
    '演示分析',
    '["demo_daily_metrics"]'::jsonb,
    '{}'::jsonb,
    400,
    FALSE,
    TRUE
) ON CONFLICT (role_id) DO NOTHING;

INSERT INTO user_permissions (staff_id, staff_name, role_id, is_active)
VALUES ('demo-user', '演示用户', 'demo_analyst', TRUE)
ON CONFLICT (staff_id) DO NOTHING;

INSERT INTO user_identities (channel, external_id, user_id, display_name)
VALUES ('web', 'demo-user', 'demo-user', '演示用户')
ON CONFLICT (channel, external_id) DO NOTHING;
