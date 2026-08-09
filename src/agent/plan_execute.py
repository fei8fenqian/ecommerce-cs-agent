"""
Plan-and-Execute Agent — 基于 LangGraph StateGraph 的多步规划执行引擎。

两个业务场景共用同一套图结构：
  build_pc:      配机选品（部分步骤可并行）
  troubleshoot:  故障诊断（全串行，每步依赖上一步）

图流转：
  planner → executor → judge ──通过──→ formatter → END
                        ↑    │
                        │    └──未通过──→ replanner → executor (循环)
                        └─────── 超过重试次数 ─────────→ formatter

你不需要手动调用节点。LangGraph 按图结构自动流转。
外部只需调 agent.run(query, scenario="build_pc")。
"""

import asyncio
import inspect
import json
from typing import Any, AsyncGenerator, TypedDict

from langgraph.graph import StateGraph

from agent.llm.llm_client import LLMClient
from agent.tools_registry import ToolRegistry, ToolResult
from config import settings

# =============================================================================
# Prompt 模板
# =============================================================================

PLANNER_BUILD_PC_PROMPT = """你是电脑装机规划师。根据用户需求，生成配件搜索计划。

## 可用工具
{tool_schema}

## 依赖规则
- motherboard 依赖 cpu（先确定CPU平台才能选主板）
- cooler 依赖 cpu（插槽必须兼容）
- psu 依赖 cpu + gpu（功耗决定电源功率）
- case 依赖 motherboard + gpu + psu（尺寸兼容）
- ram 依赖 motherboard（内存类型由主板决定）
- ssd、hdd 无依赖，可独立搜索

## 预算分配参考
- 游戏主机：CPU 20%、GPU 35%、主板 10%、内存 8%、SSD 8%、电源 8%、机箱 6%、散热器 5%
- 办公主机：CPU 25%、GPU 10%、主板 12%、内存 10%、SSD 12%、电源 8%、机箱 13%、散热器 10%

## 输出格式
只输出一个 JSON 数组，不要其他文字。每个 step 必须包含 action 字段，工具参数直接放在 step 里：
[
  {{"id": 1, "action": "search_component", "component": "cpu", "query": "5600X", "depends_on": [], "price_max": 1200}},
  {{"id": 2, "action": "search_component", "component": "gpu", "query": "RTX 4060", "depends_on": []}},
  {{"id": 3, "action": "search_component", "component": "motherboard", "query": "B650", "depends_on": [1]}},
  ...
]

## 用户需求
{user_query}

只输出 JSON 数组。"""

PLANNER_TROUBLESHOOT_PROMPT = """你是设备故障诊断专家。根据用户描述的问题，生成诊断步骤计划。

## 可用工具
{tool_schema}

## 诊断原则
- 第一步永远是 track_order 确认在保状态：在保 → 优先走官方售后，过保 → 可以 DIY 排查
- 根据在保/过保选择不同的知识库搜索策略
- 在保：搜索官方售后流程、送修注意事项
- 过保：搜索具体故障现象 + 对应品牌的知识库
- 知识库按 device_type × brand 拆分，brand 参数用英文小写（lenovo/huawei/xiaomi/apple 等）
- 只在确实无法自助解决时，才创建工单

## 输出格式
只输出一个 JSON 数组，不要其他文字。每个 step 必须包含 action 和 purpose 字段，工具参数直接放在 step 里：
[
  {{"id": 1, "action": "track_order", "keyword": "Y9000P",
    "depends_on": [], "purpose": "查询订单确认在保状态"}},
  {{"id": 2, "action": "search_product", "depends_on": [1],
    "query": "联想笔记本 无法开机",
    "table": "knowledge_chunks",
    "purpose": "搜索相关故障排查知识"}},
  {{"id": 3, "action": "create_ticket", "depends_on": [2],
    "issue": "笔记本无法开机，已尝试充电2小时仍无反应",
    "purpose": "无法自助解决则建工单"}}
]

## 用户问题
{user_query}

只输出 JSON 数组。"""


