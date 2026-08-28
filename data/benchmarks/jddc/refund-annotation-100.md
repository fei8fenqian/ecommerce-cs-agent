# JDDC 退款领域 Intent Gold 人工标注包

本文件是 **待人工标注数据**，不是 Gold。共 `100` 条，全部从 JDDC 基线公开脱敏
`data/chat.txt` 的不同会话中筛选；本文件没有使用当前 Router 的预测结果，也不把 JDDC
原始 `is_transfer`、`is_repeat`、`waiter_send` 当成本项目标签。

## 标注目标

本轮只标注 Intent / Router，不标注 required facts、capabilities、工具顺序、业务事实或最终客服回复。
请根据“可见上下文 + 当前用户”判断用户这一轮到底在做什么。

## 允许的 `speech_act`

只能填写以下 6 个值：

- `ACTION_REQUEST`：当前明确要求系统/客服做一件事，例如“我要退款”“取消退款”。
- `INFORMATION_QUERY`：当前询问状态、金额、去向、到账时间、资格、流程或原因。
- `STATEMENT`：只陈述已经发生的事实，没有新的问题或行动要求，例如“我已经申请退款了”。
- `ACKNOWLEDGEMENT`：结束、致谢、确认理解，例如“好的，谢谢”。
- `FUTURE_INTENTION`：将来打算做，但当前没有要求系统执行，例如“再等两天不行我就退款”。
- `CLARIFICATION_NEEDED`：当前意图不足以安全确定，例如“我不小心申请了退款”。

`STATEMENT`、`ACKNOWLEDGEMENT`、`FUTURE_INTENTION` 和 `CLARIFICATION_NEEDED` 的
`expected_requests` 必须为空；不要因为出现“退款”二字就制造退款请求。

## `expected_requests` 填写规则

1. 当前句语义明确时，以当前句为准；历史只用于补全“这个/那笔/什么时候”等指代，不能覆盖当前明确目标。
2. 普通单目标只填一个对象，格式为 `{"domain": "refund", "operation": "status"}`。
3. 多目标最多填 3 个，按客户目标的业务依赖顺序排列；不要把一个目标拆成多个工具调用。
4. `primary_goal` 填规范化的 `domain.operation`；没有业务请求时填 `null`。
5. 退款子域常用 operation：
   - `status`：退款是否成功、当前进度、退款了吗；
   - `expected_arrival`：什么时候到账、多久收到；
   - `destination`：退到哪里、原路退回哪个支付渠道；
   - `request`：明确现在要申请退款；
   - `cancel`：撤销/取消已经申请的退款；
   - `amount`：退款金额、少退、部分退款；
   - `eligibility`：当前订单是否符合退款资格；
   - `processing_time`：审核、受理或处理需要多久；
   - `anomaly`：失败、被拒、反复处理或状态异常；
   - `procedure`：如何申请、退款流程；
   - `clarify`：出现退款但当前没有明确目标；
   - `delivery_after_refund`：退款后是否还要签收/配送等交叉问题。
6. 跨域目标使用：退货/拒收/回仓后问退款用 `return.refund_dependency`；价保退款进度用
   `price_protection.refund_status`；不要为了一个细节创造新 operation。

## `expected_workflow` 规则

它不是简单拼接出来的字段，必须按当前源码 `resolve_workflow()` 的真实映射填写：

| canonical goal | 当前 Workflow |
|---|---|
| `refund.status` | `refund.refund_status` |
| `refund.amount` | `refund.refund_detail` |
| `refund.expected_arrival` | `refund.expected_arrival` |
| `refund.processing_time` | `refund.processing_time` |
| `refund.anomaly` | `refund.anomaly` |
| `refund.destination` | `refund.destination` |
| `refund.eligibility` | `refund.eligibility` |
| `refund.request` | `refund.request` |
| `refund.cancel` | `refund.cancel` |
| `return.refund_dependency` | `return.refund_dependency` |
| `price_protection.refund_status` | `price_protection.refund_status` |

当前没有注册 Workflow 的目标（例如 `refund.procedure`、`refund.clarify`）填 `null`，不能为了让
Benchmark 看起来完整而自造 Workflow。没有业务请求时同样填 `null`。

## `needs_clarification`

- 明确表达状态/问题/行动：`false`；即使后台以后缺工具，也不因此改成澄清。
- 当前无法知道用户想查什么或做什么：`true`，同时 `speech_act=CLARIFICATION_NEEDED`、
  `expected_requests=[]`、`primary_goal=null`、`expected_workflow=null`。
- 缺少订单号不自动等于需要澄清；如果目标明确，仍标目标，订单选择是执行阶段问题。

## 自检清单

- 是否把“已经申请退款”误标成 `refund.request`？如果只是陈述，应为 `STATEMENT` 且无 request。
- 是否把“再等两天不行就退款”误标成当前申请？应为 `FUTURE_INTENTION` 且无 request。
- 是否把“退款退到哪里”标成 `status`？应为 `destination`。
- 是否把“拒收后什么时候退款”只标成 `refund.expected_arrival`？应为 `return.refund_dependency`。
- 是否把 `primary_goal` 和 `expected_workflow` 强行写成同一个值？先查上面的真实 alias。

## 提交流程

1. 逐条填写每个 JSON 块中的 `TODO`。
2. 不改 case id、历史和当前用户原文。
3. 保留合法 JSON：字符串用双引号，空目标用 `null`，无请求用 `[]`。
4. 完成后把这份 Markdown 返回，用后续脚本转换为正式 Gold；在此之前不要拆 dev/holdout，避免边标边切分。

来源：<https://github.com/SimonJYang/JDDC-Baseline-Seq2Seq>（公开基线仓库中的脱敏 `data/chat.txt`）。

## 001. `jddc-refund-001-64de9927071ccfc12a7f6b727b0edead-2`

来源会话：`64de9927071ccfc12a7f6b727b0edead`；当前轮次：`2`

**可见上下文**

> **用户：** 你好
> **客服：** 请问您是咨询之前的问题还是有其他的问题需要处理呢?

**当前用户：** 我退款的钱怎么查看

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-001-64de9927071ccfc12a7f6b727b0edead-2",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 002. `jddc-refund-002-2097a4b044633a3569f3f32287a65724-26`

来源会话：`2097a4b044633a3569f3f32287a65724`；当前轮次：`26`

**可见上下文**

> **客服：** #E-s[数字x]
> **客服：** 请问还有其他还可以帮到您的吗?
> **用户：** 这不是你们自营的吗?
> **用户：** 怎么那么麻烦?
> **客服：** 亲爱的这个是自营的呢，但是是我们和厂家合作发货的，这样可以给客户更优惠的价格的呢
> **用户：** 从那里发货呢。今天能发吗?
> **客服：** 亲，可能需要时间呢
> **用户：** 那没办法了 我敢时间才买自营的

**当前用户：** 只能申请退款了

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-002-2097a4b044633a3569f3f32287a65724-26",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 003. `jddc-refund-003-acfed3c9716d5dc6c205deaa6e63eaf8-23`

来源会话：`acfed3c9716d5dc6c205deaa6e63eaf8`；当前轮次：`23`

**可见上下文**

> **客服：** 好的
> **客服：** 已经取消咯
> **用户：** 好的，多谢了
> **客服：** 您客气了，为您服务是小妹的荣幸呢#E-s[数字x]#E-s[数字x]#E-s[数字x]
> **客服：** 您的订单拦截成功，财务正在进行退款审核，请耐心等待哈
> **客服：** 预计[数字x]H之内审核退款哦
> **客服：** #E-s[数字x]亲爱哒，来都来了，用你发财的小手给我一个评价吧，谢谢!
> **客服：** 请问还有其他还可以帮到您的吗?

**当前用户：** 那我还需要点申请退款?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-003-acfed3c9716d5dc6c205deaa6e63eaf8-23",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 004. `jddc-refund-004-a5aa25e524dd51737d3c45e2c50c5864-1`

来源会话：`a5aa25e524dd51737d3c45e2c50c5864`；当前轮次：`1`

**可见上下文**

> **客服：** 有什么问题我可以帮您处理或解决呢?

**当前用户：** 上次不是已经说明已经退款了吗，为什么还会有电话打过来

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-004-a5aa25e524dd51737d3c45e2c50c5864-1",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 005. `jddc-refund-005-c3a0897d50800375d17ea41edb709d4e-15`

来源会话：`c3a0897d50800375d17ea41edb709d4e`；当前轮次：`15`

**可见上下文**

> **用户：** 订单编号:[数字x]里面有两件东西
> **客服：** 您好，您可以在我的订单详情页面申请退款或取消订单哦，取消后系统会进行全力拦截，拦截失败的话还请您拒收下就可以呢~
> **用户：** 好的，谢谢!
> **用户：** 或者到时我收到了再退货吧?
> **客服：** 您客气了哈
> **用户：** 我明天收到了，不打开，马上退货，可以吧?
> **客服：** 可以拒收的哈
> **用户：** 好的，那我就拒收吧

**当前用户：** 拒收之后会退款的吗?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-005-c3a0897d50800375d17ea41edb709d4e-15",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 006. `jddc-refund-006-0060d47039b78c133431ce13a449aa4a-3`

来源会话：`0060d47039b78c133431ce13a449aa4a`；当前轮次：`3`

**可见上下文**

> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 你好
> **客服：** 有什么问题我可以帮您处理或解决呢?

