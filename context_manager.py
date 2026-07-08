"""
上下文管理器 — 260K 上下文窗口保护

三级压缩保护机制：
- NORMAL: 全量对话历史 + persona + 记忆注入
- COMPRESSED: 最近 N 轮原文 + 早期轮次摘要
- RETIRED: 仅 persona + 最近 3 轮 + 全部摘要

作者: Spec 003 - 多模态自主 AI
"""

import asyncio
import json
import re
import time
from typing import Optional

from json_utils import parse_json_block

from config import (
    MAX_CONTEXT_TOKENS,
    CONTEXT_COMPRESS_PCT,
    CONTEXT_RETIRE_PCT,
    CONTEXT_KEEP_RECENT,
    LLM_API_KEY, LLM_BASE_URL, LLM_FAST_MODEL,
)

from log_config import get_logger
logger = get_logger("ctx")


def estimate_tokens(text: str) -> int:
    """粗略 token 估算。中文 1 字≈1 token，英文 1 词≈1.3 token。"""
    cjk = len(re.findall(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]', text))
    rest = re.sub(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]', ' ', text)
    words = len(rest.split())
    return cjk + int(words * 1.3)


def estimate_total_tokens(
    messages: list[dict],
    memories: list[dict],
    search_results: list[str],
) -> int:
    """估算消息 + 记忆 + 搜索结果的总 token 数，含 10% 安全余量。

    Args:
        messages: 对话消息列表 [{role, content}]
        memories: 注入的记忆列表 [{value, ...}]
        search_results: 搜索摘要字符串列表

    Returns:
        估算 token 数（含 10% 安全余量）
    """
    total = 0

    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            # 多模态: [{type:"text","text":"..."}, {type:"image_url",...}]
            text = " ".join(item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text")
            total += estimate_tokens(text)
            # 每张图片粗略算 100 tokens
            img_count = sum(1 for item in content if isinstance(item, dict) and item.get("type") == "image_url")
            total += img_count * 100
        else:
            total += estimate_tokens(str(content) if content else "")

    for mem in memories:
        total += estimate_tokens(str(mem.get("value", "")))

    for sr in search_results:
        total += estimate_tokens(sr)

    # 10% 安全余量
    return int(total * 1.1)


async def compress_old_messages(
    messages: list[dict],
    keep_recent: int = CONTEXT_KEEP_RECENT,
) -> list[dict]:
    """LLM 摘要化旧轮次消息。

    保留最近 keep_recent 轮原文，将更早的消息压缩为摘要。

    Args:
        messages: 对话消息列表（不含 system）
        keep_recent: 保留最近 N 轮原文（每轮 = user + assistant 对）

    Returns:
        压缩后的消息列表: [摘要, ...最近N轮原文...]
    """
    if len(messages) <= keep_recent * 2 + 2:
        return messages  # 太少不需要压缩

    # 分离旧消息和最近消息
    keep_count = keep_recent * 2  # user + assistant 成对
    old_msgs = messages[:-keep_count]
    recent_msgs = messages[-keep_count:]

    # 构建摘要 prompt
    old_text = []
    for m in old_msgs:
        role = m.get("role", "user")
        content = m.get("content", "")[:200]  # 截断长消息
        old_text.append(f"[{role}]: {content}")

    prompt = f"""将以下对话历史压缩为简洁摘要（保留关键信息、事实、话题转折）：

{chr(10).join(old_text)}

输出 JSON: {{"summary": "简洁摘要"}}"""

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=LLM_FAST_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                temperature=0.2,
            ),
            timeout=5.0,
        )
        raw = response.choices[0].message.content or ""
        # 解析 JSON
        try:
            data = parse_json_block(raw)
            summary = data.get("summary", "对话历史摘要不可用") if data else "对话历史摘要不可用"
        except (json.JSONDecodeError, ValueError):
            summary = raw.strip()[:300] or "对话历史摘要不可用"
    except asyncio.TimeoutError:
        logger.warning("压缩 LLM 超时，使用规则降级")
        summary = "（对话历史过长，早期内容已省略）"
    except Exception:
        logger.warning("压缩 LLM 失败，使用规则降级")
        summary = "（对话历史过长，早期内容已省略）"

    # 构建压缩后的消息列表
    compressed = [
        {"role": "system", "content": f"[对话历史摘要] {summary}"}
    ] + recent_msgs

    logger.info("消息压缩: %d 条 → %d 条 (摘要)", len(messages), len(compressed))
    return compressed


def retire_to_minimal(messages: list[dict], keep_recent: int = 3) -> list[dict]:
    """退役到最小上下文：仅保留 persona + 最近 3 轮。

    同步操作，不需要 LLM 调用。

    Args:
        messages: 当前消息列表（含 system persona）
        keep_recent: 保留最近 N 轮原文

    Returns:
        退役后的消息列表
    """
    # 保留 system 消息（persona）
    system_msgs = [m for m in messages if m.get("role") == "system" and "[对话历史摘要]" not in m.get("content", "")]
    chat_msgs = [m for m in messages if m.get("role") in ("user", "assistant")]

    keep_count = keep_recent * 2
    recent = chat_msgs[-keep_count:] if len(chat_msgs) > keep_count else chat_msgs

    result = system_msgs + recent
    logger.warning("上下文已退役: %d 条 → %d 条", len(messages), len(result))
    return result