# =============================================================================
# 依赖字段映射 — executor 用这个表决定从前置步骤提取哪些字段
# =============================================================================
# key: (前置品类的 component 值, 当前品类的 component 值)
# value: 需要从前置结果的 normalized 中提取的字段名列表
#
# 例如搜主板(component="motherboard")依赖 CPU(component="cpu")，
# executor 查 ("cpu", "motherboard") → ["socket", "memory_type"]
# → 从 CPU 的 normalized 里取出 socket="Socket AM4", memory_type="DDR4"
# → 拼到搜索主板的 query 里: "B650 Socket AM4 DDR4"
# =============================================================================

DEPENDENCY_FIELDS: dict[tuple[str, str], list[str]] = {
    ("cpu", "motherboard"): ["socket", "memory_type"],
    ("cpu", "cooler"): ["socket"],
    ("cpu", "psu"): ["tdp"],
    ("vga", "psu"): ["power_draw"],
    ("vga", "case"): ["dimensions"],
    ("motherboard", "ram"): ["memory_type"],
    ("motherboard", "case"): ["form_factor"],
    ("psu", "case"): ["dimensions"],
}


# =============================================================================
# State — 在图中所有节点之间流转的共享字典
# =============================================================================
# total=False 表示所有字段可选。初始 state 可以只传 query + scenario，
# 后续节点逐步填充 plan → step_results → judge_passed → answer。
# =============================================================================


class PlanExecuteState(TypedDict, total=False):
    # ---- 输入 ----
    messages: list[dict[str, Any]]  # 多轮对话历史
    query: str  # 用户原始输入
    scenario: str  # "build_pc" 或 "troubleshoot"
    prompt: str  # 提示词

    # ---- planner 产出 ----
    plan: list[dict]  # LLM 生成的步骤列表
    # 配机 plan 元素示例:
    #   {"id": 1, "action": "search_component", "component": "cpu",
    #    "query": "5600X", "depends_on": [], "price_max": 1200}
    # 诊断 plan 元素示例:
    #   {"id": 1, "action": "track_order", "depends_on": [],
    #    "purpose": "查询订单确认在保状态"}

    # ---- executor 产出 ----
    step_results: dict[int, ToolResult]  # {step_id: 工具执行结果}

    # ---- judge 产出 ----
    judge_passed: bool  # True=通过, False=需要重试
    judge_reason: str  # 未通过的原因

    # ---- 循环控制 ----
    iteration: int  # 当前第几次重试
    max_iterations: int  # 最多重试几次（默认 3）

    # ---- 最终输出 ----
    answer: str  # formatter 生成的用户可读回答
    total_tokens: int  # 所有 LLM 调用的 token 消耗合计


# =============================================================================
# PlanAndExecuteAgent — 配机 + 故障诊断的规划执行引擎
# =============================================================================


