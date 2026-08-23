# S1-06：迁移发布与账号清理 Runbook

## 0. 当前只读盘点

执行日期：2026-08-18。

```text
默认数据库 current：c1e9f2a7b4d6
迁移头 heads：      6aa6adbb7084 (head)
测试数据库 current：6aa6adbb7084 (head)
```

当前开发业务库落后于当前 head 一个版本。未经备份、schema 对照和发布确认，不能对当前开发业务库执行 `alembic upgrade head`。

本 Runbook 不执行迁移、不执行 stamp/downgrade、不删除账号，不涉及退款和支付。

### 0.1 本次发布目标与固定变量

同一次发布中的备份、schema 对照、迁移和验收必须使用同一组目标变量。执行前由发布负责人填写并复核；尖括号是必须替换的占位符，不得原样执行。

```bash
export TARGET_HOST="localhost"
export TARGET_PORT="5433"
export TARGET_USER="<database_user>"
export TARGET_DB="<target_database>"
export SOURCE_REVISION="<recorded_pre_migration_revision>"
export TARGET_REVISION="6aa6adbb7084"
export BACKUP_DIR="<backup_root>/ecommerce-agent/<YYYYMMDD-HHMMSS>"
export RESTORE_DB="<isolated_restore_database>"
export EMPTY_DB="<isolated_empty_database>"
```

约束：

- `TARGET_DB` 是本次发布唯一目标库；针对目标库的所有 `pg_dump`、`psql` 和 Alembic 命令都必须引用它。空库和恢复库演练分别使用 `EMPTY_DB`、`RESTORE_DB`，但必须复用同一组 `TARGET_HOST`、`TARGET_PORT` 和 `TARGET_USER`。
- Alembic 连接参数必须显式通过 `PG_HOST`、`PG_PORT`、`PG_USER`、`PG_DBNAME` 传入，不能依赖 `.env` 中可能指向其他数据库的默认值。
- `RESTORE_DB` 必须是与 `TARGET_DB` 不同的独立恢复库，只用于备份恢复演练。
- `BACKUP_DIR` 必须位于受控备份存储，不得提交到仓库；密码通过 `.pgpass`、部署密钥或密钥管理系统注入。
- 当前开发业务库从 `c1e9f2a7b4d6` 升级到 `6aa6adbb7084` 的演练仍待执行；附录 F 的旧 head 演练不能替代它。

### 0.2 S1-06 验收状态

文档修订完成不等于发布演练完成。只有完成独立恢复库 `pg_restore`、存量库决策分支演练、目标库升级和完整验收记录后，才能将 S1-06 标记为完成。

## 1. 通用发布规则

1. 所有命令必须明确目标数据库，不能依赖不确定的 `.env`。
2. 生产迁移前必须完成可恢复备份，并记录路径、时间、操作者和 SHA256。
3. 生产只执行已经审查并提交的 Alembic migration。
4. 数据库密码使用部署密钥、`.pgpass` 或密钥管理系统注入，不能写进命令、日志或仓库。
5. 生产应用启动前，目标数据库必须已经升级到目标 revision。
6. 应用启动不自动建表、不自动创建默认用户。
7. `stamp` 不能用来掩盖 schema 不一致；生产故障优先前滚修复。

## 2. 空库流程

### 2.1 执行迁移

先确认 `${EMPTY_DB}` 确实是独立空库：

```bash
PG_HOST="${TARGET_HOST}" \
PG_PORT="${TARGET_PORT}" \
PG_USER="${TARGET_USER}" \
PG_DBNAME="${EMPTY_DB}" \
alembic current
```

确认后执行：

```bash
PG_HOST="${TARGET_HOST}" \
PG_PORT="${TARGET_PORT}" \
PG_USER="${TARGET_USER}" \
PG_DBNAME="${EMPTY_DB}" \
alembic upgrade head
```

Alembic 会按照 `down_revision` 链依次执行 migration，直到 `6aa6adbb7084`。

### 2.2 验收

