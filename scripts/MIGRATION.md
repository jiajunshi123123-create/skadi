# 经验库迁移说明

代码读写的是 PostgreSQL 经验库（会话、任务、审计、模式、权限、身份）。`scripts/init_db.sql` 描述的是**当前代码**要的表，不是旧脚本里那一版。

`docker-compose` 把 `init_db.sql` 挂到 `docker-entrypoint-initdb.d`。Postgres 官方镜像**只在数据卷是空的时候**跑它。已经起过的 `pg_data` 卷不会自动改表。

## 全新安装

```bash
psql -f scripts/init_db.sql
# 或
docker-compose up -d
```

然后按下面的「授权一个用户」写入角色、权限和渠道身份。没有这两行的人会被拒绝，不会走任何默认角色。

## 已有数据卷

在仓库根目录执行：

```bash
docker exec -i agent-postgres \
  psql -U agent_user -d agent_experience -v ON_ERROR_STOP=1 \
  < scripts/migrate.sql
```

`migrate.sql` 可以重复执行。它会：

| 旧对象 | 处理 |
| --- | --- |
| `sessions` 仍是 `session_id` 主键、没有 `plan` | 改名为 `sessions_legacy`，再建代码使用的表 |
| `tasks` 仍是 `task_type` / `input_data`、没有 `task_id` | 改名为 `tasks_legacy`，再建新表 |
| `audit_logs` 缺少 `query` / `user_name` 等列，或 `action` 为 NOT NULL | 只加列并放开 `action`，保留旧审计行 |
| `patterns` / `lessons` 使用旧列名 | 补 `query_pattern` 代码依赖的列，并去掉旧列的 NOT NULL |
| `user_permissions` 已有员工 | 按 `channel='dingtalk'`、`external_id=staff_id` 回填 `user_identities` |

`*_legacy` 表不会被应用读取，确认不需要后可以自行 `DROP`。

如果 `patterns.query_pattern` 上已经有重复值，唯一索引会失败。先合并重复行再重跑：

```sql
SELECT query_pattern, COUNT(*) FROM patterns GROUP BY 1 HAVING COUNT(*) > 1;
```

## 授权一个用户

`user_identities.user_id` 必须等于 `user_permissions.staff_id`。渠道侧 ID 各不相同：钉钉用消息里的 `senderStaffId`，飞书用 `open_id`。

```sql
INSERT INTO role_definitions
    (role_id, role_name, allowed_tables, mandatory_filters, max_query_days, allow_user_detail)
VALUES (
    'analyst',
    '分析师',
    '["your_table"]'::jsonb,
    '{}'::jsonb,
    90,
    FALSE
);

INSERT INTO user_permissions (staff_id, staff_name, role_id)
VALUES ('u-001', '姓名', 'analyst');

INSERT INTO user_identities (channel, external_id, user_id, display_name)
VALUES ('dingtalk', '钉钉senderStaffId', 'u-001', '姓名');
```

未配置身份，或身份对应的权限已停用、已过期，机器人会回复拒绝说明并带上对方的渠道 ID，不会执行查询。

改完权限后，进程内缓存大约 5 分钟过期；需要立即生效时重启对应服务。