class PlanAndExecuteAgent:
    """Plan-and-Execute Agent，用 LangGraph StateGraph 编排多步规划执行。

    用法:
        agent = PlanAndExecuteAgent(llm_client, tool_registry)
        result = await agent.run("5000 预算打 3A 游戏", scenario="build_pc")
        result["answer"] 是最终的用户可读回答
    """

    def __init__(
        self,
        llm: LLMClient,  # LLM 客户端，用于 planner/replanner/formatter
        registry: ToolRegistry,  # 工具注册中心，executor 用它调工具
        *,
        max_iterations: int = 3,  # judge 不通过最多重试几次
    ):
        self.llm = llm
        self.registry = registry
        self.max_iterations = max_iterations
        # 初始化时编译好图，存起来复用
        self._graph = self._build_graph()

    # =========================================================================
    # 图构建 — 定义节点和边，LangGraph 负责按序调用
    # =========================================================================

    def _build_graph(self):
        """构建并编译 StateGraph。只定义结构，不执行任何逻辑。"""
        graph = StateGraph(state_schema=PlanExecuteState)

        # 注册节点：名字 + 对应的 async 方法
        graph.add_node("planner", self._planner)
        graph.add_node("executor", self._executor)
        graph.add_node("judge", self._judge)
        graph.add_node("replanner", self._replanner)
        graph.add_node("formatter", self._formatter)

        # 定义流转边
        graph.add_edge("planner", "executor")  # planner 之后必定 executor
        graph.add_edge("executor", "judge")  # executor 之后必定 judge
        # judge 之后根据条件分叉（见 _route_after_judge）
        graph.add_conditional_edges("judge", self._route_after_judge)
        graph.add_edge("replanner", "executor")  # replanner 之后回到 executor

        graph.set_entry_point("planner")  # 从 planner 开始
        graph.set_finish_point("formatter")  # formatter 结束

        return graph.compile()

    # =========================================================================
    # 条件路由 — judge 之后去 formatter 还是 replanner
    # =========================================================================

    def _route_after_judge(self, state: PlanExecuteState) -> str:
        """根据 judge 结果决定下一步。

        LangGraph 要求返回一个字符串，对应 add_conditional_edges
        里的节点名（"formatter" 或 "replanner"）。
        """
        judge_passed: bool = state.get("judge_passed", True)
        iteration: int = state.get("iteration", 0)
        max_iter: int = state.get("max_iterations", self.max_iterations)

        if not judge_passed and iteration < max_iter:
            return "replanner"  # 未通过且还有重试次数 → 重新规划
        return "formatter"  # 通过 或 次数用完 → 输出结果

    # =========================================================================
    # planner 节点 — LLM 生成结构化执行计划
    # =========================================================================

    async def _planner(self, state: PlanExecuteState) -> dict:
        """根据用户 query + scenario，调 LLM 生成步骤列表。

        输入: state["query"], state["scenario"]
        输出: {"plan": [step1, step2, ...]}  合并回 state

        最多重试 2 次 LLM 调用（JSON 解析失败时重试）。
        """
        query: str = state["query"]
        scenario: str = state.get("scenario", "build_pc")
        # 把工具列表转成 JSON 字符串，让 LLM 知道有哪些工具可用
        tool_schema: str = json.dumps(self.registry.to_openai_schemas(), ensure_ascii=False)

        # 根据场景选 prompt
        if scenario == "build_pc":
            prompt = PLANNER_BUILD_PC_PROMPT.format(user_query=query, tool_schema=tool_schema)
        else:
            prompt = PLANNER_TROUBLESHOOT_PROMPT.format(user_query=query, tool_schema=tool_schema)

        total_tokens: int = state.get("total_tokens", 0)

        # 最多 2 次尝试：LLM 可能输出格式错误
        for _ in range(2):
            response = await self.llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=settings.temperature,
                max_tokens=settings.max_tokens,
            )
            total_tokens += response.usage.total_tokens
            plan: list[dict] | None = self._extract_json(response.content or "")
            if plan and self._validate_plan(plan):
                return {"plan": plan, "total_tokens": total_tokens}

        # 两次都失败 → 空 plan，executor 会跳过
        return {"prompt": prompt, "plan": [], "total_tokens": total_tokens}

    # =========================================================================
    # executor 节点 — 按拓扑顺序执行 plan 里的每一步
    # =========================================================================

    async def _executor(self, state: PlanExecuteState) -> dict:
        """执行 state["plan"] 里的所有步骤。

        流程:
        1. 拓扑排序 plan → 分成多个 batch
        2. 同一 batch 内的步骤 asyncio.gather 并行执行
        3. 不同 batch 之间串行（下一 batch 依赖上一 batch 的结果）

        输入: state["plan"], state["step_results"]（replan 时已有部分结果）
        输出: {"step_results": {step_id: ToolResult, ...}}
        """
        plan: list[dict] = state["plan"]

        # planner 失败 → 空 plan
        if not plan:
            return {"step_results": {}}

        # step_results 可能已有值（replanner 循环时）
        step_results: dict[int, ToolResult] = state.get("step_results", {})

        batches: list[list[dict]] = _topological_sort(plan)

        for batch in batches:
            # 同一批次的步骤没有互相依赖，可以并行执行
            tasks = [self._execute_single_step(step, step_results, plan) for step in batch]
            # asyncio.gather 同时启动所有协程，等全部完成后返回
            batch_results: list[tuple[int, ToolResult]] = await asyncio.gather(*tasks)

            for step_id, result in batch_results:
                step_results[step_id] = result

        return {"step_results": step_results}

    async def _execute_single_step(
        self,
        step: dict,  # plan 里的一个步骤
        step_results: dict[int, ToolResult],  # 已完成的步骤结果（用于提取依赖字段）
        plan: list[dict],  # 完整 plan（用于查找依赖步骤的 component）
    ) -> tuple[int, ToolResult]:
        """执行单个 step：调工具 + 自动化依赖传参。

        返回 (step_id, ToolResult)。

        依赖传参逻辑:
        1. 遍历 step["depends_on"]
        2. 查 step_results 拿到依赖步骤的 ToolResult
        3. 从 ToolResult.data.results[0].normalized 提取兼容性字段
        4. 把提取到的值拼进当前步骤的 query 参数
           → 例: "B650" → "B650 Socket AM4 DDR5"
        """
        # ---- 1. 获取工具 ----
        action_name: str = step.get("action", "")
        tool = self.registry.get(action_name)
        if tool is None:
            return (
                int(step.get("id", -1)),
                ToolResult(name=action_name, status="error", error=f"未知工具: {action_name}"),
            )

        # ---- 2. 过滤参数：只保留工具 execute 方法签名里有的参数 ----
        sig = inspect.signature(tool.execute)
        valid_keys: set[str] = set(sig.parameters.keys()) - {"self"}
        params: dict[str, Any] = {k: v for k, v in step.items() if k in valid_keys}

        # ---- 3. 从依赖步骤提取 normalized 字段，拼进 query ----
        curr_comp: str = step.get("component", "")  # 当前步骤在搜什么品类
        extra_terms: list[str] = []

        for dep_id in step.get("depends_on", []):
            prev_result: ToolResult | None = step_results.get(dep_id)
            if prev_result is None or not prev_result.is_success:
                continue

            # 在 plan 里找到依赖的步骤，拿到它的 component
            dep_step: dict | None = next((s for s in plan if s.get("id") == dep_id), None)
            dep_comp: str = dep_step.get("component", "") if dep_step else ""

            # 查映射表：这个品类对需要提取哪些字段
            fields: list[str] = DEPENDENCY_FIELDS.get((dep_comp, curr_comp), [])

            # 从 ToolResult.data.results[0].normalized 里取字段值
            # data 结构: {"count": N, "results": [{"title": ..., "normalized": {...}}]}
            search_results: list[dict] = prev_result.data.get("results", [])
            if search_results:
                normalized: dict[str, Any] = search_results[0].get("normalized", {})
                for field_name in fields:
                    val = normalized.get(field_name)
                    if val:
                        extra_terms.append(str(val))

        # ---- 4. 把提取到的值追加到 query 里 ----
        if extra_terms and "query" in params:
            params["query"] = params["query"] + " " + " ".join(extra_terms)

        # ---- 5. 执行工具 ----
        result: ToolResult = await self.registry.execute(action_name, **params)
        return (int(step.get("id", -1)), result)

    # =========================================================================
    # judge 节点 — 检查执行结果是否满足要求（兼容性/根因）
    # =========================================================================

    async def _judge(self, state: PlanExecuteState) -> dict:
        """检查执行结果是否满足要求。

            配机：从 state 提取 plan + step_results → _build_component_map →
        跑兼容性检查列表。
            诊断：直接通过（后续用 LLM 判断根因）。

            返回: {"judge_passed": True, "judge_reason": ""}
            或   {"judge_passed": False, "judge_reason": "CPU 插槽 AM4 与主板插槽 LGA1700
        不兼容; ..."}

            逻辑:
            1. state["scenario"] != "build_pc" → 直接返回 {"judge_passed": True,
        "judge_reason": ""}
            2. 从 state 取出 plan + step_results
            3. 调用 _build_component_map(plan, step_results) 得到 comp
            4. 遍历 check 函数列表 [_check_socket_match, _check_memory_type_match,
        _check_power_enough]
            5. 收集失败原因 → 有失败则 judge_passed=False
        """
        if state["scenario"] != "build_pc":
            return {"judge_passed": True, "judge_reason": ""}

        plan = state["plan"]
        step_results = state.get("step_results", {})
        comp_map = _build_component_map(plan, step_results)
        judge: dict[str, Any] = {}
        judge_reason = ""
        judge_passed = True
        for check_func in [
            _check_socket_match,
            _check_memory_type_match,
            _check_power_enough,
        ]:
            passed, reason = check_func(comp_map)
            if not passed:
                judge_passed = False
                judge_reason += f";{reason}"
        judge["judge_passed"] = judge_passed
        judge["judge_reason"] = judge_reason
        return judge

    # =========================================================================
    # replanner 节点 — 分析失败原因，重新生成 plan
    # =========================================================================

    async def _replanner(self, state: PlanExecuteState) -> dict:
        """LLM 分析 judge 失败原因，重新生成 plan。

        输入: state["query"], state["plan"] (旧的), state["step_results"],
            state["judge_reason"], state.get("iteration", 0)

        返回: {"plan": [...新的步骤列表...], "iteration": iteration + 1}

        逻辑:
        1. 从 state 取出原始 query、旧 plan、step_results、judge_reason、当前
        iteration
        2. 把旧 plan 和每个步骤搜到的结果 title 拼成"We tried but failed"的描述
        3. 调 LLM 生成新 plan（prompt 里告诉它失败原因，让它换策略）
        4. 用 _extract_json + _validate_plan 校验（和 planner 一样）
        5. iteration + 1
        """
        query: str = state["query"]
        old_plan: list[dict] = state["plan"]
        step_results: dict[int, ToolResult] = state.get("step_results", {})
        judge_reason: str = state.get("judge_reason", "")
        iteration: int = state.get("iteration", 0)

        # 拼出"已经搜到了什么"的描述
        found_lines: list[str] = []
        for step in old_plan:
            sid: int = step.get("id", -1)
            result: ToolResult | None = step_results.get(sid)
            if result and result.is_success:
                title = result.data.get("results", [{}])[0].get("title", "?")
                found_lines.append(f"  step {sid} ({step.get('component', '?')}): {title}")
            else:
                found_lines.append(f"  step {sid} ({step.get('component', '?')}): 无结果")

        found_summary = "\n".join(found_lines) if found_lines else "（无）"
        tool_schema: str = json.dumps(self.registry.to_openai_schemas(), ensure_ascii=False)
        scenario: str = state.get("scenario", "build_pc")

        if scenario == "build_pc":
            re_prompt = f"""你是电脑装机规划师。之前的搜索计划未能通过兼容性检查，请调整策略重新规划。

可用工具:
{tool_schema}

用户需求: {query}

之前的计划:
{json.dumps(old_plan, ensure_ascii=False)}

已搜到的配件:
{found_summary}

兼容性检查失败原因: {judge_reason}

请更换搜索关键词（尝试不同品牌、型号、价格区间），重新输出 JSON 数组。"""
        else:
            re_prompt = f"""你是设备故障诊断专家。之前的诊断步骤未能定位根因，请调整策略重新排查。

可用工具:
{tool_schema}

用户问题: {query}

之前的诊断步骤:
{json.dumps(old_plan, ensure_ascii=False)}

已获取的信息:
{found_summary}

失败原因: {judge_reason}

请更换排查思路（尝试不同关键词、不同知识库），重新输出 JSON 数组。"""

        total_tokens = state.get("total_tokens", 0)

        for _ in range(2):
            response = await self.llm.chat(
                [{"role": "user", "content": re_prompt}],
                temperature=settings.temperature,
                max_tokens=settings.max_tokens,
            )
            total_tokens += response.usage.total_tokens
            new_plan: list[dict] | None = self._extract_json(response.content or "")
            if new_plan and self._validate_plan(new_plan):
                return {"plan": new_plan, "iteration": iteration + 1, "total_tokens": total_tokens}

        # LLM 实在是生成不了合法 plan，放弃重试，让 formatter 用现有结果输出
        return {"judge_passed": True, "iteration": iteration + 1, "total_tokens": total_tokens}

    # =========================================================================
    # formatter 节点 — 将执行结果格式化为用户可读回答
    # =========================================================================

    async def _formatter(self, state: PlanExecuteState) -> dict:
        """将执行结果格式化为用户可读回答。

        配机: 配置清单表格。诊断: 诊断结论+操作建议。

        返回: {"answer": "格式化后的回答文本"}
        """
        scenario: str = state["scenario"]
        plan: list[dict] = state["plan"]
        step_results: dict[int, ToolResult] = state.get("step_results", {})
        answer: str

        if scenario == "build_pc":
            total_price: int = 0
            parts: list[tuple[str, str, int]] = []
            for step in plan:
                sid: int = step.get("id", -1)
                step_res: ToolResult | None = step_results.get(sid)
                if step_res is None or not step_res.is_success:
                    continue
                result = step_res.data.get("results", [{}])[0]
                if not result:
                    continue
                title = result.get("title", "")
                price: int = result.get("price", 0) or 0
                category = result.get("category", "")
                if not title and not category:
                    continue
                total_price += price
                parts.append((category, title, price))

            table_lines = ["| 品类 | 型号 | 价格 |", "|------|------|------|"]
            for category, title, price in parts:
                table_lines.append(f"| {category} | {title} | ¥{price} |")
            table_lines.append(f"| **合计** | | **¥{total_price}** |")
            answer = "\n".join(table_lines)

        elif scenario == "troubleshoot":
            ts_lines: list[str] = []
            for ts_step in plan:
                ts_sid: int = ts_step.get("id", -1)
                purpose: str = ts_step.get("purpose", "")
                action: str = ts_step.get("action", "")
                ts_res: ToolResult | None = step_results.get(ts_sid)

                ts_lines.append(f"### {purpose}")
                if ts_res is None or not ts_res.is_success:
                    ts_lines.append(f"执行失败：{ts_res.error if ts_res else '未知错误'}")
                    continue

                if action == "track_order":
                    data = ts_res.data
                    ts_lines.append(f"订单状态：{data.get('status', '未知')}")
                    ts_lines.append(f"在保状态：{data.get('warranty', '未知')}")

                elif action == "search_product":
                    results = ts_res.data.get("results", [])
                    if results:
                        for r in results[:3]:
                            ts_lines.append(f"- **{r.get('title', '?')}**")
                            content = r.get("content", "")[:200]
                            ts_lines.append(f"  {content}...")
                    else:
                        ts_lines.append("未找到相关知识")

                elif action == "create_ticket":
                    ts_lines.append(f"工单结果：{ts_res.data.get('message', ts_res.data)}")

                else:
                    ts_lines.append(f"```json\n{json.dumps(ts_res.data, ensure_ascii=False)}\n```")

                ts_lines.append("")

            answer = "\n".join(ts_lines) if ts_lines else "暂无诊断结果"

        else:
            answer = "暂不支持该场景"

        return {"answer": answer}

    # =========================================================================
    # 外部入口
    # =========================================================================

    async def run(
        self,
        query: str,
        *,
        history: list[dict[str, Any]] | None = None,
        scenario: str = "build_pc",
    ) -> dict:
        """执行 Plan-and-Execute 并返回最终 state。

        Args:
            query: 用户原始输入
            history: 多轮对话历史（可选）
            scenario: "build_pc" | "troubleshoot"

        Returns:
            完整的 PlanExecuteState（包含 answer, plan, step_results 等）
        """
        initial_state = PlanExecuteState(
            messages=history or [],
            query=query,
            scenario=scenario,
            max_iterations=self.max_iterations,
        )
        final_state: dict = await self._graph.ainvoke(initial_state)
        return final_state

    async def run_stream(
        self,
        query: str,
        *,
        history: list[dict[str, Any]] | None = None,
        scenario: str = "build_pc",
    ) -> AsyncGenerator[dict, None]:
        """SSE 流式执行 Plan-and-Execute"""
        initial_state = PlanExecuteState(
            messages=history or [],
            query=query,
            scenario=scenario,
            max_iterations=self.max_iterations,
        )

        full_state = None

        async for chunk in self._graph.astream(initial_state, stream_mode=["updates", "values"]):
            # chunk 是 tuple: ({"planner": {"plan": [...]}}, {*完整state*})
            node_delta, full_state = chunk
            node_name = list(node_delta.keys())[0]
            yield {"event": "node_complete", "name": node_name, "data": node_delta}

        yield {"event": "done", "data": full_state}

    # =========================================================================
    # JSON 解析 — 从 LLM 返回中提取 JSON 数组
    # =========================================================================

    @staticmethod
    def _extract_json(text: str | None) -> list[dict] | None:
        """从 LLM 返回值中提取 JSON 数组。

        处理两种情况:
        - 纯 JSON: "[...]"
        - Markdown 包裹: "```json\n[...]\n```"

        返回 None 表示解析失败（调用方会重试）。
        """
        if text is None:
            return None

        text = text.strip()

        # 去掉 markdown 代码块标记
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) >= 3 else text

        try:
            return json.loads(text)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            return None

    # =========================================================================
    # Plan 校验
    # =========================================================================

    def _validate_plan(self, plan: list[dict]) -> bool:
        """校验 planner 生成的 plan 结构是否合法。

        检查项:
        1. 每个 step 有合法的 id(int)、action(str)、depends_on(list)
        2. action 是已注册的工具名
        3. 没有重复 id
        4. 没有自依赖（step 依赖自己）
        5. depends_on 里的 id 都指向已出现的步骤（不能依赖不存在的 id）
        """
        seen_ids: set[int] = set()

        for step in plan:
            sid = step.get("id")
            action: str | None = step.get("action")
            depends_on: list | None = step.get("depends_on")

            # 类型检查
            if not isinstance(sid, int) or not isinstance(action, str) or not isinstance(depends_on, list):
                return False

            # 工具名是否在 registry 里
            if self.registry.get(action) is None:
                return False

            # 不允许重复 id
            if sid in seen_ids:
                return False

            # 不允许自依赖
            if sid in depends_on:
                return False

            # 依赖的步骤必须已经出现过（按 id 顺序遍历）
            for dep_id in depends_on:
                if dep_id not in seen_ids:
                    return False

            seen_ids.add(sid)

        return True