```bash
PG_HOST="${TARGET_HOST}" \
PG_PORT="${TARGET_PORT}" \
PG_USER="${TARGET_USER}" \
PG_DBNAME="${EMPTY_DB}" \
alembic current
```

预期为：

```text
6aa6adbb7084 (head)
```

检查所有表和迁移版本：

```sql
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
ORDER BY table_name;

SELECT version_num FROM public.alembic_version;
```

预期关键对象包括：

```text
component_products, knowledge_chunks, laptop_products, phone_products,
orders, order_items, tickets, users, sessions, session_messages,
alembic_version
```

检查外键：

```sql
SELECT tc.table_name, tc.constraint_name, kcu.column_name,
       ccu.table_name AS referenced_table, ccu.column_name AS referenced_column
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name
 AND tc.table_schema = kcu.table_schema
JOIN information_schema.constraint_column_usage ccu
  ON ccu.constraint_name = tc.constraint_name
 AND ccu.table_schema = tc.table_schema
WHERE tc.table_schema = 'public'
  AND tc.constraint_type = 'FOREIGN KEY'
ORDER BY tc.table_name, tc.constraint_name;
```

重点确认：

```text
orders.customer_user_id -> users.id
tickets.customer_user_id -> users.id
tickets.assigned_agent_id -> users.id
session_messages.session_id -> sessions.id
order_items.order_id -> orders.order_id
```

检查索引：

```sql
SELECT schemaname, tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
ORDER BY tablename, indexname;
```

重点确认：

```text
idx_orders_customer_user_id
idx_tickets_customer_user_id
idx_tickets_assigned_agent_id
idx_sessions_owner_last_active
```

## 3. 存量库决策树

先对 `TARGET_DB` 做只读检查。根据结果只允许进入以下一个分支：

```text
TARGET_DB
  |
  +-- alembic_version 存在且 revision 在已知迁移链中
  |       -> 记录 current -> 备份与 schema 对照 -> upgrade head
  |
  +-- alembic_version 不存在
  |       -> 判断 schema 是否与已验证 baseline 完全等价
  |              |
  |              +-- 等价 -> 备份与 schema 对照 -> stamp 046df10e2506 -> upgrade head
  |              |
  |              +-- 不等价/无法证明 -> 停止，不 stamp，不 upgrade
  |
  +-- revision 未知、多个 head、schema 不明或不等价
          -> 停止，先完成调查或编写兼容 migration
```

只读检查：

```bash
PG_HOST="${TARGET_HOST}" \
PG_PORT="${TARGET_PORT}" \
PG_USER="${TARGET_USER}" \
PG_DBNAME="${TARGET_DB}" \
alembic current
PG_HOST="${TARGET_HOST}" \
PG_PORT="${TARGET_PORT}" \
PG_USER="${TARGET_USER}" \
PG_DBNAME="${TARGET_DB}" \
alembic heads
psql -h "${TARGET_HOST}" -p "${TARGET_PORT}" -U "${TARGET_USER}" -d "${TARGET_DB}" \
  -c "SELECT to_regclass('public.alembic_version') AS alembic_version_table;"
```

判断规则：

1. 有 `alembic_version` 且 revision 是迁移链中的已知 revision：不得 stamp，按第 4 节备份后直接 `upgrade head`。
2. 没有 `alembic_version` 但 schema 与已验证 baseline 完全等价：只能在完成备份、schema 对照和批准后 stamp `046df10e2506`，然后继续升级。
3. 没有版本表且 schema 不明、缺表、字段类型不一致、约束/索引不一致，或 revision 不在当前迁移链：停止。不得用 `stamp` 或 `upgrade` 猜测数据库状态。

baseline 等价性至少要覆盖：全部业务表、主键、唯一约束、外键、关键字段类型、向量扩展、向量索引，以及 baseline 中定义的默认值。无法提供对照证据时，按“不等价”处理。

## 4. 已有库升级流程

### 4.1 备份与升级前快照

先确定备份位置，例如：

