"""
无聊系统 — 群聊冷场 / 私聊静默主动破冰

Spec 003: 多模态自主 AI — Layer 3

特性:
- 群聊 30min 无人 → 随机破冰动作
- 私聊 2h 无互动 → 主动问候
- 严格频率限制 + 夜间静默 (00:00-07:00)
"""

import asyncio
import json
import random
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Awaitable

from config import (
    BOREDOM_GROUP_COLD_MIN,
    BOREDOM_FRIEND_SILENT_H,
    BOREDOM_GROUP_MAX_PER_DAY,
    BOREDOM_PRIVATE_MAX_PER_DAY,
    BOREDOM_NIGHT_START,
    BOREDOM_NIGHT_END,
    BOREDOM_COOLDOWN_MIN,
)

from log_config import get_logger
logger = get_logger("boredom")


# ==================== Enums & Dataclasses ====================

class BoredomActionType(str, Enum):
    GREET = "greet"               # 问候
    JOKE = "joke"                 # 笑话
    NEWS = "news"                 # 热搜/新闻
    WEATHER = "weather"           # 天气
    MEMORY_RECALL = "memory_recall"  # 翻记忆
    HOT_TOPIC = "hot_topic"       # 热门话题


@dataclass
class BoredomAction:
    """无聊触发动作"""
    action_type: BoredomActionType
    content: str = ""
    target: str = ""              # "user_{uid}" 或 "group_{gid}"
    trigger_reason: str = ""      # "cold_group" / "silent_friend" / "memory_trigger"
    sent_at: float = 0.0


# ==================== Template Helpers (memory_store-backed) ====================

_DEFAULT_GREET_TEMPLATES = [
    "大家早上好呀～☀️",
    "今天天气真好，适合出门逛逛！",
    "嘿，有人吗？感觉好安静啊～",
    "下午好！今天过得怎么样呀？",
    "晚上好～今天有什么新鲜事吗？",
    "周末快乐！大家都在干嘛呢？",
    "哈喽哈喽，有人在吗？",
]

_DEFAULT_JOKE_TEMPLATES = [
    "我刚想到一个冷笑话：为什么程序员总喜欢用 dark mode？因为 light attracts bugs！🐛",
    "听说了一个好玩的事：有人把'芝麻开门'说成了'芝麻关门'，结果自己进不去了 😂",
    "今日冷知识：考拉的大脑只占颅腔的 60%，剩下的空间都是液体。难怪它们看起来那么佛系 🐨",
]

_DEFAULT_MEMORY_TEMPLATES = [
    "说起来，上次你们聊的{tag}话题还挺有意思的～",
    "突然想起之前你们讨论过{tag}！",
    "对了，之前好像有人提过{tag}的事～",
]

_WARMED_UP = False


async def _warmup_templates():
    """首次启动时将默认模板写入 memory_store（仅写一次）。"""
    global _WARMED_UP
    if _WARMED_UP:
        return
    try:
        from memory_store import get as mem_get, set as mem_set
        existing = await mem_get("global/boredom", "templates")
        if existing is None:
            import json
            await mem_set(
                "global/boredom", "templates",
                json.dumps({
                    "greet": _DEFAULT_GREET_TEMPLATES,
                    "joke": _DEFAULT_JOKE_TEMPLATES,
                    "memory_recall": _DEFAULT_MEMORY_TEMPLATES,
                }, ensure_ascii=False),
            )
    except Exception:
        pass
    _WARMED_UP = True


async def _get_templates() -> dict:
    """从 memory_store 获取模板，不可用时回退默认值。"""
    await _warmup_templates()
    try:
        from memory_store import get as mem_get
        import json
        entry = await mem_get("global/boredom", "templates")
        if entry:
            return json.loads(entry["value"])
    except Exception:
        pass
    return {
        "greet": _DEFAULT_GREET_TEMPLATES,
        "joke": _DEFAULT_JOKE_TEMPLATES,
        "memory_recall": _DEFAULT_MEMORY_TEMPLATES,
    }


# ==================== BoredomDetector ====================