CtxStatus = str  # "normal" | "compressed" | "retired"


def check_and_handle(
    messages: list[dict],
    total_tokens: int,
    compress_pct: float = CONTEXT_COMPRESS_PCT,
    retire_pct: float = CONTEXT_RETIRE_PCT,
) -> CtxStatus:
    """检查上下文 token 使用率并返回当前状态。

    不会修改 messages——调用方根据返回状态决定操作。

    Args:
        messages: 当前消息列表（含 system）
        total_tokens: 估算的总 token 数
        compress_pct: 压缩阈值比例
        retire_pct: 退役阈值比例

    Returns:
        "normal" / "compressed" / "retired"
    """
    max_tokens = MAX_CONTEXT_TOKENS
    if max_tokens <= 0:
        return "normal"
    usage_ratio = total_tokens / max_tokens if max_tokens > 0 else 0

    if usage_ratio >= retire_pct:
        return "retired"
    elif usage_ratio >= compress_pct:
        return "compressed"
    else:
        return "normal"


class ContextManager:
    """上下文管理器 —— 封装 token 估算 + 压缩 + 退役逻辑。

    在 engine._build_messages() 中调用 check_and_handle() 决定上下文策略。
    """

    def __init__(self):
        pass

    async def handle_overflow(
        self,
        messages: list[dict],
        memories: list[dict],
        search_results: list[str],
    ) -> tuple[list[dict], CtxStatus]:
        """检查溢出并自动处理。

        Returns:
            (处理后消息列表, 状态字符串)
        """
        total = estimate_total_tokens(messages, memories, search_results)
        status = check_and_handle(messages, total)

        if status == "retired":
            messages = retire_to_minimal(messages, CONTEXT_KEEP_RECENT)
        elif status == "compressed":
            chat_msgs = [m for m in messages if m.get("role") in ("user", "assistant")]
            system_msgs = [m for m in messages if m.get("role") == "system" and "[对话历史摘要]" not in m.get("content", "")]
            if len(chat_msgs) > CONTEXT_KEEP_RECENT * 2:
                compressed = await compress_old_messages(chat_msgs, CONTEXT_KEEP_RECENT)
                messages = system_msgs + compressed

        return messages, status


# 模块级单例
_ctx_manager: Optional[ContextManager] = None


def get_context_manager() -> ContextManager:
    """获取全局单例 ContextManager。"""
    global _ctx_manager
    if _ctx_manager is None:
        _ctx_manager = ContextManager()
    return _ctx_manager


# ==================== 关键词提取（从 engine.py 迁入）====================

import re as _re

# 中文停用词（高频无意义词）
_STOPWORDS: set = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "些",
    "什么", "怎么", "怎样", "哪", "吗", "呢", "吧", "啊", "哦", "嗯",
    "可以", "能", "应该", "因为", "所以", "但是", "虽然", "如果", "的话",
    "这个", "那个", "哪个", "一下", "一点", "觉得", "知道", "想", "让",
    "给", "对", "跟", "用", "把", "被", "从", "向", "关于", "通过",
}

# 中文标点
_CN_PUNCT = set("，。！？；：""''（）【】《》…—～、·")

# jieba 可选增强（存在则用，不存在则回退规则分词）
try:
    import jieba
    jieba.setLogLevel(20)
    _JIEBA_AVAILABLE = True
except ImportError:
    _JIEBA_AVAILABLE = False


def _tokenize_jieba(text: str) -> list[str]:
    """使用 jieba 分词，过滤停用词和标点。"""
    words = jieba.cut(text)
    result: list[str] = []
    for w in words:
        w = w.strip()
        if not w or len(w) < 2:
            continue
        if w in _STOPWORDS or w in _CN_PUNCT:
            continue
        if _re.match(r'^[\s，。！？；：""''（）【】《》…—～、·]+$', w):
            continue
        result.append(w)
    return result


def _segment_text(text: str) -> list[str]:
    """简单中文分词：优先 jieba，不存在时回退规则分词。"""
    if _JIEBA_AVAILABLE:
        try:
            return _tokenize_jieba(text)
        except Exception:
            pass

    blocks = _re.findall(r'[\u4e00-\u9fff\uff00-\uffefA-Za-z0-9]+', text)
    tokens: list[str] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if _re.match(r'^[A-Za-z0-9]+$', block):
            tokens.append(block.lower())
            continue
        chars = list(block)
        n = len(chars)
        for i in range(n):
            ch = chars[i]
            if ch not in _STOPWORDS and ch not in _CN_PUNCT and len(ch.strip()) > 0:
                tokens.append(ch)
            if i + 1 < n:
                bi = chars[i] + chars[i + 1]
                if bi not in _STOPWORDS:
                    tokens.append(bi)
            if i + 2 < n:
                tri = chars[i] + chars[i + 1] + chars[i + 2]
                tokens.append(tri)
    seen: set[str] = set()
    result: list[str] = []
    for t in tokens:
        if t not in seen and len(t) >= 1:
            seen.add(t)
            result.append(t)
    return result