```text
/var/backups/ecommerce-agent/postgres/<YYYYMMDD-HHMMSS>/
```

备份至少包括完整数据、schema-only、版本记录和数据量快照：

```bash
pg_dump \
  -h "${TARGET_HOST}" \
  -p "${TARGET_PORT}" \
  -U "${TARGET_USER}" \
  -d "${TARGET_DB}" \
  --format=custom \
  --file="${BACKUP_DIR}/ecommerce-agent.dump"
pg_dump \
  -h "${TARGET_HOST}" \
  -p "${TARGET_PORT}" \
  -U "${TARGET_USER}" \
  -d "${TARGET_DB}" \
  --schema-only \
  --no-owner \
  --file="${BACKUP_DIR}/schema-before.sql"
sha256sum "${BACKUP_DIR}/ecommerce-agent.dump" "${BACKUP_DIR}/schema-before.sql" \
  > "${BACKUP_DIR}/SHA256SUMS"
```

记录：备份路径、时间、数据库名称、操作者、目标 revision 和以下完整快照。快照文件应和备份一起归档。

```bash
psql -h "${TARGET_HOST}" -p "${TARGET_PORT}" -U "${TARGET_USER}" -d "${TARGET_DB}" \
  -c "SELECT 'orders' AS table_name, COUNT(*) AS row_count FROM public.orders
      UNION ALL SELECT 'order_items', COUNT(*) FROM public.order_items
      UNION ALL SELECT 'tickets', COUNT(*) FROM public.tickets
      UNION ALL SELECT 'users', COUNT(*) FROM public.users
      UNION ALL SELECT 'sessions', COUNT(*) FROM public.sessions
      UNION ALL SELECT 'session_messages', COUNT(*) FROM public.session_messages
      ORDER BY table_name;" \
  > "${BACKUP_DIR}/counts-before.txt"
```

对于本阶段的 schema-only 迁移，`orders`、`order_items`、`tickets`、`users`、`sessions` 和 `session_messages` 的数量都应保持不变；若数量变化，必须停止验收并调查，不能只以订单数量未变作为通过条件。

### 4.2 对照和升级

使用第 4.1 已归档的 `${BACKUP_DIR}/schema-before.sql`，不要重复导出覆盖它：

```bash
PG_HOST="${TARGET_HOST}" \
PG_PORT="${TARGET_PORT}" \
PG_USER="${TARGET_USER}" \
PG_DBNAME="${TARGET_DB}" \
alembic current
PG_HOST="${TARGET_HOST}" \
PG_PORT="${TARGET_PORT}" \
PG_USER="${TARGET_USER}" \
PG_DBNAME="${TARGET_DB}" \
alembic heads
```

将升级前 `current` 记录为 `${SOURCE_REVISION}`，并核对它与 `TARGET_DB` 的版本表一致；升级后的 `current` 必须等于 `${TARGET_REVISION}`。

确认 `TARGET_DB`、备份、快照和 schema 对照无误，并完成第 3 节分支判断后，才执行：

```bash
PG_HOST="${TARGET_HOST}" \
PG_PORT="${TARGET_PORT}" \
PG_USER="${TARGET_USER}" \
PG_DBNAME="${TARGET_DB}" \
alembic upgrade head
```

### 4.3 升级后校验

```bash
PG_HOST="${TARGET_HOST}" \
PG_PORT="${TARGET_PORT}" \
PG_USER="${TARGET_USER}" \
PG_DBNAME="${TARGET_DB}" \
alembic current
pg_dump \
  -h "${TARGET_HOST}" \
  -p "${TARGET_PORT}" \
  -U "${TARGET_USER}" \
  -d "${TARGET_DB}" \
  --schema-only \
  --no-owner \
  --file="${BACKUP_DIR}/schema-after.sql"
sha256sum "${BACKUP_DIR}/schema-after.sql" \
  > "${BACKUP_DIR}/schema-after.SHA256SUM"
psql -h "${TARGET_HOST}" -p "${TARGET_PORT}" -U "${TARGET_USER}" -d "${TARGET_DB}" \
  -c "SELECT 'orders' AS table_name, COUNT(*) AS row_count FROM public.orders
      UNION ALL SELECT 'order_items', COUNT(*) FROM public.order_items
      UNION ALL SELECT 'tickets', COUNT(*) FROM public.tickets
      UNION ALL SELECT 'users', COUNT(*) FROM public.users
      UNION ALL SELECT 'sessions', COUNT(*) FROM public.sessions
      UNION ALL SELECT 'session_messages', COUNT(*) FROM public.session_messages
      ORDER BY table_name;" \
  > "${BACKUP_DIR}/counts-after.txt"
```

