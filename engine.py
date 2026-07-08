"""
核心 Chat 引擎

不依赖任何外部 CLI——纯 HTTP 调用 OpenAI 兼容 API。
支持所有兼容的 LLM 后端：DeepSeek / OpenAI / Ollama / vLLM / 硅基流动 等。

双脑模式：
- chat()       → 辅脑快速回复 (<500ms 目标)
- evaluate()   → 双主脑异步评估 + 追答
- chat_with_evaluate() → 一站式：快速回复 + 自动触发评估

上下文组装：
engine 负责从 memory_store 实时拉取记忆索引 + persona，
构建完整 system prompt + 对话历史 → 传给 LLM。
orchestrator 只负责路由，不参与 prompt 拼装。
"""

import asyncio
import json
import logging
import time
from typing import Optional

from openai import AsyncOpenAI

from config import (
    LLM_API_KEY, LLM_BASE_URL, LLM_FAST_MODEL, LLM_STRONG_MODEL,
    DEFAULT_SYSTEM_PROMPT, MAX_CONTEXT_TOKENS,
)
from session import Session, SessionManager

logger = logging.getLogger("engine")
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("[engine] %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)

session_manager = SessionManager()

# 模块级单例 AsyncOpenAI 实例（并发复用，减少连接开销）
_fast_client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
_strong_client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


# ==================== Context Assembly ====================

# --- Phase 1: 关键词提取 ---

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


# --- jieba 可选增强（CONDITIONAL：存在则用，不存在则回退规则分词）---
try:
    import jieba

    jieba.setLogLevel(20)  # 抑制 jieba 的 DEBUG 输出
    _JIEBA_AVAILABLE = True
except ImportError:
    _JIEBA_AVAILABLE = False


def _tokenize_jieba(text: str) -> list[str]:
    """使用 jieba 分词，过滤停用词和标点。"""
    import re
    words = jieba.cut(text)
    result: list[str] = []
    for w in words:
        w = w.strip()
        if not w or len(w) < 2:
            continue
        if w in _STOPWORDS or w in _CN_PUNCT:
            continue
        if re.match(r'^[\s，。！？；：""''（）【】《》…—～、·]+$', w):
            continue
        result.append(w)
    return result


def _segment_text(text: str) -> list[str]:
    """简单中文分词：优先 jieba，不存在时回退规则分词。"""
    if _JIEBA_AVAILABLE:
        try:
            return _tokenize_jieba(text)
        except Exception:
            pass  # jieba 异常时降级

    # 回退：规则分词
    import re
    blocks = re.findall(r'[\u4e00-\u9fff\uff00-\uffefA-Za-z0-9]+', text)
    tokens: list[str] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if re.match(r'^[A-Za-z0-9]+$', block):
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


def _extract_keywords(content: str) -> list[str]:
    """规则提取中文关键词。

    流程：
    1. 分词 → unigram/bigram/trigram
    2. 过滤停用词和标点
    3. 按词频排序
    4. 优先返回 bigram/trigram，其次 unigram
    5. 最多返回 10 个关键词
    """
    tokens = _segment_text(content)

    # 按长度分组
    multis = [t for t in tokens if len(t) >= 2]  # bigram/trigram
    singles = [t for t in tokens if len(t) == 1]  # unigram

    # 去重保持顺序
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


async def _llm_extract_keywords(content: str) -> list[str]:
    """LLM 提取关键词 + 话题标签 + 指代消解。

    使用 LLM_FAST_MODEL，超时 200ms 降级为规则结果。
    """
    prompt = f"""从以下用户消息中提取关键词(2-5个)、话题标签(1-3个)、指代消解。

消息: {content}

输出 JSON 格式:
{{"keywords": ["词1","词2"], "topic_tags": ["标签1"], "resolved_entities": {{"他":"张三"}}}}"""

    try:
        response = await asyncio.wait_for(
            _fast_client.chat.completions.create(
                model=LLM_FAST_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=128,
                temperature=0.1,
            ),
            timeout=1.0,
        )
        raw = response.choices[0].message.content or ""
        # 解析 JSON
        try:
            if "```json" in raw:
                raw = raw[raw.index("```json") + 7:]
                if "```" in raw:
                    raw = raw[:raw.index("```")]
            elif "```" in raw:
                raw = raw[raw.index("```") + 3:]
                if "```" in raw:
                    raw = raw[:raw.index("```")]
            data = json.loads(raw.strip())
            return data.get("keywords", [])
        except (json.JSONDecodeError, ValueError):
            return []
    except asyncio.TimeoutError:
        logger.debug("LLM 关键词提取超时，降级为规则结果")
        return []
    except Exception:
        return []


def _get_search_keywords(content: str) -> list[str]:
    """获取搜索关键词（同步入口）。

    规则: 规则提取 → 结果<3且消息>10字 → LLM 补全
    返回关键词列表。
    """
    keywords = _extract_keywords(content)

    if len(keywords) < 3 and len(content) > 10:
        # 需要 LLM 补全，但同步返回规则结果
        # LLM 补全在异步上下文调用
        return keywords  # 调用方在 retrieve_relevant 中处理 LLM 补全

    return keywords


async def _get_search_keywords_async(content: str) -> list[str]:
    """获取搜索关键词（异步入口，含 LLM 补全）。"""
    keywords = _extract_keywords(content)

    if len(keywords) < 3 and len(content) > 10:
        llm_kw = await _llm_extract_keywords(content)
        if llm_kw:
            # 合并去重，LLM 结果优先
            seen = set(keywords)
            merged = list(keywords)
            for kw in llm_kw:
                if kw not in seen:
                    seen.add(kw)
                    merged.append(kw)
            return merged[:10]

    return keywords


async def _assemble_system_prompt(
    user_id: str,
    is_new_session: bool,
    user_message: str = "",
) -> str:
    """组装 system prompt = persona + 相关记忆。

    实时从 memory_store 拉取，不缓存到 session 历史中。
    - 使用 retrieve_relevant 做语义检索，注入精选记忆
    - MEMORY_INJECT_MODE=off 时跳过记忆注入
    - MEMORY_INJECT_MODE=light 时仅注入 gist 层摘要
    - MEMORY_INJECT_MODE=full 时全量注入（旧行为）
    """
    from config import MEMORY_INJECT_MODE

    parts: list[str] = []

    # 1. 获取性格设定
    try:
        from memory_store import get as mem_get
        persona_entry = await mem_get("global/persona", "core")
        if persona_entry:
            parts.append(persona_entry["value"])
    except Exception:
        pass

    if not parts:
        parts.append(DEFAULT_SYSTEM_PROMPT)

    # 2. 记忆注入（根据模式决定）
    if MEMORY_INJECT_MODE == "off":
        return "\n\n".join(parts)

    try:
        from memory_store import retrieve_relevant

        if user_message and MEMORY_INJECT_MODE != "full":
            # 语义检索模式：提取关键词 → 检索 → 注入 top-5
            keywords = await _get_search_keywords_async(user_message)
            search_query = " ".join(keywords) if keywords else user_message
            memories = await retrieve_relevant(search_query, user_id)
            if memories:
                mem_lines = ["相关记忆:"]
                for m in memories:
                    try:
                        v = json.loads(m["value"])
                        summary = v.get("summary", str(v)[:80])
                    except (json.JSONDecodeError, TypeError):
                        summary = str(m["value"])[:80]
                    tags = f" [{', '.join(m['topic_tags'])}]" if m.get("topic_tags") else ""
                    mem_lines.append(f"- {summary}{tags}")
                parts.append("\n".join(mem_lines))
                logger.debug(
                    "记忆注入: user=%s, memories=%d",
                    user_id[:12], len(memories),
                )
        else:
            # 全量模式（旧行为，兼容）
            from memory_store import build_index
            index = await build_index(user_id)
            if index and index != "你的记忆索引: (空)":
                parts.append(index)
    except Exception:
        pass

    return "\n\n".join(parts)


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算。中文 1 字≈1 token，英文 1 词≈1.3 token。"""
    import re
    # 中文/日文/韩文字符
    cjk = len(re.findall(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]', text))
    # 去掉 CJK 后按空格分词（英文）
    rest = re.sub(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]', ' ', text)
    words = len(rest.split())
    return cjk + int(words * 1.3)


def _trim_context(messages: list[dict], max_tokens: int) -> list[dict]:
    """Token 感知的上下文修剪：保留 system 消息完整，从新到旧截断 chat 消息。"""
    system_msgs = [m for m in messages if m["role"] == "system"]
    chat_msgs = [m for m in messages if m["role"] in ("user", "assistant")]

    total = sum(_estimate_tokens(m["content"]) for m in system_msgs)
    kept = []
    for msg in reversed(chat_msgs):  # 从最新往前保留
        t = _estimate_tokens(msg["content"])
        if total + t > max_tokens:
            break
        kept.insert(0, msg)
        total += t

    trimmed = system_msgs + kept
    if len(chat_msgs) > len(kept):
        logger.warning(
            "上下文修剪: %d → %d 条消息, tokens ≈%d/%d",
            len(chat_msgs), len(kept), total, max_tokens,
        )
    return trimmed


async def _build_messages(
    session: Session,
    user_message: str,
    user_id: str,
) -> list[dict]:
    """构建发给 LLM 的完整 messages 数组。

    格式：[system: persona+记忆] + [纯净历史] + [user: 原始消息]
    """
    is_new = len(session.messages) == 0
    system_prompt = await _assemble_system_prompt(user_id, is_new, user_message)

    messages = [{"role": "system", "content": system_prompt}]
    messages += session.get_context()
    messages.append({"role": "user", "content": user_message})

    # Token 感知修剪（保留 system 完整，截断历史 chat 部分）
    if MAX_CONTEXT_TOKENS > 0:
        messages = _trim_context(messages, MAX_CONTEXT_TOKENS)

    return messages


# ==================== Lifecycle ====================

async def startup():
    """引擎启动：加载持久化会话、启动定期保存"""
    loaded = session_manager.load()
    if loaded:
        logger.info("已恢复 %d 个会话", loaded)
        for sid, s in session_manager._sessions.items():
            logger.info(
                "  会话 %s: %d 条消息, 最后活跃 %ds 前",
                sid[:12], len(s.messages),
                int(time.time() - s.last_active),
            )

    async def _auto_save():
        while True:
            await asyncio.sleep(300)  # 每 5 分钟保存
            n = session_manager.save()
            if n:
                logger.debug("自动保存: %d 个会话", n)

    asyncio.create_task(_auto_save())


async def shutdown():
    """优雅关闭：保存所有会话，停止调度器。"""
    n = session_manager.save()
    logger.info("已关闭，保存 %d 个会话", n)

    try:
        from reply_scheduler import get_scheduler
        scheduler = get_scheduler()
        await scheduler.stop()
    except Exception:
        pass


# ==================== Core API ====================

async def chat(
    session_id: str,
    user_message: str,
    system_prompt: str = "",
    temperature: float = 0.8,
    max_tokens: int = 512,
    role: str = "fast",
    metadata: Optional[dict] = None,
) -> dict:
    """发送消息并获取回复（辅脑/主脑可选）。

    engine 负责：
    - 从 memory_store 实时拉取 persona + 记忆索引
    - 组装完整上下文
    - 将原始消息（非组装后的 prompt）存入 session 历史

    Args:
        session_id: 会话 ID（QQ 场景下等于 user_id）
        user_message: 用户原始消息文本（不经任何预处理）
        system_prompt: 可选覆盖，为空时自动从 memory_store 获取
        temperature: 生成温度
        max_tokens: 最大输出 token
        role: "fast" (辅脑) 或 "strong" (主脑)
        metadata: QQ 消息元数据（可选，用于日后扩展）

    Returns:
        {"reply": str, "latency_ms": int, "session_id": str, "role": str}
    """
    t_start = time.monotonic()

    session = session_manager.get_or_create(session_id, system_prompt)

    # 构建上下文（实时拉取 persona + 记忆索引）
    try:
        messages = await _build_messages(session, user_message, session_id)
    except Exception:
        # memory_store 不可用时降级为纯历史模式
        logger.exception("上下文组装失败，降级为纯历史模式")
        messages = [{"role": "system", "content": session.system_prompt}]
        messages += session.get_context()
        messages.append({"role": "user", "content": user_message})

    # 存入 session 的是原始消息（纯净文本，不含记忆索引）
    session.add_message("user", user_message)

    api_client = _strong_client if role == "strong" else _fast_client
    model = LLM_STRONG_MODEL if role == "strong" else LLM_FAST_MODEL

    try:
        response = await asyncio.wait_for(
            api_client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            ),
            timeout=30,
        )
        reply = response.choices[0].message.content or ""
        reply = reply.strip()
    except asyncio.TimeoutError:
        reply = "[超时] LLM 未在 30s 内回复"
    except Exception as e:
        logger.exception("LLM 调用失败")
        reply = f"[错误] LLM 服务不可用: {e}"

    session.add_message("assistant", reply)
    latency_ms = int((time.monotonic() - t_start) * 1000)
    logger.info("%s reply: %dms, session=%s", role, latency_ms, session_id[:12])

    return {
        "reply": reply,
        "latency_ms": latency_ms,
        "session_id": session_id,
        "role": role,
    }


async def chat_with_evaluate(
    session_id: str,
    user_message: str,
    system_prompt: str = "",
) -> dict:
    """一站式：快速回复 + 异步双脑评估 + 追答。

    调用方应立即返回 fast_reply 给用户，然后轮询 evaluate 结果。

    Returns:
        {
            "fast_reply": str,
            "latency_ms": int,
            "session_id": str,
            "evaluation": dict | None,   # 同步返回 None，异步完成后有值
        }
    """
    import brain

    # 1. 快速回复
    result = await chat(
        session_id=session_id,
        user_message=user_message,
        system_prompt=system_prompt,
        role="fast",
    )
    fast_reply = result["reply"]

    # 2. 触发异步评估（fire-and-forget，完成后写入 session）
    async def _eval_and_update():
        try:
            decision = await brain.evaluate(
                session_id=session_id,
                user_message=user_message,
                fast_reply=fast_reply,
                system_prompt=system_prompt,
            )
            result["evaluation"] = decision
            session_manager.set_evaluation(session_id, decision)
        except Exception as e:
            logger.exception("异步评估失败")
            result["evaluation"] = {"should_follow_up": False, "reason": str(e)}

    asyncio.create_task(_eval_and_update())

    result["evaluation"] = None
    return result


# ==================== Session Queries ====================

async def get_evaluation(session_id: str) -> Optional[dict]:
    """获取会话的最新评估结果（轮询用）。"""
    return session_manager.get_evaluation(session_id)


async def get_session_info(session_id: str) -> dict:
    """获取会话信息。"""
    if session_id in session_manager._sessions:
        s = session_manager._sessions[session_id]
        return {
            "session_id": session_id,
            "message_count": len(s.messages),
            "created_at": s.created_at,
            "last_active": s.last_active,
            "expired": s.is_expired(),
        }
    return {"session_id": session_id, "message_count": 0, "exists": False}


async def delete_session(session_id: str) -> bool:
    """删除会话。"""
    session_manager.delete(session_id)
    return True


async def engine_status() -> dict:
    """引擎状态。"""
    return {
        "fast_model": LLM_FAST_MODEL,
        "strong_model": LLM_STRONG_MODEL,
        "base_url": LLM_BASE_URL,
        **session_manager.status(),
    }
