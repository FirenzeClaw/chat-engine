"""
QQ Bot 消息协调器 — chat-engine 独立版

直接将 QQ 消息流入 chat-engine，不经过 HTTP。
使用 reply_scheduler 管理回复节奏（私聊防抖 + 群聊频率 + 优先级队列）。

职责：
- 消息路由（QQ → reply_scheduler / 被动观察）
- 已委托 reply_scheduler 处理 engine.chat() + brain.evaluate() + 追答
- **不负责 prompt 拼装**（上下文组装由 engine 内部完成）
"""

import asyncio
import json
import logging
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
    """处理 QQ 消息：委托 reply_scheduler 管理回复节奏。

    Args:
        user_id: QQ openid
        content: 用户原始消息文本
        msg_metadata: QQ 消息元数据 (group_id, channel_id 等)
        send_reply: async callable(reply_dict) — 发送 QQ 消息的回调

    Returns:
        空字符串（回复由 scheduler 异步处理）。
    """
    msg_type = msg_metadata.get("msg_type", "")
    is_group = "GROUP" in msg_type
    is_at = "AT_MESSAGE" in msg_type
    is_direct = "DIRECT" in msg_type or "C2C" in msg_type

    # 群聊普通消息：仅旁听记录，不回复（避免噪音/延迟/成本）
    if is_group and not is_at and not is_direct:
        logger.debug("群聊普通消息，跳过回复: type=%s", msg_type)
        asyncio.create_task(_passive_observe(user_id, content, msg_metadata))
        # 记录发言人供频率分析用
        try:
            from reply_scheduler import get_scheduler
            await get_scheduler().record_group_speaker(
                msg_metadata.get("group_id", ""), user_id
            )
        except Exception:
            pass
        return ""

    # 私聊 / @ / C2C → 委托 reply_scheduler
    from reply_scheduler import get_scheduler
    scheduler = get_scheduler()
    await scheduler.enqueue(user_id, content, msg_metadata, send_reply)
    return ""


async def _passive_observe(user_id: str, content: str, msg_metadata: dict) -> None:
    """被动观察模式：群聊普通消息不做回复，仅记录到记忆系统。

    让 Bot 作为一个"旁听者"积累对群成员和话题的了解，
    在被 @ 时才能基于这些背景记忆做出更贴切的回复。
    """
    try:
        from memory_store import set as mem_set
        gid = msg_metadata.get("group_id", "")
        summary = json.dumps({
            "date": datetime.now(timezone.utc).isoformat(),
            "summary": f"[群聊观察] {content[:100]}",
            "topics": [],
            "source": "group",
            "group_id": gid,
        }, ensure_ascii=False)
        await mem_set(
            f"user/{user_id}/conversations",
            datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
            summary,
            source="group",
            group_id=gid,
            participants=json.dumps([user_id]),
        )
        logger.debug("被动观察已记录: %s, %s", user_id[:12], content[:30])
    except Exception:
        pass


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