# =============================================================================
# 拓扑排序 — 把步骤按依赖关系分批
# =============================================================================


def _topological_sort(steps: list[dict]) -> list[list[dict]]:
    """根据 depends_on 把步骤分成多个批次。

    同一批次内的步骤没有互相依赖 → 可以并行执行。
    不同批次之间 → 上一批次全部完成后才能开始下一批次。

    例:
        输入: [
            {"id": 1, "depends_on": []},          # CPU, 无依赖
            {"id": 2, "depends_on": []},          # GPU, 无依赖
            {"id": 3, "depends_on": [1]},         # 主板, 依赖CPU
            {"id": 4, "depends_on": [3]},         # 内存, 依赖主板
        ]
        输出: [
            [{id:1}, {id:2}],   # batch 0: CPU+GPU 并行搜
            [{id:3}],           # batch 1: 主板等 CPU 结果
            [{id:4}],           # batch 2: 内存等主板结果
        ]
    """
    # 已经分配到某个批次的步骤 id
    completed: set[int] = set()
    batches: list[list[dict]] = []

    while len(completed) < len(steps):
        batch: list[dict] = []

        for step in steps:
            sid: int = step["id"]
            depends_on: list[int] = step.get("depends_on", [])

            # 已经分过批，跳过
            if sid in completed:
                continue

            # 这个步骤的所有依赖项都已完成 → 可以进入当前批次
            if all(dep_id in completed for dep_id in depends_on):
                batch.append(step)

        # 将本批次的步骤标记为已完成
        for step in batch:
            completed.add(step["id"])

        batches.append(batch)

    return batches


