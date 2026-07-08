"""
ReplyHandler — 回复编排模块

包含 _do_reply、_evaluate_and_followup、_save_conversation_summary。
从 reply_scheduler.py 提取为独立深模块。
"""

import asyncio
import json
import logging

from qq_protocol import MessageContext

logger = logging.getLogger("scheduler")


async def handle_reply(actor) -> None:
    """执行一次完整回复：engine.chat() + brain.evaluate() + 追答

    从 actor.buffer 中合并消息文本，调用 engine，发送回复。
    """
    from engine import chat as engine_chat

    # 合并 buffer 消息
    combined_text = "\n".join(
        m.content for m in actor.buffer
    )
    last_msg = actor.buffer[-1] if actor.buffer else None
    last_meta = last_msg.metadata if last_msg else {}

    # 提取 MessageContext
    ctx = last_meta.get("ctx")
    if ctx is None:
        logger.warning("handle_reply: no MessageContext in metadata, skipping")
        return

    raw_uid = ctx.raw_uid
    image_urls = ctx.image_urls if ctx.image_urls else None

    # Spec 003: 个性权重决策
    try:
        from personality import get_personality
        p = get_personality()
        if not p.should_reply(is_at=ctx.is_at or actor._trigger_reason == "at",
                              is_direct=ctx.is_direct or not actor.is_group):
            logger.info("个性决策: 不回复 — %s (sociability=%.1f)",
                         actor.session_key[:20], p.weights.sociability)
            return
    except (ImportError, Exception):
        pass

    send_reply = actor._send_reply
    async def _send(reply_content: str):
        try:
            if send_reply:
                await send_reply(reply_content)
        except Exception:
            logger.exception("发送回复失败: %s", actor.session_key)

    # Spec 003: 个性风格调制
    personality_style = {}
    try:
        from personality import get_personality
        p = get_personality()
        personality_style = p.reply_style()
    except (ImportError, Exception):
        personality_style = {"temperature": 0.8}

    try:
        # Step 1: personality 驱动搜索决策
        search_context = ""
        try:
            from personality import get_personality
            p = get_personality()
            if p.should_search(during_conversation=True):
                from web_search import search_and_summarize
                search_context = await search_and_summarize(combined_text[:50], check_limit=True)
                if search_context:
                    logger.info("个性驱动搜索: query=%s", combined_text[:30])
        except Exception:
            pass

        # Step 2: 图片消息 → 辅脑秒回 + 主脑异步看图
        if image_urls:
            # 纯图片（无文字）：跳过辅脑，直接用硬编码回复，避免推理模型输出思考内容
            if not combined_text or not combined_text.strip():
                await _send("收到图片啦，让我看看～")
            else:
                # 有文字 + 图片：辅脑回复文字内容（不带图片）
                try:
                    fast_result = await engine_chat(
                        session_id=raw_uid,
                        user_message=combined_text,
                        role="fast",
                        temperature=0.8,
                        max_tokens=128,
                        image_urls=None,
                        persona_level="core",
                    )
                    fast_reply = fast_result["reply"]
                    if not fast_reply.startswith("[SKIP]") and not fast_reply.startswith("[SEARCH:"):
                        await _send(fast_reply)
                        logger.info("辅脑秒回: %s", fast_reply[:40])
                except Exception:
                    await _send("收到图片啦，让我看看～")

            async def _main_brain_reply():
                try:
                    main_result = await engine_chat(
                        session_id=raw_uid,
                        user_message=combined_text or "请描述这张图片的内容",
                        role="strong",
                        temperature=personality_style.get("temperature", 0.8),
                        image_urls=image_urls,
                    )
                    main_reply = main_result["reply"]
                    if main_reply and not main_reply.startswith("[SKIP]") and not main_reply.startswith("[SEARCH:"):
                        await _send(main_reply)
                        logger.info("主脑图片回复: %s", main_reply[:40])
                        asyncio.create_task(evaluate_and_followup(
                            raw_uid, combined_text, main_reply, ctx, actor._send_reply
                        ))
                except Exception:
                    logger.exception("主脑图片回复失败")
            asyncio.create_task(_main_brain_reply())

        else:
            # 纯文本消息：辅脑直接回复
            user_msg = combined_text
            if search_context:
                user_msg = f"{combined_text}\n\n[查询结果]\n{search_context}"

            result = await engine_chat(
                session_id=raw_uid,
                user_message=user_msg,
                role="fast",
                temperature=personality_style.get("temperature", 0.8),
                image_urls=None,
                persona_level="core",
            )
            reply = result["reply"]

            reply_stripped = reply.strip()
            first_line = reply_stripped.split("\n")[0][:30]
            logger.info("LLM 回复: len=%d, first30=%s", len(reply), first_line)

            if first_line.startswith("[SKIP]"):
                logger.info("[SKIP] — 不回复 %s", actor.session_key[:20])
                return
            elif first_line.startswith("[SEARCH:"):
                await _send(reply.replace(first_line, "", 1).strip() or reply)
            else:
                await _send(reply)

            logger.info("回复已发送: %s (%dms)", actor.session_key[:20],
                         result.get("latency_ms", 0))

            asyncio.create_task(evaluate_and_followup(
                raw_uid, combined_text, reply, ctx, actor._send_reply
            ))

            asyncio.create_task(save_conversation_summary(
                raw_uid, combined_text, reply, ctx
            ))

    except Exception:
        logger.exception("engine.chat() 失败: %s", actor.session_key)
        try:
            await _send("[错误] 回复生成失败，请稍后再试")
        except Exception:
            pass


async def evaluate_and_followup(
    session_id: str, user_message: str, fast_reply: str,
    ctx: MessageContext, send_reply=None,
) -> None:
    """异步双脑评估 + 追答 + 记忆更新"""
    try:
        from brain import evaluate as brain_eval

        persona_text = ""
        try:
            from memory_store import get as mem_get
            entry = await mem_get("global/persona", "eval")
            if entry:
                persona_text = entry["value"]
        except Exception:
            pass

        decision = await brain_eval(
            session_id=session_id,
            user_message=user_message,
            fast_reply=fast_reply,
            system_prompt=persona_text,
        )

        if decision.get("should_follow_up"):
            follow_up_text = decision.get("follow_up_text", "")
            if follow_up_text:
                try:
                    if send_reply:
                        await send_reply(follow_up_text)
                    logger.info("追答已发送: %s", session_id[:12])
                except Exception:
                    logger.exception("发送追答失败")

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
                    await correct_entry(
                        ns, k,
                        new_val if isinstance(new_val, str) else json.dumps(new_val, ensure_ascii=False),
                        reason,
                    )
                elif action in ("add", "update"):
                    v = update.get("value", "")
                    await mem_set(
                        ns, k,
                        v if isinstance(v, str) else json.dumps(v, ensure_ascii=False),
                        salience=salience_score,
                    )
            except Exception:
                pass
    except Exception:
        logger.exception("异步评估失败: %s", session_id[:12])


async def save_conversation_summary(
    user_id: str, content: str, reply: str, ctx: MessageContext,
) -> None:
    """保存对话摘要到 memory_store"""
    try:
        from datetime import datetime, timezone
        from memory_store import set as mem_set

        source = "group" if ctx.is_group else "private"
        gid = ctx.group_id if ctx.is_group else None

        summary = {
            "date": datetime.now(timezone.utc).isoformat(),
            "summary": f"用户: {content[:100]}; 回复: {reply[:100]}",
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