对照前后 schema，确认原有表、字段、主键、唯一约束没有意外变化，新字段、外键、索引和 `alembic_version` 正确存在。

记录数据量并将 `${BACKUP_DIR}/counts-after.txt` 与 `${BACKUP_DIR}/counts-before.txt` 做逐表比较。对本阶段 schema-only 迁移，六张表的数量都必须保持不变；新增 `tickets.assigned_agent_id` 的 NULL 值是允许的字段状态变化，但不允许因为迁移产生删行或重复行。

检查工单新字段：

```sql
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'tickets'
  AND column_name IN ('customer_user_id', 'assigned_agent_id')
ORDER BY column_name;
```

数据库校验通过后，才启动新版本应用并检查健康、登录和资源授权测试。

## 5. 备份恢复演练

备份存在和 SHA256 校验通过不能证明备份可恢复。必须在与 `TARGET_DB` 隔离的 `RESTORE_DB` 执行一次恢复；不得把恢复目标指向目标库。

### 5.1 恢复到独立数据库

确认 `RESTORE_DB` 不存在且不是 `TARGET_DB` 后，由具备数据库权限的操作者创建空恢复库：

```bash
psql -h "${TARGET_HOST}" -p "${TARGET_PORT}" -U "${TARGET_USER}" -d postgres \
  -c "CREATE DATABASE \"${RESTORE_DB}\";"
pg_restore \
  -h "${TARGET_HOST}" \
  -p "${TARGET_PORT}" \
  -U "${TARGET_USER}" \
  -d "${RESTORE_DB}" \
  --no-owner \
  --exit-on-error \
  "${BACKUP_DIR}/ecommerce-agent.dump"
```

恢复演练失败时停止，不覆盖或删除 `TARGET_DB`。恢复库仅用于本次验证，清理动作必须由操作者确认恢复库不再需要后单独执行。

### 5.2 恢复库验收

在 `RESTORE_DB` 校验迁移版本、业务表、外键、关键索引和完整数据量：