def _build_component_map(
    plan: list[dict[str, Any]],
    step_results: dict[int, ToolResult],
) -> dict[str, dict]:
    """从 plan 和 step_results 提取每个品类的 normalized 字段。

    返回: {"cpu": {socket: "AM4", tdp: 65, ...}, "motherboard": {...}, ...}

    逻辑:
    1. 遍历 plan，找到每个 step 的 component + step_id
    2. 用 step_id 从 step_results 取 ToolResult
    3. 取 ToolResult.data.results[0].normalized（第一个/最佳匹配）
    4. 以 component 名为 key 放入返回 dict
    5. 搜索失败/无结果的品类跳过（不存在 dict 里）
    """
    component_map: dict[str, dict] = {}
    for step in plan:
        sid = step.get("id")
        component = step.get("component")
        if sid is None or component is None:
            continue
        tool_res: ToolResult = step_results.get(sid)
        if tool_res is None:
            continue
        res = tool_res.data.get("results", [{}])[0]
        if not res:
            continue
        normalized = res.get("normalized", {})
        if not normalized:
            continue
        component_map[component] = normalized
    return component_map


def _check_socket_match(comp: dict[str, dict]) -> tuple[bool, str]:
    """CPU 和主板的 socket 必须一致。

        comp 结构: {"cpu": {"socket": "AM4", ...}, "motherboard": {"socket": "AM4",
    ...}}

        返回: (True, "") 或 (False, "CPU 插槽 AM4 与主板插槽 LGA1700 不兼容")

        检查逻辑：
        - 需要 cpu 和 motherboard 两个 key 都存在才检查
        - 只要有一个不存在 → 跳过，返回 True（不误判）
        - 比对 comp["cpu"].get("socket") 和 comp["motherboard"].get("socket")
    """
    cpu = comp.get("cpu")
    motherboard = comp.get("motherboard")
    if cpu is None or motherboard is None:
        return (True, "")
    cpu_socket = cpu.get("socket")
    motherboard_socket = motherboard.get("socket")
    if cpu_socket is None or motherboard_socket is None:
        return (True, "")
    if cpu_socket == motherboard_socket:
        return (True, "")
    else:
        return (False, f"CPU 插槽 {cpu_socket} 与主板插槽 {motherboard_socket} 不兼容")


