# 3C 客服知识库重建计划

状态：首批演示文档已替换为保守版，明确了来源边界和不能承诺的事实；当前 manifest 的 19 个运行时
文档已完成一次安全增量同步（79 个 runtime chunks）。数据库中的非 manifest 旧素材仍保留，但不会参与检索。

## 为什么必须重建

`data/knowledge/*.md` 中混合了商品选购建议、厂商维修步骤、店铺售后承诺、支付方式和以旧换新规则，
但没有统一的来源 URL、适用品牌/型号、地区、有效期和审核记录。部分内容是生成式示例，不能用于承诺
保修、退款、费用、支付限额或维修结果；高风险问题答错时必须澄清或转人工。

## 来源优先级

1. **本店业务事实**：商品价格/库存、订单、支付、退款、物流和本店退换货规则，只能来自数据库及确定性服务，不能从 RAG 推断。
2. **官方厂商资料**：品牌支持门户、产品手册、保修政策、服务网点和安全公告；按品牌、产品线、型号和地区绑定。
3. **法律法规/标准**：用于解释法定底线，不替代本店或厂商的具体政策。
4. **社区、论坛、博客和模型生成内容**：只用于发现候选问题，不直接作为客户可见答案依据。

JDDC/ECD 是真实对话语料，适合评测口语和动作边界，不是售后政策知识库。

## 每篇可入库文档必须带的元数据

```yaml
source_type: official_vendor | regulation | internal_policy
source_url: https://...
publisher: 厂商或发布机构
retrieved_at: 2026-08-27
effective_from: 2026-01-01
review_due: 2026-09-27
region: CN-mainland
brand: Apple
models: [iPhone ...]
risk: low | medium | high
```

缺少来源、适用范围或有效期的文档不能进入高风险客服回答；政策和保修命中不精确时，Agent 只说明
需要核验的事实，不猜测资格、金额或维修结论。

## 首批重建范围

先覆盖手机、笔记本、平板、耳机/充电器等主要 3C 商品的：型号与兼容性、常见安全排查、官方保修入口、
退换货申请路径、订单/支付状态说明。每个品牌先选少量高频型号，验证来源和更新流程后再扩展。

文档文件更新后，手动、定时任务和未来 CI/CD 都执行同一个导入脚本，让运行时 RAG 读取新内容：

```bash
PYTHONPATH=src .venv/bin/python scripts/ingest/knowledge.py
```

普通导入是按 manifest source 同步的增量操作：新增 section 会插入，正文变化会更新原 stable chunk，
删除 section 只删除该 source 的 stale chunk，未变化内容不重新计算 embedding；manifest 外的 legacy source
默认保留在数据库中但不可检索。只有明确需要物理清理全部下线 source 时才使用 `--prune`，并先核对删除范围。
导入完成后再用一条支付、退款和设备故障问句做人工冒烟，确认检索结果不再出现旧的虚构政策。

## 可参考的官方入口

- Apple 服务与维修：https://support.apple.com/zh-cn/iphone/repair
- 华为服务与支持：https://consumer.huawei.com/cn/support/
- 小米服务中心：https://www.mi.com/service
- 联想服务政策：https://support.lenovo.com.cn/lenovo/WSI/htmls/policy_1260506616921.html
- 京东 3C 售后帮助：https://help.jd.com/user/issue/325-915.html
- 国家市场监督管理总局移动电话机三包规定：https://www.samr.gov.cn/cms_files/filemanager/samr/www/samrnew/samrgkml/nsjg/zlfzj/202007/W020211115556163548294.pdf

这些页面是核验入口，不表示可以无授权地整站复制或永久缓存内容；抓取前要遵守网站条款、版权和更新机制。