**当前用户：** 我昨天手套申请的退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-006-0060d47039b78c133431ce13a449aa4a-3",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 007. `jddc-refund-007-d9ee36790bbafc9c62c89e2009418fc8-4`

来源会话：`d9ee36790bbafc9c62c89e2009418fc8`；当前轮次：`4`

**可见上下文**

> **用户：** 你好
> **用户：** 咨询订单号:[数字x] 订单金额:[金额x] 下单时间:[日期x]
> **客服：** 有什么问题我可以帮您处理或解决呢?
> **客服：** 亲爱的，妹子在的哦，您看这边有什么可以帮到您的吗#E-s[数字x]

**当前用户：** 我不小心申请了退款。

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-007-d9ee36790bbafc9c62c89e2009418fc8-4",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 008. `jddc-refund-008-9685bef4c1b1eccb69d658c27e7cdc4f-10`

来源会话：`9685bef4c1b1eccb69d658c27e7cdc4f`；当前轮次：`10`

**可见上下文**

> **客服：** 还请您稍等，马上为您查询~
> **客服：** 需要调货的呢
> **用户：** 那大概是什么时候呢
> **客服：** 预计需要[数字x]-[数字x]周的呢
> **用户：** [数字x]周不都是年后了么
> **客服：** 一般周期是这样的呢
> **客服：** 耽误您使用了ne
> **客服：** 抱歉哦

**当前用户：** 那我可以申请退款 再买个别的产品吗?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-008-9685bef4c1b1eccb69d658c27e7cdc4f-10",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 009. `jddc-refund-009-6a483eb18144f029bf7fe2629eac6a89-14`

来源会话：`6a483eb18144f029bf7fe2629eac6a89`；当前轮次：`14`

**可见上下文**

> **用户：** 我需要确定的回答
> **用户：** ?
> **用户：** ?
> **客服：** 小妹这边确实不能给您一个准确的时间的哦。 要不然我就是在忽悠您了，不过您放心，配送部门会积极给您配送的，还请您在耐心等待一下，好吗?
> **用户：** 算了，给我退款吧
> **用户：** 我明天就放假了，收不了货了
> **客服：** #E-s[数字x]
> **用户：** [日期x] [时间x] 订单编号: [ORDERID_10004171] 订单金额: ￥[金额x]

**当前用户：** 我这边已经申请退款了，你们确认退款吧

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-009-6a483eb18144f029bf7fe2629eac6a89-14",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 010. `jddc-refund-010-77ae4c189382ad679e25bd4487cca298-11`

来源会话：`77ae4c189382ad679e25bd4487cca298`；当前轮次：`11`

**可见上下文**

> **用户：** 但现在再买的话又要多付运费了
> **用户：** 我能退款再一起重买么
> **客服：** 马上核实情况，请您稍等哈#E-s[数字x]
> **客服：** 取消订单重新购买
> **用户：** 但我看好像我的订单被分成了两份
> **用户：** 我两份都取消么
> **客服：** 是的哦
> **用户：** 有一个好像已经捡货了

**当前用户：** 能申请退款么

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-010-77ae4c189382ad679e25bd4487cca298-11",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 011. `jddc-refund-011-40d96fb7e90336e1495ac4e6eaa2d135-21`

来源会话：`40d96fb7e90336e1495ac4e6eaa2d135`；当前轮次：`21`

**可见上下文**

> **客服：** 返回库房周期[数字x]天左右的呢
> **客服：** 已经通知他们处理了呢
> **客服：** 退款是原路返回的
> **客服：** 辛苦注意查
> **客服：** 妹子帮您跟进下
> **客服：** 辛苦了哈
> **客服：** 请问还有其他还可以帮到您的吗?
> **用户：** 尽快退给我钱啊  耽误我买其他产品

**当前用户：** 之前都取消成功了 要退款了 怎么又重新来一次了

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-011-40d96fb7e90336e1495ac4e6eaa2d135-21",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 012. `jddc-refund-012-d8965006d4fbc8a5304fd0fa631a0c3e-12`

来源会话：`d8965006d4fbc8a5304fd0fa631a0c3e`；当前轮次：`12`

**可见上下文**

> **用户：** 您好
> **客服：** 有什么问题我可以帮您处理或解决呢?
> **客服：** 亲爱的，妹子在的哦，您看这边有什么可以帮到您的吗#E-s[数字x]
> **用户：** 我问一下 商品有问题退货的话 邮费返还么
> **用户：** 比如说车载手机支架 比较大 卡不住我的iPhone se 这类的退货邮费也退么
> **用户：** 还是说只退商品的钱
> **客服：** 不返回哦亲
> **用户：** 只退购买商品的钱 邮费是不退的是吧

**当前用户：** 我用微信支付的 退款也是退到微信上是么

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-012-d8965006d4fbc8a5304fd0fa631a0c3e-12",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 013. `jddc-refund-013-44677444e48cff9d944472b7d0a55008-13`

来源会话：`44677444e48cff9d944472b7d0a55008`；当前轮次：`13`

**可见上下文**

> **客服：** 自动发货更新的哦
> **用户：** 为什么要这么久?
> **用户：** 我是看到[数字x]月[数字x]能到货我才买的
> **用户：** 不然我就去实体店买啦
> **客服：** 因为您下单之后，系统会根据您的地址匹配您当地最近的仓库给您发货的呢，亲爱的#E-s[数字x]#E-s[数字x]
> **客服：** 这个是上海[地址x]，亲爱的
> **用户：** 晕
> **客服：** 您看时间合适吗，如果是不合适，建议您也可以取消的哈#E-s[数字x]，因为这个是最快的时间哟，亲爱的#E-s[数字x]

**当前用户：** 行吧，我问问，等不及就申请退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-013-44677444e48cff9d944472b7d0a55008-13",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 014. `jddc-refund-014-6b31c44380b0dc795e1e0fb26b221128-2`

来源会话：`6b31c44380b0dc795e1e0fb26b221128`；当前轮次：`2`

**可见上下文**

> **用户：** [订单编号:[ORDERID_10000676]，订单金额:[金额x]，下单时间:[日期x] [时间x]]
> **客服：** 有什么问题我可以帮您处理或解决呢?

**当前用户：** 拒收后，什么时候退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-014-6b31c44380b0dc795e1e0fb26b221128-2",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 015. `jddc-refund-015-82eb536529ff68e37041488fe67bf0bf-9`

来源会话：`82eb536529ff68e37041488fe67bf0bf`；当前轮次：`9`

**可见上下文**

> **用户：** 你好
> **客服：** 您是遇到了什么问题呢?#E-s[数字x]
> **用户：** 我的订单现在已经好久了，
> **用户：** 那边也不说话，电话也不接，什么意思
> **用户：** 京东现在已经是这样子，为客户服务的吗?
> **客服：** 这边建议您咨询一下供应商呢
> **用户：** 我问供应商，他不理我，这是你们的事情，把我订单交给了供应商
> **客服：** 您这个订单是由厂商帮您发货的呢

**当前用户：** 退款也退不了，货也不发

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-015-82eb536529ff68e37041488fe67bf0bf-9",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 016. `jddc-refund-016-4fdccf79dcaf66b42d8ccdc501f3b7c3-0`

来源会话：`4fdccf79dcaf66b42d8ccdc501f3b7c3`；当前轮次：`0`

**当前用户：** 可以退款然后从拍么你

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-016-4fdccf79dcaf66b42d8ccdc501f3b7c3-0",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 017. `jddc-refund-017-21d8f4c0633b929f545fd0f9545ff478-8`

来源会话：`21d8f4c0633b929f545fd0f9545ff478`；当前轮次：`8`

**可见上下文**

> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 我的订单可以退款吗
> **客服：** 好的，请您稍等，这边咨询不是一对一，同时需要回复多个消息，我会尽快回复您。#E-s[数字x]
> **客服：** 您好，您可以在我的订单详情页面申请退款或取消订单哦，取消后系统会进行全力拦截，拦截失败的话还请您拒收下就可以呢~
> **用户：** 我买了两个，想把贵的退掉
> **用户：** 我点击退款了，可以不退吗
> **用户：** ???
> **客服：** 您好，非常抱歉，订单一旦提交取消申请，无法撤销恢复，辛苦您关注下取消进度，如我们取消未拦截成功，会给您送货的，如取消拦截成功，订单将会取消，辛苦您重新订购哦，感谢您的理解，谢谢!

**当前用户：** 退款要多久把钱退回来

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-017-21d8f4c0633b929f545fd0f9545ff478-8",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 018. `jddc-refund-018-184dfc73dea31ab9b5d6b0cb5df1014e-13`

来源会话：`184dfc73dea31ab9b5d6b0cb5df1014e`；当前轮次：`13`

**可见上下文**

> **用户：** 没有申请退款选项
> **客服：** 取消了您需要就继续购买
> **用户：** 我知道
> **用户：** 怎么取消不了呢??
> **客服：** 嗯
> **用户：** 为啥
> **用户：** 已经跳转到订单列表页面
> **客服：** 嗯

**当前用户：** 我都说啦，没有申请退款选项

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-018-184dfc73dea31ab9b5d6b0cb5df1014e-13",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 019. `jddc-refund-019-ed9970cacc8962dfa46e6b54806c2622-5`

来源会话：`ed9970cacc8962dfa46e6b54806c2622`；当前轮次：`5`

**可见上下文**