def extract_keywords_rule(content: str) -> list[str]:
    """规则提取中文关键词（同步）。

    流程：分词 → unigram/bigram/trigram → 过滤停用词 → 去重 → 最多 10 个。
    """
    tokens = _segment_text(content)
    multis = [t for t in tokens if len(t) >= 2]
    singles = [t for t in tokens if len(t) == 1]
    seen: set[str] = set()
    result: list[str] = []
    for t in multis:
        if t not in seen:
            seen.add(t)
            result.append(t)
    for t in singles:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result[:10]


async def extract_keywords_async(content: str) -> list[str]:
    """获取搜索关键词（异步入口，含 LLM 补全）。

    规则提取 → 结果 < 3 且消息 > 10 字 → LLM 补全。
    """
    from openai import AsyncOpenAI
    from config import LLM_FAST_MODEL

    keywords = extract_keywords_rule(content)

    if len(keywords) < 3 and len(content) > 10:
        prompt = f"""从以下用户消息中提取关键词(2-5个)、话题标签(1-3个)、指代消解。

消息: {content}

输出 JSON 格式:
{{"keywords": ["词1","词2"], "topic_tags": ["标签1"], "resolved_entities": {{"他":"张三"}}}}"""
        try:
            client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=LLM_FAST_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=128,
                    temperature=0.1,
                ),
                timeout=1.0,
            )
            raw = response.choices[0].message.content or ""
            data = parse_json_block(raw)
            llm_kw = data.get("keywords", [])
            if llm_kw:
                seen = set(keywords)
                merged = list(keywords)
                for kw in llm_kw:
                    if kw not in seen:
                        seen.add(kw)
                        merged.append(kw)
                return merged[:10]
        except (asyncio.TimeoutError, Exception):
            logger.debug("LLM 关键词提取失败，使用规则结果")

    return keywords


# ==================== 会话监测（从 context_monitor 迁入）====================

def _session_health(session) -> dict:
    """计算单个 session 的健康指标。"""
    total_tokens = sum(estimate_tokens(m["content"]) for m in session.messages)
    usage_pct = round(total_tokens / MAX_CONTEXT_TOKENS, 3) if MAX_CONTEXT_TOKENS > 0 else 0
    idle_s = time.time() - session.last_active

    from config import CONTEXT_SATURATION_PCT
    if hasattr(session, 'is_expired') and session.is_expired():
        status = "expired"
    elif usage_pct > 0.95:
        status = "critical"
    elif usage_pct > CONTEXT_SATURATION_PCT:
        status = "saturated"
    elif idle_s > 1800:
        status = "idle"
    else:
        status = "healthy"

    return {
        "session_id": session.session_id,
        "message_count": len(session.messages),
        "estimated_tokens": total_tokens,
        "max_tokens": MAX_CONTEXT_TOKENS,
        "context_usage_pct": usage_pct,
        "saturated": usage_pct > CONTEXT_SATURATION_PCT,
        "idle_seconds": round(idle_s, 1),
        "status": status,
    }


def check_session(session) -> dict:
    """检查单个 session 健康状态。"""
    report = _session_health(session)
    if report["status"] == "critical":
        logger.warning(
            "上下文严重饱和: session=%s tokens=%d/%d (%.0f%%)",
            session.session_id[:12], report["estimated_tokens"],
            report["max_tokens"], report["context_usage_pct"] * 100,
        )
    elif report["status"] == "saturated":
        logger.info(
            "上下文饱和: session=%s tokens=%d/%d (%.0f%%)",
            session.session_id[:12], report["estimated_tokens"],
            report["max_tokens"], report["context_usage_pct"] * 100,
        )
    return report


async def global_monitor() -> dict:
    """全局监测摘要：所有 session 的健康分布。"""
    from engine import session_manager
    sessions = list(session_manager._sessions.values())
    if not sessions:
        return {
            "total_sessions": 0,
            "healthy": 0, "saturated": 0, "critical": 0,
            "idle": 0, "expired": 0,
            "avg_tokens": 0,
            "sessions": [],
        }
    reports = [_session_health(s) for s in sessions]
    counts = {"healthy": 0, "saturated": 0, "critical": 0, "idle": 0, "expired": 0}
    for r in reports:
        status = r["status"]
        if status in counts:
            counts[status] += 1
    avg_tokens = sum(r["estimated_tokens"] for r in reports) / len(reports) if reports else 0
    return {
        "total_sessions": len(sessions),
        **counts,
        "avg_tokens": round(avg_tokens, 1),
        "sessions": reports,
    }
