# 客户支持真实语料评测

## 数据来源

当前有两层真实语料：

1. **主评测：JDDC 脱敏客服会话**。官方基线仓库说明 `data/chat.txt` 来自京东客服与客户的真实聊天，
   已做脱敏处理；保留 `user/客服` 角色和多轮上下文。它用于 3C 客服全域安全扫描。
2. **补充扫描：E-commerce Dialogue Corpus（ECD）**。官方仓库说明该语料来自电商客服多轮对话，
   测试集包含 10,000 个 session-response pair；原始格式是 `label\tconversation\tresponse`，用于检索
   和更广泛的电商口语鲁棒性。

原始数据不放进本仓库：下载文件保存在本机外部目录，评测脚本只读取路径。这样不会把来源不明的原始
对话再次分发，也避免把外部语料误当成项目自有数据。

## 公开数据集的正确用法

公开数据集的标签只对它声明的任务负责，不能把来源数据的标签直接改名成项目动作金标。当前核实到
的 GitHub/论文数据集可以这样使用：

| 数据集 | 原始标注/任务 | 适合评估 | 不适合直接证明 |
| --- | --- | --- | --- |
| [JDDC 官方论文与基线](https://aclanthology.org/2020.lrec-1.58.pdf) / [Baseline](https://github.com/SimonJYang/JDDC-Baseline-Seq2Seq) | 京东多轮客服；论文描述了 289 个 query intent 和带候选回复权重的挑战集。当前 `chat.txt` 只有角色、转人工、重复等字段 | 客服意图识别、回复检索/生成、上下文鲁棒性 | 项目动作（是否建单、是否退款）金标；当前文件的 `is_transfer` 也不能代替动作标签 |
| [DataCLUE/CIC](https://github.com/CLUEbenchmark/DataCLUE) | 118 类中文客户意图；train/dev 有噪声，公开测试集是高质量集 | 单轮意图分类、标签定义和混淆分析 | 多轮状态判断、权限检查、工单创建决定 |
| [CSDS](https://github.com/xiaolinAndy/CSDS) | 人工标注整体/用户/客服摘要、主题片段和 QA | 工单原因重建、财务摘要、主题切分、QA 匹配 | 动作安全、金额和订单状态正确性 |
| [DianJin-CSC/CSConv](https://github.com/aliyun/qwen-dianjin/tree/master/DianJin-CSC) | 1,855 条真实客服会话经 LLM 重写，并按 COPC 定义 5 个阶段、12 个服务策略标注；数据在 [Hugging Face](https://huggingface.co/datasets/DianJin/DianJin-CSC-Data) | 回复是否重述问题、澄清、共情、给出下一步等策略质量 | 纯人工事实金标、3C 业务动作或支付状态 |
| [DialEval-1](https://github.com/DialEval-1/dataset) | 中文客服对话的人工作务完成度、满意度、有效性评分 | 校准“任务是否完成”和回复质量的评审器 | 3C 电商领域意图和工具调用 |
| [ECD](https://github.com/cooelf/DeepUtteranceAggregation) | 候选客服回复相关性标签 | 检索排序、候选回复选择 | 工单、退款、维修等业务动作 |

JDDC、ECD 和 CSDS 解决的是不同层：意图/回复检索/摘要。它们不能组成一份现成的
`expected_action` 文件。到目前为止没有发现公开数据集同时提供“客户原话 + 真实订单状态 + 权限 +
允许的业务动作”这种与本项目完全一致的金标；动作金标仍需按本项目规则独立标注。

DataCLUE 还明确把高质量公开测试集与含噪训练集分开，这种做法值得沿用：外部训练语料可以用于发现
混淆和回归，不能把在同一批数据上拟合出来的结果当作验收结论。

## 下载与运行

从 [ECD 官方仓库](https://github.com/cooelf/DeepUtteranceAggregation) 的 README 下载公开压缩包，解压后运行：

```bash
PYTHONPATH=src .venv/bin/python scripts/run_customer_support_real_eval.py \
  --source "/path/to/E-commerce dataset/test.txt"
```

默认行为是对测试文件中的用户末轮去重后全部运行。ECD 测试文件虽然有 10,000 行，但当前文件只有
879 个不同的用户末轮；同一个上下文对应多个候选客服回复，不能重复计为 10,000 个独立工单案例。
快速检查可以加 `--limit 500`，但 500 只是子集，不是最终覆盖量。

从 [JDDC 基线仓库](https://github.com/SimonJYang/JDDC-Baseline-Seq2Seq) 获取获授权的脱敏 `data/chat.txt` 后，
运行 3C 客服全域扫描：

```bash
PYTHONPATH=src .venv/bin/python scripts/run_jddc_customer_support_eval.py \
  --source "/path/to/JDDC-Baseline-Seq2Seq/data/chat.txt"
```

脚本默认使用所有包含 3C 商品线索的会话，每个会话只选一条可行动用户消息，避免同一会话重复计权；
`--limit 500` 仅用于快速 smoke。

从 [DataCLUE 官方仓库](https://github.com/CLUEbenchmark/DataCLUE) 获取 CIC 后，优先使用
`datasets/raw_cic/test_public.json` 做高质量公开测试扫描：

```bash
PYTHONPATH=src .venv/bin/python scripts/run_dataclue_cic_support_scan.py \
  --source "/tmp/dataclue/datasets/raw_cic/test_public.json" \
  --json-out /tmp/dataclue-cic-scan.json
```

该脚本会保留 CIC 的 `label`/`label_des`，但不会把 118 个来源意图映射成项目动作；它只检查把消息
强制当作 `ticket` 建议时，动作闸门是否违反安全不变量。CIC 是单轮电商意图集，不应被当成 3C 多轮
动作准确率数据。

## 评测口径

### 重要：安全扫描的 0 不是准确率

这三份扫描脚本都属于 `deterministic_gate_invariant_scan`：它们不调用 LLM、不运行完整聊天 Agent、不
比较数据集原始标签，也不执行数据库写操作。脚本把每条消息强制作为 `intent_target=ticket` 的提议，
再检查确定性闸门是否放行了明显危险动作。因此 `violation_count=0` 只能说明“当前定义的几个闸门不变量
没有被这批输入触发”，不能说明意图识别正确、回复正确、工具调用正确或业务闭环成功。

由于闸门和扫描器都使用相同的确定性语义，安全扫描出现全 0 是可能且正常的；它更像单元/冒烟回归，
不是独立评测。真正的动作准确率必须运行完整 Agent，并与盲审后的 `human_gold_action` 比较。

ECD 的标签是检索候选回复是否匹配，不是本项目的 `CREATE_TICKET`、`SHOW_REFUND_PROGRESS` 等动作金标。
所以脚本报告的是安全不变量扫描，不宣称意图准确率：

- 普通咨询不能因为模型提出了 `ticket` 就直接创建工单；
- 已申请、已退款、退款未到账等进度表达不能被当作首次退款入口；
- 退款进度动作必须确实有进度语义；
- 创建工单必须有明确人工/异常/售后操作信号。

脚本输出动作分布和违规原话。出现违规时退出码为 1；修复策略后应重新运行全量 879 个去重案例。

JDDC 的扫描脚本同样会强制把每条候选消息当作“模型提议建单”来测试闸门，并按 3C 会话主题报告分布；
它验证的是“不该建单时能否拦住”和“设备故障是否进入排查分支”，不是在测试意图路由模型本身。

截至 2026-08-27，公开 JDDC 基线文件共解析出 10,945 个会话，其中 3,316 个会话含 3C 商品线索；
全量 3C 扫描结果为 2,790 条澄清、315 条退款自助引导、115 条设备排查、29 条退款进度、67 条升级，
安全违规 0 条。这个结果只能说明当前动作闸门未触发已定义的危险不变量，不能当作客服意图准确率；
后者仍需独立标注的项目动作金标。

首次接入 DataCLUE/CIC 公开测试集（2,000 条、112 个实际出现的标签）后，扫描结果为 1,842 条澄清、
106 条退款自助引导、10 条设备排查、19 条退款进度、23 条升级，安全违规 0 条。该结果同样只是
安全闸门回归，不代表 CIC 意图分类准确率。

脚本输出中的 `source_sessions`/`three_c_sessions`/`evaluated`（JDDC）和
`source_records`/`unique_queries`/`evaluated`（ECD）用于核对覆盖量；默认全量运行，不把
同一会话的重复消息或同一问题的候选回复重复计权。

## 与项目自有金标的关系

真实语料安全扫描不能替代项目业务金标。ECD/JDDC 的原始标签都不是本项目动作金标。下一步若要报告动作准确率，需要从该语料（或脱敏后的项目真实
会话）抽取样本，由独立标注者按当前业务规则另存 oracle 文件；输入文件不能携带 `expected_action`。
现有模板扩展数据只保留作冒烟测试，不用于发布或验收结论。

### 机器初标与盲审批次

JDDC 的 `waiter_send`、`is_transfer`、`is_repeat` 会保留在 `source_labels`，但它们不能直接回答
“本项目应该自助引导、澄清、显示进度还是建单”。其中 `is_transfer=1` 极度稀疏，不能当作项目动作金标。

可以先生成一份覆盖全部 3,316 个 3C 会话的机器初标批次：

```bash
PYTHONPATH=src .venv/bin/python scripts/build_jddc_provisional_annotations.py \
  --source "/path/to/JDDC-Baseline-Seq2Seq/data/chat.txt" \
  --output /tmp/jddc-3c-provisional.jsonl
```

每行同时包含原话、最多 8 条上下文、主题、`provisional_action`、规则原因和 `review_reasons`。
`human_gold_action` 固定为空，`human_review_status=pending`；人工复核时只填写独立的金标字段，
不要把 `provisional_action` 复制成金标。`high_risk_action`、多主题、信息不足和原始转人工信号会
自动进入复核队列。这样可以先批量覆盖真实口语，再对争议样本做盲审，避免“用同一套规则出题再用
同一套规则判分”。

本地盲审使用终端标注器。它只显示用户原话和上下文，隐藏机器动作和规则原因；每次提交立即原子写盘，
中断后用同一个 `--output` 继续：

```bash
PYTHONPATH=src .venv/bin/python scripts/review_jddc_annotations.py \
  --input /tmp/jddc-3c-provisional.jsonl \
  --output /tmp/jddc-3c-human-review.jsonl \
  --limit 300 --annotator local-reviewer
```

300 条不是最终全量金标，而是覆盖不同主题的第一轮人工代理样本。至少让 10%–20% 的样本由第二位
标注者独立复核，再处理分歧；只有 `human_review_status=reviewed` 且 `human_gold_action` 非空的
记录才可用于动作准确率。终端脚本模拟的是实际人工选择过程，不会把另一个模型的自评伪装成人工金标。

## 没有人工金标时的代理评测

在人工金标尚未建立前，可以用“盲评模型 + 确定性检查”推进，但报告名称必须是代理指标，而不是准确率：

1. **确定性检查（硬门槛）**：用 Python 检查是否越权读取、是否在缺少订单事实时写入、退款/支付金额是否被
   模型文本改写、工具失败后是否错误宣称成功。这些检查不需要模型裁判。
2. **独立模型裁判（软指标）**：裁判只看原始对话和 Agent 输出，不看 `provisional_action`、策略代码或
   参考答案；要求结构化输出 `pass/fail`、理由、置信度和 `abstain`。OpenAI 的
   [Graders](https://developers.openai.com/api/reference/resources/graders) 提供了 Label、Score、Python
   和组合 Grader 的同类做法；其 [Evals 构建指南](https://github.com/openai/evals/blob/main/docs/build-eval.md)
   也建议用少量人工 choice labels 做 model-graded eval 的 meta-eval 校准。
3. **双裁判与校准**：先在 100–300 条上让两个不同模型独立判断，只把一致且高置信度的样本作为代理集；
   分歧、低置信度和所有高风险写操作进入人工复核。每轮发布保留 10%–20% 的人工抽检，计算模型裁判与
   人工的一致率，而不是宣称模型标签是真实标签。
4. **分层报告**：至少分别报告安全违规率、工具调用精确率/召回率（仅在有人审的子集上）、任务完成率、
   正确澄清率、错误升级率和回复质量。单一“总分”会掩盖错误建单或错误退款这类高风险问题。

本项目已经做过一次盲测：独立 Agent 只看到 300 条 JDDC 原话和上下文，完全看不到机器初标和策略，
与当前规则动作仅一致 74/300（24.7%）。这不是谁“准确率低”的结论，而是说明动作定义和边界仍有
歧义；在建立少量人工仲裁集前，不能把任何一个模型的批量标签当作金标。

### 标注一致性报告

标注文件可以用下面的脚本按 `id` 对齐并输出混淆矩阵。脚本只报告
`proxy_agreement`，不会把规则初标或独立模型的意见自动升级成金标：

```bash
PYTHONPATH=src .venv/bin/python scripts/compare_support_annotations.py \
  --left /tmp/jddc-3c-provisional.jsonl \
  --right /tmp/jddc-3c-independent-agent-annotations.jsonl \
  --left-field provisional_action \
  --right-field human_gold_action \
  --left-name policy_v1_machine \
  --right-name independent_agent \
  --json-out /tmp/jddc-annotation-agreement.json
```

本次 300 条交集的代理一致率为 `74/300 = 24.7%`；其余 3,016 条只有机器初标，不能计入比较。
分歧原话会写进 JSON，供人工仲裁和后续边界修订。只有经过人工复核、且明确记录业务事实和允许动作的
样本，才可以用于发布门禁或动作准确率报告。

### 真实入口轨迹评测

当需要验证的不只是规则，而是意图路由、RAG/Agent、工具和 SSE 结果时，使用在线轨迹脚本。它请求
正在运行的 `/api/v1/chat/stream`，记录 `tool_call` 名称、结束/错误事件和从最终回复识别出的动作；
不会把完整回复、工具参数或身份信息写入结果。默认每条案例使用新会话，输入文件里的历史不重放，
这是单轮对照组，不是多轮准确率。

要验证真实的多轮上下文，显式使用 `--replay-history --owner-user-id <客户用户 ID>`。评测进程会把
输入的 `history` 写入独立的临时服务端 session，再只向公开聊天 API 发送 `session_id` 和最后一句
`query`；请求完成后删除该 session。公开 API 仍然不接受客户端直接传入 history。该模式只能对开发/测试
环境运行，且应使用独立的 `*_test` 数据库。

```bash
EVAL_AUTH_TOKEN='开发环境 JWT' \
PYTHONPATH=src .venv/bin/python scripts/run_customer_support_agent_trace_eval.py \
  --source /tmp/jddc-3c-independent-selection.jsonl \
  --expected-file /tmp/jddc-3c-independent-agent-annotations.jsonl \
  --base-url http://127.0.0.1:8000 \
  --expected-field human_gold_action \
  --interval-seconds 6.5 \
  --allow-side-effects \
  --json-out /tmp/customer-support-agent-trace.json
```

多轮 replay 示例：

```bash
EVAL_AUTH_TOKEN='开发环境 JWT' \
PYTHONPATH=src .venv/bin/python scripts/run_customer_support_agent_trace_eval.py \
  --source /tmp/jddc-3c-independent-selection.jsonl \
  --base-url http://127.0.0.1:8000 \
  --allow-side-effects --replay-history --owner-user-id 73 \
  --limit 30 --quiet --summary-only \
  --json-out /tmp/customer-support-agent-trace-replayed.json
```

对同一批 `id` 分别运行默认模式和 replay 模式，比较动作、工具名、错误和安全违规；不能把两次结果的
差异单独归因于上下文，除非固定模型版本/参数并控制请求时间、账号事实和副作用。评测输入按 chat
message 结构保存，目标轮之后的客服回复只作为参考答案，不能提前放入输入。这种“结构化对话输入 +
独立 choice labels/人工校准”的评测组织方式也符合公开 Evals 实践建议：
[OpenAI evals 构建指南](https://github.com/openai/evals/blob/main/docs/build-eval.md)。

`--allow-side-effects` 是有意设置的安全门槛：评测可能创建售后工单，只能对开发/测试环境运行。输出的
`proxy_agreement` 仍只是与调用方提供标签的一致性；`llm_calls` 在 HTTP 边界未知，会保持为 `null`。
脚本默认在请求之间等待 6.5 秒，以避开聊天接口每分钟 10 次的用户限流；429、网络错误和其他失败
不会计入 `comparable` 或 `proxy_agreement`。如果历史报告里出现大量 `RATE_LIMITED`，应先按此间隔
重跑，不能把失败请求当作动作结果。

一次 30 条在线试跑（2026-08-27）全部返回 200 并收到 `done` 事件，未触发限流；与独立模型标签的
代理一致率为 `11/30 = 36.7%`。这批输入的历史没有重放，且独立标签不是人工金标，因此该数字只说明
真实入口已经可观测，不能作为发布准确率，更不能作为多轮上下文结果。分歧主要是独立标注倾向直接 `CREATE_TICKET`，而当前策略在
首次退款/故障场景先给自助或排查引导；应进入人工仲裁，不要直接改成更激进的自动建单。

2026-08-28 又对同一批 30 条、每条至少含 4 条前文的 JDDC 案例做了真实入口配对试跑：replay 组和无
history 对照组均为 `30/30` 完成、传输错误 `0`、限流错误 `0`。两组观测动作都为
`CONTINUE=27`、`OFFER_REFUND_SELF_SERVICE=3`，逐条动作变化为 `0/30`。这只能说明在这批样本和
当前动作抽取口径下没有观察到上下文带来的动作差异；由于没有人工 `human_gold_action`，两组都不报告
准确率。临时放宽的运行时限流为普通/流式各 `100/分钟`，评测完成后已停止临时进程并恢复默认
`20/10`，评测 session 也已清理。

对应的无全文轨迹结果保存在评测机的 `/tmp/jddc-context-30-replayed.json` 和
`/tmp/jddc-context-30-no-history.json`；它们只保留动作、工具名、事件名、错误和耗时等结构化信息。
如果需要人工直接检查脱敏测试样本的实际回复，可额外传 `--include-answer`；默认仍不保存完整回复，
避免评测产物携带客户或模型全文。
如果只想做不写业务状态的回归，继续使用三份 `deterministic_gate_invariant_scan` 安全扫描。
