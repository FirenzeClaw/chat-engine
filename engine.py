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
import time
from typing import Optional

from openai import AsyncOpenAI

from config import (
    LLM_API_KEY, LLM_BASE_URL, LLM_FAST_MODEL, LLM_STRONG_MODEL,
    DEFAULT_SYSTEM_PROMPT, MAX_CONTEXT_TOKENS, LLM_REASONING_EFFORT,
    CONTEXT_COMPRESS_PCT, CONTEXT_RETIRE_PCT, CONTEXT_KEEP_RECENT,
)
from session import Session, SessionManager

from log_config import get_logger
logger = get_logger("engine")

session_manager = SessionManager()

# 模块级单例 AsyncOpenAI 实例（并发复用，减少连接开销）
_fast_client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
_strong_client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


# ==================== Context Assembly ====================

# 关键词提取已迁至 context_manager.extract_keywords_async


async def _assemble_system_prompt(
    user_id: str,
    is_new_session: bool,
    user_message: str = "",
    persona_level: str = "full",
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

    # 1. 获取分层性格设定（"core" / "full" / "eval"）
    try:
        from memory_store import get as mem_get
        persona_entry = await mem_get("global/persona", persona_level)
        if persona_entry:
            parts.append(persona_entry["value"])
    except Exception:
        pass

    # 2. 回退：分层不存在时尝试 core
    if not parts and persona_level != "core":
        try:
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
            keywords = await context_manager.extract_keywords_async(user_message)
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

            # Spec 003: 注入相关图片记忆
            try:
                from image_handler import retrieve_relevant_images
                if keywords:
                    image_query = " ".join(keywords[:3])
                    images = await retrieve_relevant_images(image_query, user_id)
                    if images:
                        img_lines = ["相关图片记忆:"]
                        for img in images[:3]:
                            desc = img.get("description", "")[:60]
                            opinion = img.get("opinion", "")[:40]
                            img_lines.append(f"- [{img.get('category', '')}] {desc}" + (f" ({opinion})" if opinion else ""))
                        parts.append("\n".join(img_lines))
            except Exception:
                pass

        else:
            # 全量模式（旧行为，兼容）
            from memory_store import build_index
            index = await build_index(user_id)
            if index and index != "你的记忆索引: (空)":
                parts.append(index)
    except Exception:
        pass

    return "\n\n".join(parts)


async def _build_messages(
    session: Session,
    user_message: str,
    user_id: str,
    image_urls: list[str] = None,
    persona_level: str = "full",
) -> list[dict]:
    """构建发给 LLM 的完整 messages 数组。

    格式：[system: persona+记忆] + [压缩/退役后历史] + [user: 文本/多模态]
    有图片时 user content 为 [{text}, {image_url}, ...] 多模态数组。
    通过 ContextManager 的三级保护防止静默截断。
    """
    import context_manager

    is_new = len(session.messages) == 0
    system_prompt = await _assemble_system_prompt(user_id, is_new, user_message, persona_level)

    messages = [{"role": "system", "content": system_prompt}]
    messages += session.get_context()

    # 构建最后一条 user message（有图片时用多模态格式）
    def _build_user_msg():
        if image_urls:
            uc = [{"type": "text", "text": user_message or "看一下这张图片"}]
            for url in image_urls:
                uc.append({"type": "image_url", "image_url": {"url": url}})
            return {"role": "user", "content": uc}
        return {"role": "user", "content": user_message}

    messages.append(_build_user_msg())

    # ContextManager 三级保护：估算 → 检查 → 压缩/退役
    ctx_mgr = context_manager.get_context_manager()
    total = context_manager.estimate_total_tokens(messages, [], [])
    ctx_status = context_manager.check_and_handle(
        messages, total,
        compress_pct=CONTEXT_COMPRESS_PCT,
        retire_pct=CONTEXT_RETIRE_PCT,
    )

    if ctx_status == "compressed":
        chat_msgs = [m for m in session.get_context() if m.get("role") in ("user", "assistant")]
        if len(chat_msgs) > CONTEXT_KEEP_RECENT * 2:
            compressed = await context_manager.compress_old_messages(
                chat_msgs, keep_recent=CONTEXT_KEEP_RECENT
            )
            messages = [{"role": "system", "content": system_prompt}] + compressed + [_build_user_msg()]
            logger.info("上下文已压缩: session=%s", session.session_id[:12])
    elif ctx_status == "retired":
        messages = context_manager.retire_to_minimal(messages, CONTEXT_KEEP_RECENT)
        messages = [{"role": "system", "content": system_prompt}] + messages[len([m for m in messages if m.get("role") == "system"]):] + [_build_user_msg()]
        logger.warning("上下文已退役: session=%s", session.session_id[:12])

    # context_manager 的压缩/退役已是最终保护，不再需要 _trim_context 二次修剪
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
    image_urls: list[str] = None,
    persona_level: str = "full",
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
        messages = await _build_messages(session, user_message, session_id, image_urls, persona_level)
    except Exception:
        # memory_store 不可用时降级为纯历史模式
        logger.exception("上下文组装失败，降级为纯历史模式")
        messages = [{"role": "system", "content": session.system_prompt}]
        messages += session.get_context()
        if image_urls:
            user_content = [{"type": "text", "text": user_message}]
            for url in image_urls:
                user_content.append({"type": "image_url", "image_url": {"url": url}})
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": user_message})

    # 存入 session 的是原始消息（纯净文本，不含记忆索引）
    session.add_message("user", user_message)

    api_client = _strong_client if role == "strong" else _fast_client
    model = LLM_STRONG_MODEL if role == "strong" else LLM_FAST_MODEL

    try:
        # 构建 API 参数
        api_kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # step 模型支持 reasoning_effort (low/medium/high)
        if LLM_REASONING_EFFORT:
            api_kwargs["reasoning_effort"] = LLM_REASONING_EFFORT

        response = await asyncio.wait_for(
            api_client.chat.completions.create(**api_kwargs),
            timeout=30,
        )
        choice = response.choices[0]
        reply = choice.message.content or ""
        # step-3.7-flash 推理模型可能用 reasoning_content
        if not reply and hasattr(choice.message, 'reasoning_content'):
            reply = choice.message.reasoning_content or ""
        reply = reply.strip()
        logger.info("LLM raw: finish=%s, content_len=%d, model=%s",
                     choice.finish_reason, len(reply), model)
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
