# S1-01 资源归属与权限入口盘点

> 盘点时间：2026-08-17
>
> 目的：为 V6 Sprint 1 建立“资源 → owner/scope → 角色 → 读取/写入入口”基线。
> 本文只记录当前代码事实和待决缺口，不代表 S1 已验收，也不授权进入订单/退款实现。

## 1. 角色与身份来源

| 身份 | 来源 | 当前应用方式 | 资源范围原则 |
|---|---|---|---|
| `customer` | JWT `sub` → `request.state.user["id"]` | 外部用户通过认证后不经过 Casbin 资源路径授权 | 只能访问自身订单、工单、会话；范围必须由服务/store 查询条件强制执行 |
| `agent` | JWT + Casbin | 内部用户先经过 Casbin 路径/方法匹配 | 当前计划要求按认领范围访问工单；现代码尚未实现认领模型 |
| `finance` | V6 目标角色 | 当前代码未实现 | 仅访问财务审批队列；不得提前接入支付执行 |
| `operator` | JWT + Casbin | 内部用户先经过 Casbin | 运营数据应脱敏聚合；当前代码无对应业务 API |
| `admin` | JWT + Casbin | Casbin `*` 全放行 | 系统管理，不应因此默认获得业务 PII 或退款审批职责；需在 V6 领域 API 中重新细化 |

## 2. 资源映射

| 资源 | owner / scope | 读取入口 | 写入入口 | 当前强制点 | 当前缺口 |
|---|---|---|---|---|---|
| `sessions` | `sessions.owner_user_id = current_user.id` | `GET /api/v1/sessions`、`GET /api/v1/sessions/{id}`、`POST /api/v1/chat`、`POST /api/v1/chat/stream` | `POST /api/v1/chat`、`POST /api/v1/chat/stream`、`DELETE /api/v1/sessions/{id}` | store 查询带 `owner_user_id`；未知/他人 session 返回 404；消息级联删除 | 列表无分页；详情/LLM 上下文仅取最近 200 条；保留期、删除策略和访问审计未定义 |
| `tickets` | 目标为 `tickets.customer_user_id = current_user.id`；内部角色另需 assignment/scope | 实际路由：`GET /api/v1/tickets`、`GET /api/v1/tickets/{id}` | 实际路由：`PATCH /api/v1/tickets/{id}`；Agent 工具 `create_ticket` | 仅有数据库可空外键列，当前查询未使用 | API 未接收当前用户；store 的 list/get/update 无 owner 参数；创建工单不写 owner；customer 可被外部认证放行到全量路由 |
| `orders` | 目标为 `orders.customer_user_id = current_user.id`；内部角色按 V6 数据范围 | 当前业务入口主要是 Agent 工具 `track_order` | 当前无订单写 API | 仅有数据库可空外键列和索引 | `track_order` 按订单号/手机号查询，未接 user scope；历史数据未回填；没有订单 API 的资源级集成测试 |
| `order_items` | 继承所属订单 scope | 被 `track_order` join 读取 | 无当前写入口 | 通过 `order_id` 外键关联订单 | 订单查询未先按 owner 限制，子项会随越权订单泄露 |
| `chat` | 会话 scope 由 session owner 继承；商品/知识内容为公共只读 | `POST /api/v1/chat`、`POST /api/v1/chat/stream` | 会话消息写入 PG；Agent 工具可能写工单 | session 路径传入当前 user ID | Agent 工具执行层没有统一 actor/scope；内部 `chat/stream` 与 Casbin policy 也未对齐 |
| 商品/知识库 | 公共只读（不含客户 PII） | `search_product`、`search_component`、`compare_products`、`check_stock`、RAG | 当前无客户写入口 | 工具执行 SQL 参数化/白名单情况由各工具自行保证 | 注册表没有按角色/风险统一过滤，所有已注册工具默认可见 |

## 3. 当前路由与 Casbin 契约偏差

当前 Casbin policy 与实际路由不一致，必须在 S1-01 审查阶段裁决，不能等到前端或退款阶段再修：

| 项目 | Casbin policy | 实际代码 | 影响 |
|---|---|---|---|
| customer 工单列表/详情 | `/api/v1/tickets/my/*` + `GET` | `/api/v1/tickets`、`/api/v1/tickets/{ticket_id}` | external customer 不经过 Casbin，但 API 又无 owner 过滤，可能读到全量工单 |
| agent 工单更新 | `/api/v1/tickets/*` + `PUT` | `/api/v1/tickets/{ticket_id}` + `PATCH` | agent 的实际更新请求可能被 Casbin 拒绝 |
| chat 流式接口 | 只有 `/api/v1/chat` + `POST` | 另有 `/api/v1/chat/stream` + `POST` | internal agent/operator 的流式入口可能被 Casbin 拒绝 |
| 角色集合 | `customer/agent/operator/admin` | V6 要求新增 `finance`、`refund-worker` | 支付/审批实现前必须先冻结新角色和机器主体权限 |

## 4. Agent 工具清单

`main.py` 当前把以下工具全部注册到同一个 `ToolRegistry`，没有按当前用户、角色、资源归属或风险等级过滤：

| 工具 | 数据范围/副作用 | 当前 actor/scope | S1 处理结论 |
|---|---|---|---|
| `search_product` | 商品检索，只读 | 无 actor，公共数据 | 可保留公共只读；纳入工具权限矩阵 |
| `search_component` | 配件检索，只读 | 无 actor，公共数据 | 可保留公共只读；纳入工具权限矩阵 |
| `compare_products` | 商品检索，只读 | 无 actor，公共数据 | 可保留公共只读；纳入工具权限矩阵 |
| `check_stock` | 库存查询，只读但属运营数据 | 无 actor/scope | 明确 customer 是否可见库存细节，不能由 Prompt 决定 |
| `track_order` | 订单及物流、金额、商品项读取 | 不读取当前 user；按订单号/手机号查询 | S1 P0：接入 actor 与 `customer_user_id` scope，补越权测试 |
| `create_ticket` | 创建工单，写 PII/客服业务数据 | 不读取当前 user；不写 owner | S1 P0：创建必须绑定 current user，内部创建需明确代理身份与审计 |

## 5. S1-01 结论与验收前置项

当前盘点结论是：会话资源归属已有可用实现；订单和工单只有 schema 起点，尚未形成应用层授权闭环；工具层尚未具备统一的 actor/scope 边界。因此 S1-01 产物完成，但 S1 整体不能标记完成。

在进入 S1-02/S1-03 前，负责人需要确认以下四点：

1. `customer_user_id` 的历史回填规则：现有 `orders.customer_id` 是合成字符串，必须给出与 `users.id` 的可审计映射；无法映射的记录进入异常队列，不得静默归给某个用户。
2. 工单内部范围：V6 要求 agent 按 `assigned_agent_id` 访问，但当前 schema 没有认领字段；需在设计阶段决定字段、认领命令和未认领队列范围。
3. Casbin policy 与实际路径/HTTP 方法的唯一契约；尤其是 `PATCH`、`/chat/stream` 和 customer 的资源路径。
4. Redis 登录态故障策略与消息数据治理：token 校验需 fail-closed 或依赖高可用；会话消息需定义保留、删除、备份脱敏和访问审计。

**建议状态：** `S1-01 = 已产出，待负责人审查；S1-02/S1-03/S1-04 = 未开始或未完成。` 在审查通过前保持订单/退款实现冻结。