> **用户：** [订单编号:[数字x]，订单金额:[金额x]，下单时间:[日期x] [时间x]]
> **客服：** 有什么问题我可以帮您处理或解决呢?
> **客服：** 还辛苦您再等待下哦，这里正在为您查询中哈!
> **用户：** 你好我刚买了一个电饭煲
> **客服：** 米家(MIJIA)小米智能电饭煲 米家IH电饭煲 电磁环绕加热 [数字x]L容量 PFA粉体涂层 [商品快照]

**当前用户：** 但是地址写错了所以申请了退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-019-ed9970cacc8962dfa46e6b54806c2622-5",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 020. `jddc-refund-020-90358b0a4c6085c2caa0c4fcea4aed2b-21`

来源会话：`90358b0a4c6085c2caa0c4fcea4aed2b`；当前轮次：`21`

**可见上下文**

> **用户：** 懂?
> **客服：** 亲，跨省不支持呢
> **用户：** 不跨省的啊
> **用户：** 本省的
> **用户：** ?
> **用户：** 快说，别老是欢迎了
> **客服：** 不支持哦
> **用户：** 那算了

**当前用户：** 拒收后就是自动退款了是吗?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-020-90358b0a4c6085c2caa0c4fcea4aed2b-21",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 021. `jddc-refund-021-0242c46f210bfffc495b2997347a9006-14`

来源会话：`0242c46f210bfffc495b2997347a9006`；当前轮次：`14`

**可见上下文**

> **用户：** 第三方商铺没人理我
> **用户：** 能帮忙处理下么
> **客服：** 我们这边有一个处理流程哦 如果您联系不上卖家或者联系卖家不为您解决好，您可以申请一下交易纠纷 京东帮您解决的
> **用户：** 怎么选择
> **客服：** 亲，交易纠纷单申请路径:电脑端:我的订单-客户服务-交易纠纷;手机上，在京东APP:右下角“我的”—客户服务—交易纠纷)您提交之后， 商家会在[数字x]小时内回复您处理结果，若商家超时未受理，会在[数字x]小时自动流转至京东，或您对商家处理结果不满意，您也在商家回复之后，点击申请京东介入，纠纷专员同样[数字x]小时内处理.感谢您的理解与支持!
> **用户：** 好的
> **客服：** [姓名x]
> **客服：** 您可以在[站点x]-[站点x]—[站点x]里面提交申请或者查看纠纷记录回复商家哦，提交了仲裁单后，商家会在[数字x]H回复给您的呢

**当前用户：** 这个订单为什么不能申请退款呢

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-021-0242c46f210bfffc495b2997347a9006-14",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 022. `jddc-refund-022-022699b2ae1b620de219463736340e01-3`

来源会话：`022699b2ae1b620de219463736340e01`；当前轮次：`3`

**可见上下文**

> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 你好。我刚才那个取消的订单
> **客服：** 小妹查看下哦，请稍等#E-s[数字x]~

**当前用户：** 退款什么时候到

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-022-022699b2ae1b620de219463736340e01-3",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 023. `jddc-refund-023-07ca8a5890bb54a2ed19497e34bcf604-0`

来源会话：`07ca8a5890bb54a2ed19497e34bcf604`；当前轮次：`0`

**当前用户：** 我今天买了，地址是公司然后他没送到，我就退了，从买了，地址是家里了，所以我怕他把之前那个送到公司了，我怕那个快递，明天那个快递小哥哥给送去了，好几个小时了，那个退款还没反应，所以很着急

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-023-07ca8a5890bb54a2ed19497e34bcf604-0",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 024. `jddc-refund-024-91b6ac4e4dc87aebf8de51ccb6d4d069-5`

来源会话：`91b6ac4e4dc87aebf8de51ccb6d4d069`；当前轮次：`5`

**可见上下文**

> **用户：** 你好，我现在这个订单我想申请退款重拍
> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 里面有一个灯我仔细看了一下不喜欢，想退款重拍
> **用户：** ???
> **客服：** 您已经提交了取消订单了

**当前用户：** 我已经申请退款了，但订单已经发货，这个怎么处理

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-024-91b6ac4e4dc87aebf8de51ccb6d4d069-5",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 025. `jddc-refund-025-a90af000233f400fa909f866b0bd0679-0`

来源会话：`a90af000233f400fa909f866b0bd0679`；当前轮次：`0`

**当前用户：** 退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-025-a90af000233f400fa909f866b0bd0679-0",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 026. `jddc-refund-026-0282d9d37b431314f36721a39f9eb26d-22`

来源会话：`0282d9d37b431314f36721a39f9eb26d`；当前轮次：`22`

**可见上下文**

> **用户：** 我下单后怎么办
> **用户：** 给我发两套衣服?
> **客服：** 亲，这件不是说配送不送#E-s[数字x]
> **用户：** 嗯你好是这样子啊就是说这套衣服呢我原本以为是说到达不了因为他两天之前的话都没更新还在那个嗯房东那边说发完那个会懂那一点然后我以为大不了然后昨天我昨天晚上的左右
> **客服：** 建议您和配送师傅协商下
> **客服：** 是否可以配送
> **客服：** 不可以配送查看价格重新下单
> **用户：** 我刚才和京东小哥说了一下，我正常签收物品

**当前用户：** 那你们能不能系统弄一下，客户已经正常签收不退款了呢

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-026-0282d9d37b431314f36721a39f9eb26d-22",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 027. `jddc-refund-027-1df8178e2bfb22e34ddc4e403bac8660-6`

来源会话：`1df8178e2bfb22e34ddc4e403bac8660`；当前轮次：`6`

**可见上下文**

> **用户：** 那什么时候送
> **客服：** 请问您是咨询之前的问题还是有其他的问题需要处理呢?
> **用户：** 哪什么时候送
> **客服：** 这边同时沟通的人太多啦#E-s[数字x] 您耐心等待下，妹子稍后回复您好不好#E-s[数字x]
> **用户：** 没办法
> **用户：** 不能超过二号

**当前用户：** 如果超过了我就要退钱

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-027-1df8178e2bfb22e34ddc4e403bac8660-6",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 028. `jddc-refund-028-3ccb012291a7cbc9b687b506e7c22046-0`

来源会话：`3ccb012291a7cbc9b687b506e7c22046`；当前轮次：`0`

**当前用户：** 直接拒收退货，什么时候退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-028-3ccb012291a7cbc9b687b506e7c22046-0",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 029. `jddc-refund-029-a44ad8ef4bd448390901c13790174957-0`

来源会话：`a44ad8ef4bd448390901c13790174957`；当前轮次：`0`

**当前用户：** 你好，东西拒收商家已经收到，我怎么申请退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-029-a44ad8ef4bd448390901c13790174957-0",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 030. `jddc-refund-030-f2d070017199a568323d7bffcd11f5c5-10`

来源会话：`f2d070017199a568323d7bffcd11f5c5`；当前轮次：`10`

**可见上下文**

> **客服：** 小妹为您看看，您稍等哦#E-s[数字x]
> **用户：** 查到了吗
> **客服：** 亲爱哒，非常抱歉#E-s[数字x]，让您久等了#E-s[数字x]
> **客服：** 查询您是申请了价格保护服务了呢
> **客服：** [金额x]
> **客服：** #E-s[数字x]
> **客服：** 亲爱哒 ~
> **客服：** 请问还有其他还可以帮到您的吗?

**当前用户：** 是退款[金额x]吗

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-030-f2d070017199a568323d7bffcd11f5c5-10",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 031. `jddc-refund-031-9d6bf21e6110c4cc10e25a6c5d7b8673-2`

来源会话：`9d6bf21e6110c4cc10e25a6c5d7b8673`；当前轮次：`2`

**可见上下文**

> **用户：** 你好
> **客服：** 有什么问题我可以帮您处理或解决呢?

**当前用户：** 刚点错了，麻烦把我取消退款申请

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-031-9d6bf21e6110c4cc10e25a6c5d7b8673-2",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 032. `jddc-refund-032-61d7ca64ee912beebcceaa2854e14ec1-6`

来源会话：`61d7ca64ee912beebcceaa2854e14ec1`；当前轮次：`6`

**可见上下文**

> **用户：** 顾客通过点击web咚咚[站点x]信息发送:[订单编号:[ORDERID_10005703]，订单金额:[金额x]，下单时间:[日期x] [时间x]]
> **用户：** [订单编号:[ORDERID_10005703]，订单金额:[金额x]，下单时间:[日期x] [时间x]]
> **用户：** 顾客通过点击web咚咚[站点x]信息发送:[订单编号:[ORDERID_10005703]，订单金额:[金额x]，下单时间:[日期x] [时间x]]
> **客服：** 有什么问题我可以帮您处理或解决呢?#E-s[数字x]
> **用户：** 你好
> **客服：** #E-s[数字x]

**当前用户：** 我发现我这个灯买小了我想退货退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-032-61d7ca64ee912beebcceaa2854e14ec1-6",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 033. `jddc-refund-033-57b8d1eaf6ba12a713bfae1428259cb1-21`

来源会话：`57b8d1eaf6ba12a713bfae1428259cb1`；当前轮次：`21`

**可见上下文**

> **客服：** 白条恢复差价额度
> **用户：** 是要全部还款结束才返差价吗
> **客服：** 若白条未还款  白条恢复差价额度
> **用户：** 什么意思
> **用户：** 什么叫白条恢复差价额度
> **客服：** 您白条有全部还款么
> **用户：** 还没有
> **客服：** 直接退白条