def _parse_watt(s: str | None) -> int | None:
    """从 "65W" / "575W" / "360W (最大400W)" 中提取第一个数字。失败返回 None。"""
    if not s:
        return None
    import re

    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def _check_memory_type_match(comp: dict[str, dict]) -> tuple[bool, str]:
    """主板和内存的 memory_type 必须匹配。

    主板 memory_type 为 "4×DDR4 DIMM"，内存为 "DDR4" → 子串匹配。
    需要 motherboard 和 ram 两个 key 都存在才检查。
    """
    mb = comp.get("motherboard")
    ram = comp.get("ram")
    if mb is None or ram is None:
        return (True, "")

    mb_type: str | None = mb.get("memory_type")
    ram_type: str | None = ram.get("memory_type")
    if mb_type is None or ram_type is None:
        return (True, "")

    if ram_type in mb_type:
        return (True, "")
    return (False, f"主板支持 {mb_type} 但内存是 {ram_type}，不兼容")


def _check_power_enough(comp: dict[str, dict]) -> tuple[bool, str]:
    """CPU TDP + GPU power_draw 不超过电源额定功率。

    需要 cpu + vga + psu 三个 key 都存在才检查。
    任意一个数值解析失败 → 跳过（不误判）。
    """
    cpu = comp.get("cpu")
    vga = comp.get("vga")
    psu = comp.get("psu")
    if cpu is None or vga is None or psu is None:
        return (True, "")

    cpu_w = _parse_watt(cpu.get("tdp"))
    vga_w = _parse_watt(vga.get("power_draw"))
    psu_w = _parse_watt(psu.get("wattage"))
    if cpu_w is None or vga_w is None or psu_w is None:
        return (True, "")

    total = cpu_w + vga_w
    if total <= psu_w:
        return (True, "")
    return (False, f"CPU + GPU 功耗 {total}W 超出电源额定 {psu_w}W，建议升级电源")
