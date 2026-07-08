"""
QQ Bot 消息协调器 — chat-engine 独立版

直接将 QQ 消息流入 chat-engine，不经过 HTTP。
使用 engine.chat() 快速回复，brain.evaluate() 异步评估。

职责：
- 消息路由（QQ → engine → QQ）
- 异步评估调度
- 对话摘要保存到 memory_store
- **不负责 prompt 拼装**（上下文组装由 engine 内部完成）
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger("orch")
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("[orch] %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


async def process_qq_message(
    user_id: str,
    content: str,
    msg_metadata: dict,
    send_reply,
) -> str:
    """处理 QQ 消息：快速回复 + 异步评估。

    Args:
        user_id: QQ openid
        content: 用户原始消息文本
        msg_metadata: QQ 消息元数据 (group_id, channel_id 等)
        send_reply: async callable(reply_dict) — 发送 QQ 消息的回调

    Returns:
        快速回复文本
    """
    # 1. 辅脑快速回复（engine 内部自动组装 persona + 记忆索引）
    t_start = time.monotonic()
    try:
        result = await _chat_via_engine(
            session_id=user_id,
            user_message=content,
        )
        fast_reply = result["reply"]
    except Exception as e:
        fast_reply = f"[错误] 辅脑不可用: {e}"
    latency_ms = int((time.monotonic() - t_start) * 1000)
    logger.info("辅脑回复: %dms", latency_ms)
    if latency_ms > 500:
        logger.warning("辅脑回复延迟超标: %dms (SLO: <500ms)", latency_ms)

    # 2. 立即发送
    reply_data = {
        "type": "reply",
        "user_id": user_id,
        "content": fast_reply,
        "group_id": msg_metadata.get("group_id", ""),
        "channel_id": msg_metadata.get("channel_id", ""),
        "guild_id": msg_metadata.get("guild_id", ""),
        "ref_msg_id": msg_metadata.get("ref_msg_id", ""),
        "msg_type": msg_metadata.get("msg_type", ""),
    }
    try:
        await send_reply(reply_data)
    except Exception:
        logger.exception("发送快速回复失败")

    # 3. 异步评估 + 追答
    asyncio.create_task(_async_handle(
        user_id, content, fast_reply, send_reply, msg_metadata
    ))

    # 4. 保存对话摘要到 memory_store（含场景标记）
    try:
        from memory_store import set as mem_set

        # 提取场景信息
        msg_type = msg_metadata.get("msg_type", "")
        if "GROUP" in msg_type:
            source = "group"
            gid = msg_metadata.get("group_id", "")
        else:
            source = "private"
            gid = None

        summary = {
            "date": datetime.now(timezone.utc).isoformat(),
            "summary": f"用户: {content[:100]}; 回复: {fast_reply[:100]}",
            "topics": [],
            "message_count": 1,
            "source": source,
            "group_id": gid,
            "linked_private_session": None,
            "linked_group_session": None,
        }
        await mem_set(
            f"user/{user_id}/conversations",
            datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
            json.dumps(summary, ensure_ascii=False),
            source=source,
            group_id=gid,
            participants=json.dumps([user_id]),
        )
    except Exception:
        pass

    return fast_reply


async def _chat_via_engine(session_id: str, user_message: str) -> dict:
    """调用 engine.chat()，engine 内部自动组装上下文。"""
    from engine import chat
    return await chat(
        session_id=session_id,
        user_message=user_message,
    )


async def _async_handle(user_id, content, fast_reply, send_reply, msg_metadata):
    """异步评估 + 追答。"""
    try:
        from brain import evaluate as brain_eval

        # 获取 persona 用于评估
        persona_text = ""
        try:
            from memory_store import get as mem_get
            entry = await mem_get("global/persona", "core")
            if entry:
                persona_text = entry["value"]
        except Exception:
            pass

        decision = await brain_eval(
            session_id=user_id,
            user_message=content,
            fast_reply=fast_reply,
            system_prompt=persona_text,
        )

        if decision.get("should_follow_up"):
            follow_up_text = decision.get("follow_up_text", "")
            if follow_up_text:
                fu_data = {
                    "type": "reply",
                    "user_id": user_id,
                    "content": follow_up_text,
                    "group_id": msg_metadata.get("group_id", ""),
                    "channel_id": msg_metadata.get("channel_id", ""),
                    "guild_id": msg_metadata.get("guild_id", ""),
                    "ref_msg_id": msg_metadata.get("ref_msg_id", ""),
                    "msg_type": msg_metadata.get("msg_type", ""),
                }
                await send_reply(fu_data)
                logger.info("追答已发送: %s", user_id)

        # 记忆更新
        salience_score = decision.get("salience_score")
        for update in decision.get("memory_updates", []):
            try:
                action = update.get("action", "")
                ns = update.get("namespace", "")
                k = update.get("key", "")
                if not ns or not k:
                    continue
                from memory_store import set as mem_set, mark_expired, correct_entry
                if action == "expire":
                    await mark_expired(ns, k)
                elif action == "correct":
                    new_val = update.get("new_value", update.get("value", ""))
                    reason = update.get("reason", "brain correction")
                    await correct_entry(ns, k, new_val if isinstance(new_val, str) else json.dumps(new_val, ensure_ascii=False), reason)
                elif action in ("add", "update"):
                    v = update.get("value", "")
                    await mem_set(ns, k, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False),
                                  salience=salience_score)
            except Exception:
                pass
    except Exception:
        logger.exception("异步评估失败")


async def init_memory():
    """初始化记忆系统 + 性格。"""
    import memory_store
    from memory_store import init, get, set as mem_set
    await init()
    stats = await memory_store.status()
    logger.info("记忆库: total=%d active=%d", stats["total"], stats["active"])

    persona = await get("global/persona", "core")
    if persona is None:
        default = (
            "你是一个友好的 QQ 聊天伙伴，名字叫「小助手」。\n"
            "性格特点：温暖、幽默、善解人意，偶尔有点小调皮。\n"
            "说话风格：自然随意，2-4句话为宜，可以适当使用表情符号。\n"
            "原则：诚实但不刻薄，有趣但不低俗，关心但不越界。"
        )
        await mem_set("global/persona", "core", default)
        logger.info("性格已初始化")