```bash
psql -h "${TARGET_HOST}" -p "${TARGET_PORT}" -U "${TARGET_USER}" -d "${RESTORE_DB}" \
  -c "SELECT version_num FROM public.alembic_version;"
PG_HOST="${TARGET_HOST}" \
PG_PORT="${TARGET_PORT}" \
PG_USER="${TARGET_USER}" \
PG_DBNAME="${RESTORE_DB}" \
alembic current
psql -h "${TARGET_HOST}" -p "${TARGET_PORT}" -U "${TARGET_USER}" -d "${RESTORE_DB}" \
  -c "SELECT table_name
      FROM information_schema.tables
      WHERE table_schema = 'public'
      ORDER BY table_name;"
psql -h "${TARGET_HOST}" -p "${TARGET_PORT}" -U "${TARGET_USER}" -d "${RESTORE_DB}" \
  -c "SELECT 'orders' AS table_name, COUNT(*) AS row_count FROM public.orders
      UNION ALL SELECT 'order_items', COUNT(*) FROM public.order_items
      UNION ALL SELECT 'tickets', COUNT(*) FROM public.tickets
      UNION ALL SELECT 'users', COUNT(*) FROM public.users
      UNION ALL SELECT 'sessions', COUNT(*) FROM public.sessions
      UNION ALL SELECT 'session_messages', COUNT(*) FROM public.session_messages
      ORDER BY table_name;"
psql -h "${TARGET_HOST}" -p "${TARGET_PORT}" -U "${TARGET_USER}" -d "${RESTORE_DB}" \
  -c "SELECT tc.table_name, tc.constraint_name, kcu.column_name,
             ccu.table_name AS referenced_table, ccu.column_name AS referenced_column
      FROM information_schema.table_constraints tc
      JOIN information_schema.key_column_usage kcu
        ON tc.constraint_name = kcu.constraint_name
       AND tc.table_schema = kcu.table_schema
      JOIN information_schema.constraint_column_usage ccu
        ON ccu.constraint_name = tc.constraint_name
       AND ccu.table_schema = tc.table_schema
      WHERE tc.table_schema = 'public'
        AND tc.constraint_type = 'FOREIGN KEY'
      ORDER BY tc.table_name, tc.constraint_name;"
psql -h "${TARGET_HOST}" -p "${TARGET_PORT}" -U "${TARGET_USER}" -d "${RESTORE_DB}" \
  -c "SELECT tc.table_name, tc.constraint_name, tc.constraint_type,
             kcu.column_name, kcu.ordinal_position
      FROM information_schema.table_constraints tc
      JOIN information_schema.key_column_usage kcu
        ON tc.constraint_name = kcu.constraint_name
       AND tc.table_schema = kcu.table_schema
      WHERE tc.table_schema = 'public'
        AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
      ORDER BY tc.table_name, tc.constraint_type,
               tc.constraint_name, kcu.ordinal_position;"
psql -h "${TARGET_HOST}" -p "${TARGET_PORT}" -U "${TARGET_USER}" -d "${RESTORE_DB}" \
  -c "SELECT c.conrelid::regclass AS table_name, c.conname,
             c.contype, c.convalidated, pg_get_constraintdef(c.oid)
      FROM pg_constraint c
      JOIN pg_namespace n ON n.oid = c.connamespace
      WHERE n.nspname = 'public'
        AND c.contype = 'c'
      ORDER BY c.conrelid::regclass::text, c.conname;"
psql -h "${TARGET_HOST}" -p "${TARGET_PORT}" -U "${TARGET_USER}" -d "${RESTORE_DB}" \
  -c "SELECT schemaname, tablename, indexname, indexdef
      FROM pg_indexes
      WHERE schemaname = 'public'
      ORDER BY tablename, indexname;"
pg_dump \
  -h "${TARGET_HOST}" \
  -p "${TARGET_PORT}" \
  -U "${TARGET_USER}" \
  -d "${RESTORE_DB}" \
  --schema-only \
  --no-owner \
  --file="${BACKUP_DIR}/schema-restored.sql"
diff -u "${BACKUP_DIR}/schema-before.sql" \
  "${BACKUP_DIR}/schema-restored.sql" \
  > "${BACKUP_DIR}/schema-restore.diff" || schema_diff_status=$?
if [ "${schema_diff_status:-0}" -gt 1 ]; then
    echo "schema diff 命令执行失败，停止验收。"
    exit 1
fi
```

`schema-restore.diff` 必须归档并人工审查。退出码为 1 只表示存在文本差异，不得直接判定失败或通过。允许的非语义差异仅包括：`pg_dump` 每次生成的 `\\restrict`/`\\unrestrict` token，以及 PostgreSQL 对已知 CHECK 表达式的等价重新格式化；这些差异必须由上面的系统目录查询、约束名称、类型、`convalidated` 和业务语义共同证明。出现其他字段、表、约束、外键或索引差异时，必须停止验收。

预期：

- 恢复库中的 `alembic_version` 与备份时记录的 `${SOURCE_REVISION}` 一致；
- 主键、唯一约束、CHECK 约束、外键和关键索引查询结果已保存，并与 `schema-before.sql` 对照；
- `schema-restore.diff` 已人工审查且没有未解释差异；
- 六张业务表的数据量与 `counts-before.txt` 逐项一致；
- `orders` 与 `order_items` 的外键关系可查询，不能只验证表存在。