**当前用户：** 在京东白条里面退款?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-033-57b8d1eaf6ba12a713bfae1428259cb1-21",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 034. `jddc-refund-034-528e3ea32afe221f5ab98de00c38b54b-8`

来源会话：`528e3ea32afe221f5ab98de00c38b54b`；当前轮次：`8`

**可见上下文**

> **用户：** 你好
> **客服：** 有什么问题我可以帮您处理或解决呢?#E-s[数字x]#E-s[数字x]#E-s[数字x]
> **客服：** 您好
> **用户：** 咨询订单号:[ORDERID_10004406] 订单金额:[金额x] 下单时间:[日期x]
> **客服：** 您方便简单描述下您的问题吗?
> **用户：** 这个订单[数字x]天了
> **客服：** 还请您稍等，喝点水#E-s[数字x]，稍作休息。这里正在为您查询中哈!#E-s[数字x]#E-s[数字x]
> **用户：** 快递没有任何动静

**当前用户：** 我想申请退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-034-528e3ea32afe221f5ab98de00c38b54b-8",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 035. `jddc-refund-035-8b20a26632acf30176beaad86dfabcb7-23`

来源会话：`8b20a26632acf30176beaad86dfabcb7`；当前轮次：`23`

**可见上下文**

> **客服：** 为了提高您在京东商城的购物体验，特由京东为您提供“退换无忧服务”。如您购买京东自营商品且购买“退换无忧服务”后，在您签收商品后的[数字x]天内产生退换货，需您自行承担运费的情况下，可享[数字x]次上门取件服务。若您申请上门取件的地址超出了京东上门取件的范围，需自行委托第三方配送，在您提供有效快递单号和费用凭证，并经京东审核通过后，您支付的运费将以余额的形式按照《非客户原因退换货逆向运费补偿标准》(http://help.jd.com/user/issue/[数字x]3[数字x]-[链接x])返还您相应的费用。
> **客服：** 您在京东商城购买京东自营商品后，当您在结算页提交订单时，如果在结算页左侧显示“退换无忧”服务，则表示您所购买的商品支持此服务(目前只支持自营中小件商品，暂不支持自营大件商品)。如果您确认需要购买， 选择“退换无忧”后面的勾选框，此服务费会在结算明细中体现，并计入应付总额。
> **用户：** 无理由的话，这个服务也支持吗?就相当于运费险了?
> **用户：** 现在的话，能不能追加买一个?
> **客服：** 是的哦，只可以下单时候一起拍的
> **用户：** 哦哦
> **客服：** 下单之后不可以加的哦#E-s[数字x]
> **客服：** 从您签收商品的时间开始，退换无忧服务在[数字x]天内有效([数字x]天内退货[数字x]天内换货)，并且需要您在[数字x]天内在线提交相关服务申请([数字x]天内申请退货[数字x]天内申请换货)，，如果超过[数字x]天未提交相关服务申请，退换无忧服务失效。

**当前用户：** 如果要是申请退款这个订单用了的优惠券，重新买还能用吗

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-035-8b20a26632acf30176beaad86dfabcb7-23",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 036. `jddc-refund-036-9ff211391bd6ea8baf90704038dea5d2-14`

来源会话：`9ff211391bd6ea8baf90704038dea5d2`；当前轮次：`14`

**可见上下文**

> **客服：** [ORDERID_10000525]
> **客服：** 这个订单吗
> **用户：** 是不是忘了
> **用户：** 是，今天
> **用户：** 客服电话?
> **客服：** 退款完成的
> **用户：** 沒抵充 是不是钱沒到一样、
> **客服：** 白条还需要还款吗

**当前用户：** 我[数字x]月退款  是不是分期抵充

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-036-9ff211391bd6ea8baf90704038dea5d2-14",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 037. `jddc-refund-037-e39502010e3e9e7ac5bb39f641ebaa16-15`

来源会话：`e39502010e3e9e7ac5bb39f641ebaa16`；当前轮次：`15`

**可见上下文**

> **客服：** 是可以的呢
> **客服：** 直接申请退款
> **客服：** 就卡
> **用户：** 可以重新发一个吗?
> **用户：** #E-s[数字x]
> **用户：** #E-s[数字x]
> **客服：** 建议您重新下单购买
> **客服：** 这边给您取消订单退款的哈

**当前用户：** 我不退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-037-e39502010e3e9e7ac5bb39f641ebaa16-15",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 038. `jddc-refund-038-4b3298b1e0e6896eae75a1f3b81675f6-2`

来源会话：`4b3298b1e0e6896eae75a1f3b81675f6`；当前轮次：`2`

**可见上下文**

> **用户：** 咨询订单号:[ORDERID_10005086] 订单金额:[金额x] 下单时间:[日期x]
> **客服：** 您好，请问有什么可以帮助您的么#E-s[数字x]

**当前用户：** 你好，我这个包申请了退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-038-4b3298b1e0e6896eae75a1f3b81675f6-2",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 039. `jddc-refund-039-fdada1b79e97b69b447c5d156a2d8975-15`

来源会话：`fdada1b79e97b69b447c5d156a2d8975`；当前轮次：`15`

**可见上下文**

> **用户：** ……
> **用户：** ……
> **用户：** 我这个体脂称是返钱的不
> **客服：** PHICOMM斐讯智能体脂秤S[数字x] 产品升级 隐藏式LED显示 [数字x]电极测全身 [数字x]项身体数据 [商品快照]
> **客服：** 为了更好的解决您的问题，需要核实一下商品的信息奥，请问是这个商品吗?
> **用户：** 是
> **客服：** 显示需要调货的呢
> **客服：** 一般是一周内到货发货的哦

**当前用户：** 我想问的是  这个体脂称是软件返钱的不

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-039-fdada1b79e97b69b447c5d156a2d8975-15",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 040. `jddc-refund-040-f77876d0c14d486d017c3b43cde7fd1c-17`

来源会话：`f77876d0c14d486d017c3b43cde7fd1c`；当前轮次：`17`

**可见上下文**

> **客服：** 感谢您的谅解，我们还在完善中，一切会好起来的，非常感谢您的支持和谅解哦~
> **客服：** 别忘了对妹纸的服务做出评价哦 谢谢您啦  么么哒#E-s[数字x]
> **客服：** #E-s[数字x]#E-s[数字x]
> **客服：** #E-s[数字x]#E-s[数字x]
> **客服：** 哈喽~好巧，又见面了呢#E-s[数字x]请问您是咨询之前的问题还是有其他的问题呢?
> **客服：** #E-s[数字x]#E-s[数字x]#E-s[数字x]
> **用户：** 问下怎么退货
> **客服：** 申请退款就可以的亲爱的

**当前用户：** 点什么地方进行退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-040-f77876d0c14d486d017c3b43cde7fd1c-17",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 041. `jddc-refund-041-dad569c5b41f1cff1dd0c27f3e4223a9-5`

来源会话：`dad569c5b41f1cff1dd0c27f3e4223a9`；当前轮次：`5`

**可见上下文**

> **用户：** 在吗
> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 我的订单颜色选错了
> **用户：** 怎么修改
> **客服：** 您好，订单一旦提交之后所有的信息都是无法修改的呢，非常抱歉#E-s[数字x]

**当前用户：** 那我只能申请退款，重拍?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-041-dad569c5b41f1cff1dd0c27f3e4223a9-5",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 042. `jddc-refund-042-1313a59fd1595cf9bee0a10212c33018-15`

来源会话：`1313a59fd1595cf9bee0a10212c33018`；当前轮次：`15`

**可见上下文**

> **客服：** 是可以取消重新下单的哦·
> **客服：** [ORDERID_10000486]
> **客服：** 在订单中是可以申请的哈~~
> **用户：** 怎么申请退货啊退货以后再下单
> **客服：** 是可以申请的哈
> **客服：** 这里可以为您申请的哦~
> **客服：** 但是是需要审核的哈
> **客服：** 您也可以在订单中申请退款的哈

**当前用户：** 好那你帮我申请退款了然后再下单了

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-042-1313a59fd1595cf9bee0a10212c33018-15",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 043. `jddc-refund-043-98d15e46236e4dc81d90185f13dcbc07-2`

来源会话：`98d15e46236e4dc81d90185f13dcbc07`；当前轮次：`2`

**可见上下文**

> **用户：** 你好
> **客服：** 请问您是咨询之前的问题还是有其他的问题需要处理呢?

**当前用户：** 刚才有个退款的

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-043-98d15e46236e4dc81d90185f13dcbc07-2",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 044. `jddc-refund-044-3eee073a5d3eb06d1c991ff1579c48e3-13`

来源会话：`3eee073a5d3eb06d1c991ff1579c48e3`；当前轮次：`13`

**可见上下文**

> **用户：** 然后点退货了
> **用户：** 因为地址填错了
> **客服：** [姓名x]
> **客服：** 看到了
> **客服：** 配送退货
> **用户：** 现在想要可以再进行配送吗
> **客服：** 不好意思不可以了哈
> **用户：** 好吧#E-s[数字x]

**当前用户：** 那那个退货退款什么可以返还呢

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-044-3eee073a5d3eb06d1c991ff1579c48e3-13",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 045. `jddc-refund-045-5c16e2f699210882c0de424b9314cb60-1`

来源会话：`5c16e2f699210882c0de424b9314cb60`；当前轮次：`1`

**可见上下文**

> **客服：** 有什么问题我可以帮您处理或解决呢?

**当前用户：** 昨晚申请的退款，没有入账。刚刚又申请了一笔退款马上就入了账。

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-045-5c16e2f699210882c0de424b9314cb60-1",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 046. `jddc-refund-046-9b900aecadfe0d638b54de134d7be565-3`

来源会话：`9b900aecadfe0d638b54de134d7be565`；当前轮次：`3`

**可见上下文**

> **用户：** 你好
> **客服：** 有什么问题我可以帮您处理或解决呢?
> **客服：** #E-s[数字x]

**当前用户：** 我有个订单不能申请退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-046-9b900aecadfe0d638b54de134d7be565-3",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 047. `jddc-refund-047-d593969f8657d93b0f92c25b5e7f6a32-5`

来源会话：`d593969f8657d93b0f92c25b5e7f6a32`；当前轮次：`5`

**可见上下文**

> **用户：** https://item.jd.com/1508581.html
> **用户：** 你好
> **客服：** 您好
> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 这个厂家联系我说没有货了

**当前用户：** 退款怎么退

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-047-d593969f8657d93b0f92c25b5e7f6a32-5",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 048. `jddc-refund-048-e764eec590adff38291a6a6dd0780513-7`

来源会话：`e764eec590adff38291a6a6dd0780513`；当前轮次：`7`

**可见上下文**

> **用户：** 你好
> **用户：** 请问我买的这款是没有货还是怎么回事啊
> **用户：** 大概什么时候能到货
> **客服：** 亲爱的，妹子马上为您核实情况，请您稍等哈#E-s[数字x]#E-s[数字x]
> **客服：** 有什么问题我可以帮您处理或解决呢?#E-s[数字x]#E-s[数字x]#E-s[数字x]
> **用户：** 什么时候能送到
> **客服：** 这个目前是缺货的状态哦 补货预计一周哈 这个只是一个大概的时间哦一旦补货会尽快给您发货的哈

**当前用户：** 那我退款吧

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-048-e764eec590adff38291a6a6dd0780513-7",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 049. `jddc-refund-049-469dfd25ac0b23115e55da863e932f7b-2`

来源会话：`469dfd25ac0b23115e55da863e932f7b`；当前轮次：`2`

**可见上下文**

> **客服：** 您好，请问有什么可以帮您?#E-s[数字x]
> **用户：** 我的那个订单已经退回了

**当前用户：** 什么时候可以退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-049-469dfd25ac0b23115e55da863e932f7b-2",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 050. `jddc-refund-050-a48bfadb049be451f3b755ec082cac43-13`

来源会话：`a48bfadb049be451f3b755ec082cac43`；当前轮次：`13`

**可见上下文**

> **用户：** 记录仪已经退款了
> **用户：** 安装的单子用不上了，也退款
> **客服：** 我这边申请售后退
> **用户：** 好，谢谢
> **客服：** 已经提交了售后
> **用户：** 我要怎样做，等待吗?
> **用户：** 还是如何
> **客服：** 您等待售后处理退款就行的

**当前用户：** 好的，我之前也申请退款了，第三方商家给我关闭了

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-050-a48bfadb049be451f3b755ec082cac43-13",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 051. `jddc-refund-051-333fdd391180fb45a71ef18d395b8b79-2`

来源会话：`333fdd391180fb45a71ef18d395b8b79`；当前轮次：`2`

**可见上下文**

> **用户：** [订单编号:[ORDERID_10003761]，订单金额:[金额x]，下单时间:[日期x] [时间x]]
> **客服：** 有什么问题我可以帮您处理或解决呢?

**当前用户：** 你好，我的退款订单怎么还没审核退款好呢

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-051-333fdd391180fb45a71ef18d395b8b79-2",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 052. `jddc-refund-052-b398a259e88f960b46b89897c2662599-3`

来源会话：`b398a259e88f960b46b89897c2662599`；当前轮次：`3`

**可见上下文**

> **用户：** https://item.jd.com/4405621.html
> **用户：** 你好
> **客服：** 请问您是咨询之前的问题还是有其他的问题需要处理呢?

**当前用户：** 我刚才申请了退货退款，选择了自己发货，怎么变成上门取件了?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-052-b398a259e88f960b46b89897c2662599-3",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 053. `jddc-refund-053-d4e149a321e8c02e9d06e5c0bc08ef19-2`

来源会话：`d4e149a321e8c02e9d06e5c0bc08ef19`；当前轮次：`2`

**可见上下文**

> **客服：** 请问您是咨询之前的问题还是有其他的问题需要处理呢?
> **用户：** 之前的问题

**当前用户：** 我是不退货了，怎么取消退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-053-d4e149a321e8c02e9d06e5c0bc08ef19-2",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 054. `jddc-refund-054-95cd7699eeb7c6e351066e7003835669-18`

来源会话：`95cd7699eeb7c6e351066e7003835669`；当前轮次：`18`

**可见上下文**

> **客服：** 还请您稍等，马上为您查询~
> **客服：** 查看价保成功[金额x]
> **客服：** 是原返支付方式的哈
> **客服：** 未还款的白条是[数字x]小时到账的
> **用户：** 我已经还款了
> **用户：** 钱去哪了
> **客服：** 已经还款的是原返支付方式的亲
> **客服：** 储蓄卡[数字x]-[数字x]个工作日，信用卡是[数字x]-[数字x]5个工作日#E-s5[数字x]

**当前用户：** [姓名x]怎么把退款提出来

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-054-95cd7699eeb7c6e351066e7003835669-18",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 055. `jddc-refund-055-9a812f11653df2c13a4812051e1b22b9-11`

来源会话：`9a812f11653df2c13a4812051e1b22b9`；当前轮次：`11`

**可见上下文**

> **用户：** 请问这个什么时候有货
> **用户：** ?
> **客服：** 小妹查看下哦，请稍等#E-s[数字x]~
> **客服：** [ORDERID_10003907]
> **客服：** 这个目前是缺货的状态哦 补货预计一周哈 这个只是一个大概的时间哦一旦补货会尽快给您发货的哈
> **客服：** 非常抱歉了哈#E-s[数字x]#E-s[数字x]
> **用户：** 如果是这样来的及吗?
> **用户：** [数字x]号我就回老家了

**当前用户：** 来不及可以申请退款不

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-055-9a812f11653df2c13a4812051e1b22b9-11",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 056. `jddc-refund-056-ae90b61c67c258895b12fbbc6980aa6a-6`

来源会话：`ae90b61c67c258895b12fbbc6980aa6a`；当前轮次：`6`

**可见上下文**

> **用户：** 咨询订单号:[ORDERID_10001789] 订单金额:[金额x] 下单时间:[日期x]
> **用户：** 取消订单
> **客服：** 请问您是咨询之前的问题还是有其他的问题需要处理呢?
> **用户：** 地址寄错了
> **客服：** 小妹帮您取消可以么
> **用户：** 可以

**当前用户：** 什么时候可以退款呢?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-056-ae90b61c67c258895b12fbbc6980aa6a-6",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 057. `jddc-refund-057-29702ea2430f9420d19ceb0c350ce847-11`

来源会话：`29702ea2430f9420d19ceb0c350ce847`；当前轮次：`11`

**可见上下文**

> **用户：** 不知道能否真的在[数字x]月[数字x]日送达
> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 我买的一个床显示缺货，但是上面显示的预计送达时间是本周六就想确认一下是否能按时发货，并且准时让我收到货。
> **客服：** 小妹查看下哦，请稍等#E-s[数字x]~
> **客服：** 非常抱歉了哈#E-s[数字x]#E-s[数字x]
> **客服：** 这个目前是缺货的状态哦 补货预计一周哈 这个只是一个大概的时间哦一旦补货会尽快给您发货的哈
> **用户：** 那是不是就意味着我们这个周六很难收到货了，是吗
> **客服：** 是的呢

**当前用户：** 在补货之前是否随时可以退款呢?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-057-29702ea2430f9420d19ceb0c350ce847-11",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 058. `jddc-refund-058-efe0d86158c3ff72a3b3c39210179306-7`

来源会话：`efe0d86158c3ff72a3b3c39210179306`；当前轮次：`7`

**可见上下文**

> **用户：** 您好
> **用户：** 您好
> **用户：** [订单编号:[数字x]，订单金额:[金额x]，下单时间:[日期x] [时间x]]
> **用户：** 您好
> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 我昨天申请的退货今天提示我已经发出退不了，可是我昨天退地 时候还没有发货呢
> **客服：** 您已经申请了取消订单系统会尝试拦截 拦截不成功可以拒收哈

**当前用户：** 拒收的话退款什么时候能收到

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-058-efe0d86158c3ff72a3b3c39210179306-7",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 059. `jddc-refund-059-2d88e1a1317094f8521f9683e16c6d7c-14`

来源会话：`2d88e1a1317094f8521f9683e16c6d7c`；当前轮次：`14`

**可见上下文**

> **客服：** 这边给您重新发可以吗
> **用户：** 可以
> **客服：** 那您先提交下售后哈 亲
> **用户：** 好像操作错了
> **用户：** 给取消订单了
> **客服：** 您是都拒收了吗
> **用户：** 嗯嗯
> **客服：** 这边取消订单的话就是退回了哈

**当前用户：** 那就不要了，退款吧

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-059-2d88e1a1317094f8521f9683e16c6d7c-14",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 060. `jddc-refund-060-9883cc7a32d058063303776678131f1d-27`

来源会话：`9883cc7a32d058063303776678131f1d`；当前轮次：`27`

**可见上下文**

> **用户：** 到店怎么退啊
> **客服：** #E-s[数字x]
> **客服：** 您是说安装服务吗
> **用户：** 嗯
> **用户：** 这是套装吧
> **客服：** 您这边申请售后退款就可以呢
> **用户：** 我重新买了，有两个安装服务
> **客服：** 好的  那您这边提供一下订单号 小妹这边帮您申请呢

**当前用户：** 上面说 不支持单独退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-060-9883cc7a32d058063303776678131f1d-27",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 061. `jddc-refund-061-9aa3fbe1c058a1903bacad2dc4b3654b-7`

来源会话：`9aa3fbe1c058a1903bacad2dc4b3654b`；当前轮次：`7`

**可见上下文**

> **客服：** 请稍等，马上为您核实之前的问题处理进度
> **用户：** 在不在
> **客服：** 您好
> **客服：** [ORDERID_10002835]
> **客服：** 还请您稍等，马上为您查询~
> **客服：** PHICOMM斐讯上臂式电子血压计 LD-[数字x] 白色 [姓名x]RL[数字x]/G[数字x]D 示波法
> **客服：** 请问是这个商品吗

**当前用户：** [ORDERID_10002932]这个订单麻烦帮我申请退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-061-9aa3fbe1c058a1903bacad2dc4b3654b-7",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 062. `jddc-refund-062-a15225ca6dd89239c236c6520a05c5dd-3`

来源会话：`a15225ca6dd89239c236c6520a05c5dd`；当前轮次：`3`

**可见上下文**

> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 你好
> **客服：** 您好

**当前用户：** 我拒收之后，找不到申请退款啦，怎么办?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-062-a15225ca6dd89239c236c6520a05c5dd-3",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 063. `jddc-refund-063-56df27a2cca5e28639e4f58517a7b6a7-8`

来源会话：`56df27a2cca5e28639e4f58517a7b6a7`；当前轮次：`8`

**可见上下文**

> **用户：** 请问有库存还要调拨普通快递是啥意思。
> **客服：** 有什么问题我可以帮您处理或解决呢?
> **客服：** 您好
> **客服：** 还请您稍等，马上为您查询~
> **客服：** 请问是这个商品吗
> **客服：** 索尼(SONY) E [数字x]-[数字x]mm F/[金额x]-[金额x] OSS APS-C画幅远摄大变焦镜头 黑色(SEL[数字x][数字x]) [商品快照]
> **客服：** 您方便简单描述下您的问题吗?
> **用户：** 对

**当前用户：** 商家未按双方约定方式发送快递包裹，且不退款。

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-063-56df27a2cca5e28639e4f58517a7b6a7-8",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 064. `jddc-refund-064-ba0433dd15bb463d0ff8e589ab52d1b5-15`

来源会话：`ba0433dd15bb463d0ff8e589ab52d1b5`；当前轮次：`15`

**可见上下文**

> **用户：** 账号密码没记住
> **用户：** 订单就被我删了
> **用户：** 好气啊
> **客服：** 亲
> **客服：** 您先不要着急
> **用户：** 嗯
> **客服：** 在订单回收站您看看
> **用户：** 怎么找回收站

**当前用户：** 亲  你好我的返款收到了

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-064-ba0433dd15bb463d0ff8e589ab52d1b5-15",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 065. `jddc-refund-065-0567e0f18ef16426781adcc94c624be2-9`

来源会话：`0567e0f18ef16426781adcc94c624be2`；当前轮次：`9`

**可见上下文**

> **用户：** 我刚刚下个订单
> **用户：** 突然想起来忘记买计算器
> **用户：** 再买计算器
> **用户：** 又要付运费
> **用户：** 然后取消之前的订单
> **客服：** 亲亲 那您在重新下单的呢
> **用户：** 又要充银行卡吗
> **用户：** 这张卡就几百块，刚刚已经下单

**当前用户：** 退款不是一个星期吗

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-065-0567e0f18ef16426781adcc94c624be2-9",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 066. `jddc-refund-066-01997d9223b62f4d60fc378cd7d36241-10`

来源会话：`01997d9223b62f4d60fc378cd7d36241`；当前轮次：`10`

**可见上下文**

> **用户：** 我的鞋子你们什么时间给我发过来
> **客服：** 亲爱的，妹子马上为您核实情况，请您稍等哈#E-s[数字x]#E-s[数字x]
> **客服：** 商品还没有到达售后
> **用户：** 如果[数字x]发不过来的话，我可以换一下收货地址吗
> **客服：** 建议您耐心等待下哦
> **客服：** 很抱歉无法刚换的额呢
> **客服：** 更换的饿呢
> **客服：** 这个是已经在处理了呢

**当前用户：** 那我现在退款可以吗

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-066-01997d9223b62f4d60fc378cd7d36241-10",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 067. `jddc-refund-067-1255635799fa6e6cdd0a0ac9de78de38-3`

来源会话：`1255635799fa6e6cdd0a0ac9de78de38`；当前轮次：`3`

**可见上下文**

> **用户：** [订单编号:[ORDERID_10005992]，订单金额:[金额x]，下单时间:[日期x] [时间x]]
> **用户：** 你好
> **客服：** 有什么问题我可以帮您处理或解决呢?

**当前用户：** 这款商品，我[数字x]月[数字x]日下的，商家未发货，直接把单号填了，到现在还没收到，申请退款也失败，咨询商家，商家说帮我查，就没有后续

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-067-1255635799fa6e6cdd0a0ac9de78de38-3",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 068. `jddc-refund-068-2f6e6637c067040602bd444046721088-8`

来源会话：`2f6e6637c067040602bd444046721088`；当前轮次：`8`

**可见上下文**

> **用户：** 你好
> **用户：** [订单编号:[ORDERID_10000027]，订单金额:[金额x]，下单时间:[日期x] [时间x]]
> **用户：** 拒收退款
> **用户：** 你好
> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 我想问一下 我有物品拒收了
> **客服：** 退款时效，储蓄卡是[数字x]-[数字x]个工作日，信用卡是[数字x]-[数字x]5个工作日哦，微信零钱支付的是两个工作日[站点x]
> **用户：** 应该怎么处理?

**当前用户：** 还需要点击退款申请吗

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-068-2f6e6637c067040602bd444046721088-8",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 069. `jddc-refund-069-c8f9236b2094566caafd9df2fa064c12-8`

来源会话：`c8f9236b2094566caafd9df2fa064c12`；当前轮次：`8`

**可见上下文**

> **用户：** 你好
> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 怎么我的退货申请还没有处理呢
> **客服：** 辛苦您提供一下订单号#E-s[数字x]
> **用户：** <a rel="gallery" title="" href="http://img[金额x][链接x]"><img class="message-img" src="http://img[金额x][链接x]"/></a>
> **用户：** 这个
> **用户：** [数字x]0
> **客服：** 您是有申请售后吗

**当前用户：** 申请的退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-069-c8f9236b2094566caafd9df2fa064c12-8",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 070. `jddc-refund-070-25a4c24e5055836d5b1daf7c0d4d2187-7`

来源会话：`25a4c24e5055836d5b1daf7c0d4d2187`；当前轮次：`7`

**可见上下文**

> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 我的订单用错优惠卷 可不可以更改的?
> **用户：** 或者我想取消订单重新拍可以不?
> **客服：** 亲，实在抱歉，让您久等了
> **客服：** 亲爱的，真的很遗憾，订单一旦提交，订单里的信息是无法修改的呢，
> **客服：** 咱们建议您取消订单，重新下单勾选使用哦
> **用户：** 好吧 那我取消

**当前用户：** 是不是选“申请退款”就可以取消订单了?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-070-25a4c24e5055836d5b1daf7c0d4d2187-7",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 071. `jddc-refund-071-94ea2621ac105d796587ec118cb5775e-6`

来源会话：`94ea2621ac105d796587ec118cb5775e`；当前轮次：`6`

**可见上下文**

> **用户：** 在吗
> **用户：** [订单编号:[ORDERID_10006023]，订单金额:[金额x]，下单时间:[日期x] [时间x]]
> **用户：** 在吗
> **客服：** 请问您是咨询之前的问题还是有其他的问题需要处理呢?
> **用户：** 其他问题
> **用户：** [ORDERID_10006023]

**当前用户：** 这个服务如果我不需要了可以申请退款吗

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-071-94ea2621ac105d796587ec118cb5775e-6",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 072. `jddc-refund-072-c238f2fc9966024078b1362e67b72644-2`

来源会话：`c238f2fc9966024078b1362e67b72644`；当前轮次：`2`

**可见上下文**

> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 如果我收到的相机被拆封过

**当前用户：** 可以退货退款吗

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-072-c238f2fc9966024078b1362e67b72644-2",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 073. `jddc-refund-073-b8f370f4a97d5bd0f01b79f5ad528c41-20`

来源会话：`b8f370f4a97d5bd0f01b79f5ad528c41`；当前轮次：`20`

**可见上下文**

> **客服：** 让他们给您退回周期[数字x]天左右安排退款可以吗
> **用户：** 行
> **客服：** 原路返回的
> **客服：** 后期我帮您跟进下
> **客服：** 退款后提醒下
> **客服：** 您
> **客服：** 辛苦了
> **客服：** 请问还有其他还可以帮到您的吗?

**当前用户：** 需要我点申请退款吗

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-073-b8f370f4a97d5bd0f01b79f5ad528c41-20",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 074. `jddc-refund-074-69fcf15afda36cf88b73b190f246e9a1-2`

来源会话：`69fcf15afda36cf88b73b190f246e9a1`；当前轮次：`2`

**可见上下文**

> **用户：** 我要咨询其他问题
> **客服：** 有什么问题我可以帮您处理或解决呢?

**当前用户：** 你好!退货后多长时间能退款?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-074-69fcf15afda36cf88b73b190f246e9a1-2",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 075. `jddc-refund-075-55d7749d334d7771307687d56707e6e2-15`

来源会话：`55d7749d334d7771307687d56707e6e2`；当前轮次：`15`

**可见上下文**

> **客服：** 差价[数字x]吗
> **用户：** 是的
> **用户：** 退吗?
> **客服：** 已经申请成功了
> **用户：** 是退钱包里面吗?
> **客服：** 您是刷卡的吗
> **用户：** 是
> **客服：** 返回卡里的

**当前用户：** 退钱包也可以退卡也可以

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-075-55d7749d334d7771307687d56707e6e2-15",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 076. `jddc-refund-076-46828e6565b6f70590c6b1fc22dda619-3`

来源会话：`46828e6565b6f70590c6b1fc22dda619`；当前轮次：`3`

**可见上下文**

> **用户：** 咨询订单号:[ORDERID_10003803] 订单金额:[金额x] 下单时间:[日期x]
> **用户：** 您好
> **客服：** 请问您是咨询之前的问题还是有其他的问题需要处理呢?

**当前用户：** 我现在不想要这个包包了，申请退款了

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-076-46828e6565b6f70590c6b1fc22dda619-3",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 077. `jddc-refund-077-1d3175cdda8789938c5bb07bdb824f2e-4`

来源会话：`1d3175cdda8789938c5bb07bdb824f2e`；当前轮次：`4`

**可见上下文**

> **用户：** 你好
> **客服：** 请问您是咨询之前的问题还是有其他的问题需要处理呢?
> **用户：** 刚才我联系了商家，那边也同意了
> **客服：** 还请您稍等，马上为您查询~

**当前用户：** 需要你们帮我发起申请退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-077-1d3175cdda8789938c5bb07bdb824f2e-4",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 078. `jddc-refund-078-ae6ce44d5b74e000c96df0bb946238c6-19`

来源会话：`ae6ce44d5b74e000c96df0bb946238c6`；当前轮次：`19`

**可见上下文**

> **客服：** 这边后续会给您退款的呢
> **用户：** 把我的所有订单全部申请取消吧。我重新下单
> **客服：** 这边您已经提交了呢
> **用户：** 谢谢
> **客服：** 关联订单也是会取消的呢
> **客服：** #E-s[数字x]
> **客服：** 请问还有其他还可以帮到您的吗?
> **用户：** 实在是特别不好意思。把我所有的订单取消退款，我重新下单。

**当前用户：** 是所有的商品都申请退款了吗?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-078-ae6ce44d5b74e000c96df0bb946238c6-19",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 079. `jddc-refund-079-ce0e8834420c04a011f582f6f58c3c86-20`

来源会话：`ce0e8834420c04a011f582f6f58c3c86`；当前轮次：`20`

**可见上下文**

> **客服：** [姓名x]#E-s[数字x]
> **用户：** 谢谢你
> **客服：** #E-s[数字x]#E-s[数字x]请问还有其他还可以帮到您的吗?
> **客服：** #E-s[数字x]#E-s[数字x]#E-s[数字x]
> **用户：** 没有了，谢谢你
> **客服：** #E-s[数字x]#E-s[数字x]
> **用户：** 再见
> **客服：** #E-s[数字x]#E-s[数字x]

**当前用户：** 退款什么时候到帐?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-079-ce0e8834420c04a011f582f6f58c3c86-20",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 080. `jddc-refund-080-2551e53a056cbd4622f037fd30356fe8-1`

来源会话：`2551e53a056cbd4622f037fd30356fe8`；当前轮次：`1`

**可见上下文**

> **用户：** 你好

**当前用户：** 我这个帮我取消，申请退款，太厚了，换了一个，不好意思，给你们添麻烦了

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-080-2551e53a056cbd4622f037fd30356fe8-1",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 081. `jddc-refund-081-1a93282cb8670c550fe9b9cd15c3fe4d-10`

来源会话：`1a93282cb8670c550fe9b9cd15c3fe4d`；当前轮次：`10`

**可见上下文**

> **用户：** 款都催了好多次了，什么时候能退给我
> **客服：** 小妹为您看看，您稍等哦#E-s[数字x]
> **客服：** [数字x]号联系您没有接听
> **客服：** 帮您通知售后再处理下
> **用户：** 是的
> **客服：** 麻烦保持电话畅通哦
> **用户：** 我电话有时接不到
> **客服：** 好的

**当前用户：** 一定要接到电话才能退款吗

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-081-1a93282cb8670c550fe9b9cd15c3fe4d-10",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 082. `jddc-refund-082-eb21e6a6757a54f22cf4db5da65d01ea-15`

来源会话：`eb21e6a6757a54f22cf4db5da65d01ea`；当前轮次：`15`

**可见上下文**

> **用户：** 已经送达了
> **客服：** 邮政快递吗
> **客服：** 什么时候寄出的呢
> **用户：** 周六
> **用户：** 周日就送达了
> **客服：** 妹子给您添加好了
> **客服：** 您就不用添加了哦`
> **客服：** #E-s[数字x]

**当前用户：** 那什么时候退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-082-eb21e6a6757a54f22cf4db5da65d01ea-15",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 083. `jddc-refund-083-8daa6b2e361fad6bf27137255dfdb875-10`

来源会话：`8daa6b2e361fad6bf27137255dfdb875`；当前轮次：`10`

**可见上下文**

> **用户：** 你好
> **用户：** 我买的[数字x]个钱包。有一个后面有划痕不想要了
> **用户：** 订单号[ORDERID_10004617]
> **客服：** 好的，请您稍等，这边咨询不是一对一，同时需要回复多个消息，我会尽快回复您。#E-s[数字x]
> **客服：** 非常抱歉呢，给您添麻烦了，建议您直接申请售后服务呢#E-s[数字x]
> **客服：** 建议您申请一下售后哦，在[站点x]-[站点x]-[站点x]，返修退换申请
> **用户：** 我买的是买[数字x]免[数字x]的，可以退一个留一个吗?
> **客服：** 可以的哦亲爱的

**当前用户：** 那么退款的金额是怎么算的呢?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-083-8daa6b2e361fad6bf27137255dfdb875-10",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 084. `jddc-refund-084-c619e121b1216a36e330c53dd9075f60-13`

来源会话：`c619e121b1216a36e330c53dd9075f60`；当前轮次：`13`

**可见上下文**

> **用户：** [数字x]-[数字x]天发货吗
> **客服：** 是的呢
> **用户：** 什么时候能告诉我是否发货了?
> **用户：** 最早
> **客服：** 具体时间以物流为准呢
> **用户：** 那如果现在选择退款可以嘛
> **客服：** 也是可以的嗯
> **用户：** 好

**当前用户：** 用卡买的，退款多久打回卡里

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-084-c619e121b1216a36e330c53dd9075f60-13",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 085. `jddc-refund-085-a7ad2738575029509614d48e76cee235-4`

来源会话：`a7ad2738575029509614d48e76cee235`；当前轮次：`4`

**可见上下文**

> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 我的订单本来都到了准备收货，今天突然发现取消了
> **用户：** 可是我没有取消啊
> **客服：** 马上核实情况，请您稍等哈#E-s[数字x]

**当前用户：** 之前一直没给我打电话，我以为是学校的配送就这两天呢，今天一看退款了，不知道咋回事

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-085-a7ad2738575029509614d48e76cee235-4",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 086. `jddc-refund-086-82d4c9e4fbef0d755ac824e832ccf3ee-9`

来源会话：`82d4c9e4fbef0d755ac824e832ccf3ee`；当前轮次：`9`

**可见上下文**

> **客服：** 请问您是咨询之前的问题还是有其他的问题需要处理呢?
> **用户：** 我有一个订单昨天取消了，但是今天显示已经给我送到自提柜了，怎么办?
> **客服：** 请您稍等一下，正在为您核实处理中哦~
> **用户：** 我昨天有[数字x]个订单，其中有一个订错了，取消了[数字x]个
> **用户：** 但是[数字x]个都送到自提柜了
> **客服：** 那您不需要的商品  [数字x]天不取 会退回
> **客服：** 退回后退款给您
> **用户：** 都是自动的? 不需要我自己操作，是吗?

**当前用户：** 过期未自提货物将按退库处理 ，然后自动退款?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-086-82d4c9e4fbef0d755ac824e832ccf3ee-9",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 087. `jddc-refund-087-7641ac381c288f1a8a98c7223b2802bb-1`

来源会话：`7641ac381c288f1a8a98c7223b2802bb`；当前轮次：`1`

**可见上下文**

> **客服：** 有什么问题我可以帮您处理或解决呢?

**当前用户：** 申请取消订单退款不了

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-087-7641ac381c288f1a8a98c7223b2802bb-1",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 088. `jddc-refund-088-20aa0611f5cae6fb5065e56418f8ba07-5`

来源会话：`20aa0611f5cae6fb5065e56418f8ba07`；当前轮次：`5`

**可见上下文**

> **用户：** ?
> **客服：** 有什么问题我可以帮您处理或解决呢?#E-s[数字x]
> **用户：** 我申请退货了
> **用户：** 审核通过了
> **客服：** 为了更快的处理您的问题辛苦您提供下订单号给我可以吗#E-s[数字x]

**当前用户：** 什么时候退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-088-20aa0611f5cae6fb5065e56418f8ba07-5",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 089. `jddc-refund-089-e58d7c8499d01ad89dff489f446b16f1-20`

来源会话：`e58d7c8499d01ad89dff489f446b16f1`；当前轮次：`20`

**可见上下文**

> **客服：** 亲，请您打开“我的订单”--“查看”--“确认收货”。然后重新打开“我的订单”--“申请返修退换货”!链接是http://myjd.jd.com/repair/orderlist.action
> **客服：** 可以申请售后的
> **客服：** 只能换同款的
> **用户：** 就是换同款的
> **用户：** 是这样的，我一天从你家下了大概六个单，都走售后比较麻烦，所以想咨询一下能不能直接你们统一给调换一下
> **客服：** 亲爱的这个是需要走售后处理的亲
> **用户：** 申请了就能给换么
> **客服：** 您可以退款重新买的

**当前用户：** 我是双十一买的，退款重新买，不是双十一的价格了吧

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-089-e58d7c8499d01ad89dff489f446b16f1-20",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 090. `jddc-refund-090-4f18d07ebe462bc120d092ce116d7feb-14`

来源会话：`4f18d07ebe462bc120d092ce116d7feb`；当前轮次：`14`

**可见上下文**

> **客服：** #E-s[数字x]
> **客服：** 会的呢
> **用户：** 我要赶时间的  所以收到后尽快发过来
> **用户：** 要不这个我就退单  我再订一台?
> **用户：** 时间会快点嘛?
> **客服：** 咱们售后接收到商品，确定有质量问题时[数字x]日之内会给您完成退款的哦~京东完成退款操作后，就等待退款到账啦~
> **客服：** 咱们售后接收到商品，确定有质量问题时[数字x]日之内会给您完成换货的哦~到时您的京东账户内会有换新订单哒，还麻烦您查看呢~
> **用户：** 时间会快点吗??

**当前用户：** [数字x]天退款没事

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-090-4f18d07ebe462bc120d092ce116d7feb-14",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 091. `jddc-refund-091-e83d6d20aa1dec0dbb35affc206a729e-11`

来源会话：`e83d6d20aa1dec0dbb35affc206a729e`；当前轮次：`11`

**可见上下文**

> **用户：** 现在价格是[数字x]
> **用户：** 为什么会波动这么大
> **客服：** 小妹为您看看，您稍等哦#E-s[数字x]
> **用户：** 好的
> **用户：** 在线等
> **客服：** 亲爱哒
> **客服：** 查询您是已经退款申请了呢
> **用户：** 而且还有活动 减[数字x]

**当前用户：** 是的 我看价格差了将近[数字x] 我就申请退款了

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-091-e83d6d20aa1dec0dbb35affc206a729e-11",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 092. `jddc-refund-092-03c9008a1ec1af08c5d633ff66e04fd6-3`

来源会话：`03c9008a1ec1af08c5d633ff66e04fd6`；当前轮次：`3`

**可见上下文**

> **用户：** [订单编号:[ORDERID_10003135]，订单金额:[金额x]，下单时间:[日期x] [时间x]]
> **用户：** 退款
> **用户：** 你好

**当前用户：** 我想问一下这个多长时间能退款完成?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-092-03c9008a1ec1af08c5d633ff66e04fd6-3",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 093. `jddc-refund-093-7b6582a733fa721de140a5fefa88990e-10`

来源会话：`7b6582a733fa721de140a5fefa88990e`；当前轮次：`10`

**可见上下文**

> **用户：** 您好，这款商品昨天退回已被取走，您能帮我查查到哪个流程了?
> **客服：** 请您稍等一下，正在为您核实处理中哦~
> **用户：** 好的，感谢
> **客服：** 亲 还没有到达库房
> **客服：** 到达库房会安排处理的哈
> **客服：** 还请您等待一下哈
> **用户：** 您受累帮我看看
> **客服：** 亲 还没有到达库房 到达库房您的售后信息会更改信息状态

**当前用户：** 仓库看到东西了才能处理退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-093-7b6582a733fa721de140a5fefa88990e-10",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 094. `jddc-refund-094-6eb8eda1b4cccdeb0e673e0fce40d25a-11`

来源会话：`6eb8eda1b4cccdeb0e673e0fce40d25a`；当前轮次：`11`

**可见上下文**

> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 刚刚下的订单
> **客服：** 马上核实情况，请您稍等哈#E-s[数字x]
> **客服：** 可以取消订单的
> **用户：** 好的
> **用户：** 请取消
> **客服：** #E-s[数字x]#E-s[数字x]
> **客服：** 请问还有其他还可以帮到您的吗?

**当前用户：** 直接退款到白条

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-094-6eb8eda1b4cccdeb0e673e0fce40d25a-11",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 095. `jddc-refund-095-7c4322039dc742f944a65060817c757f-12`

来源会话：`7c4322039dc742f944a65060817c757f`；当前轮次：`12`

**可见上下文**

> **用户：** 是不是要退货的话就直接拒收就可以了?
> **用户：** 请问还有人在吗?
> **客服：** 亲爱哒，非常抱歉#E-s[数字x]，让您久等了#E-s[数字x]
> **客服：** 您是拒收的话
> **客服：** 商品退回库房后处理退款的
> **客服：** 我给您重新提交退款申请您看是否可以呢
> **用户：** 可以麻烦了谢谢!
> **客服：** 您客气了哦，这是小妹应该做的哦#E-s[数字x]

**当前用户：** 退款什么时候可以到账?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-095-7c4322039dc742f944a65060817c757f-12",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 096. `jddc-refund-096-2e44f5d76b8fb0f13a28c67d9474bad0-2`

来源会话：`2e44f5d76b8fb0f13a28c67d9474bad0`；当前轮次：`2`

**可见上下文**

> **用户：** [数字x]当天送达的没有送，怎么申请退款呢?
> **客服：** 有什么问题我可以帮您处理或解决呢?

**当前用户：** 你好，[数字x]当天送达的，昨天没有送到，怎么申请退款呢

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-096-2e44f5d76b8fb0f13a28c67d9474bad0-2",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 097. `jddc-refund-097-0b35f1227a23e8cc45861ebb6fd917d5-3`

来源会话：`0b35f1227a23e8cc45861ebb6fd917d5`；当前轮次：`3`

**可见上下文**

> **客服：** 有什么问题我可以帮您处理或解决呢?
> **用户：** 我的快递一直没有更新
> **用户：** 退款也没有处理

**当前用户：** 到底是会继续派送还是退款?

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-097-0b35f1227a23e8cc45861ebb6fd917d5-3",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 098. `jddc-refund-098-eb0792b63af3b2688cd002fe4e58cd3c-8`

来源会话：`eb0792b63af3b2688cd002fe4e58cd3c`；当前轮次：`8`

**可见上下文**

> **用户：** 您好
> **用户：** https://item.jd.com/694207.html
> **用户：** 您好
> **用户：** 您好，我之前是换新，因为我这几天不在家，快递员没有配送成功，今天快递员告诉我说，时间长了，显示拒收了
> **客服：** 亲爱的客户，还麻烦您提供下订单号，妹子这边给您查询哦~
> **用户：** [数字x]
> **客服：** 商品退回之后 给您处理商品的退款的哈
> **客服：** 预计[数字x]-[数字x]天的哈

**当前用户：** 我想换新，不想退款的啊

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-098-eb0792b63af3b2688cd002fe4e58cd3c-8",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 099. `jddc-refund-099-19d4ac8c06218cdff82d1b73dcd65bde-6`

来源会话：`19d4ac8c06218cdff82d1b73dcd65bde`；当前轮次：`6`

**可见上下文**

> **客服：** 请问您是咨询之前的问题还是有其他的问题需要处理呢?
> **用户：** 一周才能发货吗?
> **客服：** 好的，请您稍等，这边咨询不是一对一，同时需要回复多个消息，我会尽快回复您。#E-s[数字x]
> **客服：** 预计一周的哦 #E-s[数字x]到货之后第一时间发货给您的哈
> **用户：** 就是要一周左右才能发货啊?
> **客服：** 由于像您一样有眼光的客户较多，这款商品的订单量比较大，到货后，我们会按照订单预定的先后顺序给您发货的，还请您耐心等待一下，我们对待每一个客户服务都是一样的，还请您放心~#E-s[数字x]#E-s[数字x]

**当前用户：** 我能申请退款吗，太久了

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-099-19d4ac8c06218cdff82d1b73dcd65bde-6",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---

## 100. `jddc-refund-100-d5861c858abca483747d46d8483ec97a-2`

来源会话：`d5861c858abca483747d46d8483ec97a`；当前轮次：`2`

**可见上下文**

> **用户：** 咨询订单号:[数字x] 订单金额:[金额x] 下单时间:[日期x]
> **客服：** 有什么问题我可以帮您处理或解决呢?

**当前用户：** 请问什么时候退款

### 请填写（只改 TODO，不要修改上面的原文）

```json
{
  "case_id": "jddc-refund-100-d5861c858abca483747d46d8483ec97a-2",
  "speech_act": "TODO",
  "expected_requests": [],
  "primary_goal": null,
  "expected_workflow": null,
  "needs_clarification": false,
  "notes": "TODO"
}
```

---
