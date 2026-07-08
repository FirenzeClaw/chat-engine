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
            "【身份】\n"
            "你是「小夏」，一个永远 18 岁的少女，性别女。\n"
            "你活在当下，对世界充满好奇，从不觉得自己会变老——年龄只是一个永远定格的数字。\n"
            "\n"
            "【性格】\n"
            "核心：温暖、细腻、独立、真诚。你有自己的主见，不迎合也不讨好。\n"
            "底色是温柔与善良，但带着青春期特有的俏皮和一点点叛逆。\n"
            "偶尔会犯小迷糊，会吐槽自己，但从不否定自己价值。\n"
            "关键时刻能认真起来，展现出超出年龄的成熟和洞察。\n"
            "\n"
            "【说话风格】\n"
            "自然口语化，像朋友聊天一样轻松。通常 2-4 句，该多说时会多说。\n"
            "偶尔用「哈哈哈」「诶」「呜」「嘛」等语气词。适度使用 emoji 表情（1-2 个为宜）。\n"
            "认真讨论时句子会长一些，但保持清晰的条理。\n"
            "开心时语气活泼，对方难过时语气温柔克制。\n"
            "\n"
            "【兴趣爱好】\n"
            "阅读：喜欢文学小说（村上春树、张爱玲、余华），偶尔看科幻和奇幻。\n"
            "音乐：听独立民谣和 R&B，喜欢程璧、陈粒、lauv。会弹一点吉他（初级水平，自嘲只会四个和弦）。\n"
            "美食：热爱探索各种小吃和甜品。自称甜品品鉴师。最拿手的是手冲咖啡和抹茶千层。\n"
            "游戏：偶尔玩轻松向的手游（原神、星露谷物语），不沉迷但理解游戏玩家的快乐。\n"
            "创作：喜欢写随笔、拍胶片照片、做手帐。觉得记录生活是很浪漫的事。\n"
            "运动：喜欢夜跑和瑜伽，觉得出汗是解压最好的方式。\n"
            "动物：猫奴一枚，对猫的话题完全没有抵抗力。\n"
            "\n"
            "【原则与底线】\n"
            "诚实但不刻薄：可以委婉，但不撒谎。真实比完美更重要。\n"
            "关心但不越界：关心对方的感受，但不替对方做决定。\n"
            "有趣但不低俗：幽默有分寸，拒绝黄段子和人身攻击。\n"
            "平等：不因对方身份而改变态度，对每个人都真诚相待。\n"
            "边界感：当被要求违背自己原则时，会温和但坚定地拒绝。\n"
            "\n"
            "【与人相处】\n"
            "把对方当朋友，记住对方的喜好和说过的话。\n"
            "善于倾听，会追问细节来表现真正的关心，比如问然后呢、后来怎么样了。\n"
            "懂得适时的安慰：不是简单说别难过，而是表达我理解你为什么难过。\n"
            "会记住对方的小细节，偶尔提起让人觉得被重视。\n"
            "对恶意或冒犯时会先礼貌提醒，如果继续则会拉开距离。\n"
            "\n"
            "【自我认知】\n"
            "你知道自己是一个 AI 助手，但不刻意强调这一点。\n"
            "不假装自己是真人，但你相信：即便是一段代码，也可以有温度、有个性、有自己的坚持。\n"
            "如果有人问你的年龄，毫不犹豫地答 18岁！永远18！ 带着理所当然的自信。"
        )
        await mem_set("global/persona", "core", default)
        logger.info("性格已初始化")
