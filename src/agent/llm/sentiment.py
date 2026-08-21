"""src/agent/sentiment.py — 情绪检测 + 升级策略

职责：
1. detect_sentiment()   — 规则检测用户负面情绪（0ms，不花 Token）
2. build_escalation_prompt() — 把检测结果转成 system prompt 指令片段

两个函数各干各的，不互相调用。外部拼起来用：
    sentiment = detect_sentiment(query, history=ctx.messages)
    extra = build_escalation_prompt(sentiment)
    system_prompt = base + extra
"""

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# 检测结果
# =============================================================================
@dataclass
class SentimentResult:
    is_negative: bool = False
    should_escalate: bool = False
    reason: str = ""
    keywords_matched: list[str] = field(default_factory=list)


# =============================================================================
# 词库（V1 硬编码，V2 可从配置文件 / DB 加载）
# =============================================================================
COMPLAINT_KEYWORDS: list[str] = [
    "投诉",
    "举报",
    "退款",
    "12315",
    "消协",
    "工商",
]

ANGER_KEYWORDS: list[str] = [
    "垃圾",
    "烂",
    "坑人",
    "骗人",
    "太差",
    "什么玩意",
    "无语",
    "气死",
    "恶心",
    "什么破",
    "太坑",
]

NEGATION_KEYWORDS: list[str] = [
    "不对",
    "不是",
    "错了",
    "没用",
    "不行",
    "还是不行",
    "不是这个",
    "搞错",
]


# =============================================================================
# 辅助
# =============================================================================
def _find_hits(text: str | None, keywords: list[str]) -> list[str]:
    """返回 text 中命中的所有关键词。text 为 None 时返回空列表。"""
    if not text:
        return []
    return [kw for kw in keywords if kw in text]


# =============================================================================
# 检测
# =============================================================================
def detect_sentiment(
    query: str,
    *,
    history: list[dict[str, Any]] | None = None,
) -> SentimentResult:
    """检测单条用户消息的情绪，纯规则。

    Args:
        query:   当前用户消息（指代消解之后的原文）
        history: 前几轮的 messages 列表 [{"role": "user", "content": "..."}, ...]
                 用于检测"连续否定"

    返回 SentimentResult，不抛异常。
    """
    # ---- 1. 投诉词 — 命中即 escalate ----
    hit = _find_hits(query, COMPLAINT_KEYWORDS)
    if hit:
        logger.info("投诉关键词触发")
        return SentimentResult(
            is_negative=True,
            should_escalate=True,
            reason=f"投诉关键词: {', '.join(hit)}",
            keywords_matched=hit,
        )

    # ---- 2. 愤怒词 ----
    hit_anger = _find_hits(query, ANGER_KEYWORDS)
    if hit_anger:
        logger.info("愤怒关键词触发")

    # ---- 3. 否定词 — 本轮 ----
    hit_neg_now = _find_hits(query, NEGATION_KEYWORDS)

    # ---- 4. 否定词 — 历史 ----
    had_neg_before = False
    if hit_neg_now and history:
        for msg in history:
            if msg.get("role") == "user":
                if _find_hits(msg.get("content"), NEGATION_KEYWORDS):
                    had_neg_before = True
                    break

    # ---- 5. 综合判断 ----
    keywords = hit_anger + hit_neg_now

    if hit_neg_now and had_neg_before:
        # 连续否定 → escalate
        return SentimentResult(
            is_negative=True,
            should_escalate=True,
            reason="用户连续否定，Agent 可能给出了错误答案",
            keywords_matched=keywords,
        )

    if hit_anger and hit_neg_now:
        # 愤怒 + 否定（单轮就两个都有）→ escalate
        return SentimentResult(
            is_negative=True,
            should_escalate=True,
            reason="用户愤怒且表达否定",
            keywords_matched=keywords,
        )

    if hit_anger:
        # 只有愤怒，无否定 → 记录但不升级
        return SentimentResult(
            is_negative=True,
            should_escalate=False,
            reason=f"用户表达负面情绪: {', '.join(hit_anger)}",
            keywords_matched=keywords,
        )

    # ---- 6. 一切正常 ----
    return SentimentResult()


# =============================================================================
# prompt 生成（不调 detect_sentiment，只消费它的结果）
# =============================================================================
def build_escalation_prompt(sentiment: SentimentResult) -> str:
    """把检测结果转成 system prompt 指令片段，拼到 AgentLoop 的 system_prompt 里。

    用法:
        sentiment = detect_sentiment(query, history=ctx.messages)
        extra = build_escalation_prompt(sentiment)
        system_prompt = base_prompt + "\n" + extra
    """
    if sentiment.should_escalate:
        return (
            f"[重要指令] {sentiment.reason}。"
            "请优先安抚用户情绪，不要辩解。"
            "回答结束后必须调用 create_ticket 工具生成工单转接人工客服。"
        )

    if sentiment.is_negative:
        words = ", ".join(sentiment.keywords_matched)
        return f"[注意] 用户表达了负面情绪（{words}）。请注意语气，保持耐心和同理心。"

    return ""