恢复库验收记录至少包括：恢复库名称、恢复开始/结束时间、`pg_restore` 返回码、revision、表结构校验结果、六张表数据量和操作者。

## 6. `stamp` 使用条件

`stamp` 只写入 Alembic 版本记录，不执行 migration 的 `upgrade()`。

只有同时满足以下条件才允许使用：

1. schema 已经确认与目标 revision 完全等价；
2. `alembic_version` 缺失或版本记录丢失；
3. 已完成备份；
4. 已有 schema 对照证据、操作者记录和发布批准。

使用示例：

```bash
PG_HOST="${TARGET_HOST}" \
PG_PORT="${TARGET_PORT}" \
PG_USER="${TARGET_USER}" \
PG_DBNAME="${TARGET_DB}" \
alembic stamp 046df10e2506
PG_HOST="${TARGET_HOST}" \
PG_PORT="${TARGET_PORT}" \
PG_USER="${TARGET_USER}" \
PG_DBNAME="${TARGET_DB}" \
alembic current
PG_HOST="${TARGET_HOST}" \
PG_PORT="${TARGET_PORT}" \
PG_USER="${TARGET_USER}" \
PG_DBNAME="${TARGET_DB}" \
alembic upgrade head
```

不能直接执行以下命令掩盖结构不一致：

```bash
PG_HOST="${TARGET_HOST}" \
PG_PORT="${TARGET_PORT}" \
PG_USER="${TARGET_USER}" \
PG_DBNAME="${TARGET_DB}" \
alembic stamp head
```

如果缺字段、缺外键、缺索引或字段类型不同，应先写兼容 migration 或修复方案。

## 7. 失败处理

### 7.1 失败判断

以下任一情况都视为失败：

- Alembic 返回非 0；
- `current` 不是目标 revision；
- 关键表、字段、外键或索引缺失；
- `orders`、`order_items`、`tickets`、`users`、`sessions` 或 `session_messages` 任一受控业务表的数量变化；
- 应用启动或健康检查失败；
- 登录、资源范围或关键回归测试失败。

### 7.2 立即动作

1. 停止后续 migration；
2. 停止新版本应用发布并阻止新版本继续接收流量；
3. 保留迁移日志、应用日志、版本信息和备份；
4. 记录失败时间、命令和错误信息；
5. 确认事务是否已回滚；
6. 不要未经评估反复重跑或执行 `downgrade`。

### 7.3 恢复原则

生产优先前滚修复：

```text
定位原因 -> 修正或新增兼容 migration
        -> 在独立库重放并验证
        -> 审核后继续升级
```

只有在评估数据、锁和兼容性，并确认备份可用时才考虑恢复。恢复优先到隔离实例，不能未经批准直接覆盖生产库。

恢复后至少校验：

```sql
SELECT COUNT(*) FROM public.orders;
SELECT COUNT(*) FROM public.order_items;
SELECT COUNT(*) FROM public.tickets;
SELECT COUNT(*) FROM public.users;
SELECT COUNT(*) FROM public.sessions;
SELECT COUNT(*) FROM public.session_messages;
```

并重新确认关键表结构、外键、索引、`alembic_version`、应用健康和授权测试。

## 8. 旧默认账号处理

先只读查询，不自动删除：

```sql
SELECT id, username, role
FROM public.users
WHERE username IN ('admin', 'agent', 'operator', 'customer');
```

记录账号是否存在、角色、是否仍在使用、处理决定和审批人。

本阶段禁止自动执行：

```sql
DELETE FROM public.users ...;
UPDATE public.users SET password_hash = ...;
```

后续由运维或安全负责人确认采用密码轮换、停用、重命名或人工确认后删除。处理前必须检查订单、工单、会话和审计记录的外键及留存要求。

## 9. 收尾记录

归档发布时间、操作者、目标数据库、迁移前后 revision、备份路径和 SHA256、schema 对照、关键表数据量、健康检查、旧账号盘点结果和未解决风险。
