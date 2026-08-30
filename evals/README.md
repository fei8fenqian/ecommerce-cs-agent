# 评测数据说明

`customer_support_cases.jsonl` 与 `customer_support_oracle.jsonl` 是模板扩展的冒烟回归集，
只用于检查策略改动后是否破坏已知边界，不代表真实用户分布，也不能作为准确率或上线依据。

真实语料评测见 [`docs/evaluation/CUSTOMER_SUPPORT_REAL_DATA.md`](../docs/evaluation/CUSTOMER_SUPPORT_REAL_DATA.md)。
它从外部下载的 E-commerce Dialogue Corpus 测试文件读取数据，不把原始语料提交到仓库。

需要验证运行中 Agent 的真实路由、SSE 工具事件和状态副作用时，使用
`scripts/run_customer_support_agent_trace_eval.py`。该脚本必须显式允许开发/测试环境副作用，
不应指向生产；它输出的是 `online_agent_trace_eval` 和代理一致性，不是无人工校准的准确率。
如果原话与动作标签是两个 JSONL 文件，使用 `--expected-file` 按 `id` 对齐。