class BoredomDetector:
    """无聊检测器 — 冷场/静默检测 + 动作生成 + 频率限制"""

    def __init__(self):
        # 每个 actor 的最后消息时间
        self._last_message_time: dict[str, float] = {}
        # 每个 target 的发送计数器（滑动窗口）
        self._rate_counters: dict[str, list[float]] = {}
        # 每个 target 的冷却截止时间
        self._cooldowns: dict[str, float] = {}

    # ==================== Detection ====================

    def update_last_message(self, target: str, ts: float = 0):
        """更新目标最后活跃时间。

        Args:
            target: "user_{uid}" 或 "group_{gid}"
            ts: 时间戳，默认当前时间
        """
        self._last_message_time[target] = ts or time.monotonic()

    def _is_night(self) -> bool:
        """判断当前是否为夜间静默时段 (00:00-07:00)。"""
        now = datetime.now()
        hour = now.hour
        if BOREDOM_NIGHT_START < BOREDOM_NIGHT_END:
            return BOREDOM_NIGHT_START <= hour < BOREDOM_NIGHT_END
        else:
            # 跨午夜，如 22:00-07:00
            return hour >= BOREDOM_NIGHT_START or hour < BOREDOM_NIGHT_END

    async def check_group_cold(self, group_id: str) -> bool:
        """检查群聊是否冷场（>30min 无消息）。

        Args:
            group_id: 群 ID

        Returns:
            True 如果应该触发破冰
        """
        if self._is_night():
            return False

        target = f"group_{group_id}"
        last_ts = self._last_message_time.get(target)
        if last_ts is None:
            return False

        elapsed_min = (time.monotonic() - last_ts) / 60
        return elapsed_min >= BOREDOM_GROUP_COLD_MIN

    async def check_friend_silent(self, user_id: str) -> bool:
        """检查好友是否长时间静默（>2h 无互动）。

        仅检查频繁联系人（在 _last_message_time 中有记录的）。

        Args:
            user_id: 用户 ID

        Returns:
            True 如果应该触发破冰
        """
        if self._is_night():
            return False

        target = f"user_{user_id}"
        last_ts = self._last_message_time.get(target)
        if last_ts is None:
            return False

        elapsed_h = (time.monotonic() - last_ts) / 3600
        return elapsed_h >= BOREDOM_FRIEND_SILENT_H

    # ==================== Action Selection ====================

    async def pick_action(
        self, target: str, is_group: bool
    ) -> Optional[BoredomAction]:
        """根据场景选择破冰动作。

        Args:
            target: "user_{uid}" 或 "group_{gid}"
            is_group: 是否群聊

        Returns:
            BoredomAction 或 None（无可用动作）
        """
        # 频率限制检查
        if not self._is_action_allowed(target, is_group):
            return None

        # 根据场景加权选择动作类型
        if is_group:
            # 群聊倾向于问候/话题
            pool = [
                (BoredomActionType.GREET, 0.35),
                (BoredomActionType.HOT_TOPIC, 0.25),
                (BoredomActionType.JOKE, 0.15),
                (BoredomActionType.MEMORY_RECALL, 0.15),
                (BoredomActionType.NEWS, 0.10),
            ]
        else:
            # 私聊倾向于问候/翻记忆
            pool = [
                (BoredomActionType.GREET, 0.3),
                (BoredomActionType.MEMORY_RECALL, 0.3),
                (BoredomActionType.JOKE, 0.2),
                (BoredomActionType.NEWS, 0.1),
                (BoredomActionType.WEATHER, 0.1),
            ]

        # 加权随机选择
        action_types, weights = zip(*pool)
        chosen = random.choices(action_types, weights=weights, k=1)[0]

        trigger = "cold_group" if is_group else "silent_friend"
        return BoredomAction(
            action_type=chosen,
            target=target,
            trigger_reason=trigger,
        )

    # ==================== Execution ====================

    async def execute_action(
        self,
        action: BoredomAction,
        send_reply,
    ) -> bool:
        """执行破冰动作。

        Args:
            action: 要执行的动作
            send_reply: async callable(content: str) — 发送回复的回调

        Returns:
            True 如果成功发送
        """
        content = await self._generate_content(action.action_type, action.target)
        if not content:
            return False

        try:
            await send_reply(content)
            action.sent_at = time.monotonic()
            self._record_action(action.target)
            self._cooldowns[action.target] = time.monotonic() + BOREDOM_COOLDOWN_MIN * 60
            logger.info("破冰消息已发送: %s type=%s", action.target[:20], action.action_type.value)
            return True
        except Exception:
            logger.exception("破冰消息发送失败: %s", action.target)
            return False

    async def _generate_content(
        self, action_type: BoredomActionType, target: str
    ) -> str:
        """根据动作类型生成消息内容。

        Args:
            action_type: 动作类型
            target: 目标标识

        Returns:
            消息内容
        """
        templates = await _get_templates()
        greet_list = templates.get("greet", _DEFAULT_GREET_TEMPLATES)

        if action_type == BoredomActionType.GREET:
            return random.choice(greet_list)

        elif action_type == BoredomActionType.JOKE:
            joke_list = templates.get("joke", _DEFAULT_JOKE_TEMPLATES)
            return random.choice(joke_list)

        elif action_type == BoredomActionType.NEWS:
            try:
                from web_search import search_and_summarize
                summary = await search_and_summarize("今日热搜")
                if summary:
                    return f"刚看到今天的新闻：\n{summary[:300]}"
            except Exception:
                pass
            return random.choice(greet_list)  # 降级为问候

        elif action_type == BoredomActionType.WEATHER:
            try:
                from web_search import search_and_summarize
                summary = await search_and_summarize("今日天气")
                if summary:
                    return f"看了一下天气：\n{summary[:300]}"
            except Exception:
                pass
            return random.choice(greet_list)

        elif action_type == BoredomActionType.HOT_TOPIC:
            try:
                from web_search import search_and_summarize
                summary = await search_and_summarize("热门话题")
                if summary:
                    return f"最近大家都在聊这些：\n{summary[:300]}"
            except Exception:
                pass
            return random.choice(greet_list)

        elif action_type == BoredomActionType.MEMORY_RECALL:
            # 从模板 + 记忆库标签生成
            mem_templates = templates.get("memory_recall", _DEFAULT_MEMORY_TEMPLATES)
            try:
                from memory_store import get_top_tags
                tags = await get_top_tags(limit=5)
                if tags:
                    tag = random.choice(tags)
                    template = random.choice(mem_templates)
                    return template.replace("{tag}", tag)
            except Exception:
                pass
            return random.choice(greet_list)

        return random.choice(greet_list)

    # ==================== Rate Limiting ====================

    def _is_action_allowed(self, target: str, is_group: bool) -> bool:
        """检查频率限制。

        - 群聊: ≤3/day, ≥5min cooldown
        - 私聊: ≤1/day, ≥5min cooldown
        - 夜间: 不允许
        """
        if self._is_night():
            return False

        now = time.monotonic()

        # Cooldown 检查
        cd_until = self._cooldowns.get(target, 0)
        if now < cd_until:
            return False

        # 滑动窗口检查
        max_count = BOREDOM_GROUP_MAX_PER_DAY if is_group else BOREDOM_PRIVATE_MAX_PER_DAY
        window = 86400  # 24h

        timestamps = self._rate_counters.get(target, [])
        cutoff = now - window
        timestamps = [t for t in timestamps if t >= cutoff]

        if len(timestamps) >= max_count:
            return False

        return True

    def _record_action(self, target: str):
        """记录一次破冰动作。"""
        now = time.monotonic()
        if target not in self._rate_counters:
            self._rate_counters[target] = []
        self._rate_counters[target].append(now)
        # 清理过期
        cutoff = now - 86400
        self._rate_counters[target] = [t for t in self._rate_counters[target] if t >= cutoff]


# 模块级单例
_boredom_detector: Optional[BoredomDetector] = None


def get_boredom_detector() -> BoredomDetector:
    """获取全局单例 BoredomDetector。"""
    global _boredom_detector
    if _boredom_detector is None:
        _boredom_detector = BoredomDetector()
    return _boredom_detector
