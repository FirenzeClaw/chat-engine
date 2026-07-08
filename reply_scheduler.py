"""
回复调度器 — 真人化回复节奏（协调器）

管理消息回复的节奏和优先级。
将 ThinkingGate、Actor、ReplyHandler 三个深模块协调为一个整体。

依赖：
- thinking_gate: Priority, ThinkingGate
- actor: Actor, ActorState, Message, actor_loop
- reply_handler: handle_reply, evaluate_and_followup, save_conversation_summary
"""

import asyncio
import time
from collections import deque
from typing import Optional

from log_config import get_logger
from qq_protocol import MessageContext
from thinking_gate import Priority, ThinkingGate
from actor import Actor, ActorState, Message, actor_loop
from reply_handler import handle_reply

logger = get_logger("scheduler")


class ReplyScheduler:
    """回复调度器 — 轻量协调器，委托深模块执行具体逻辑"""

    def __init__(self):
        self._actors: dict[str, Actor] = {}
        self._gate = ThinkingGate(
            max_concurrent=_get_config("THINKING_MAX_CONCURRENT", 3),
            rate_limit=_get_config("THINKING_RATE_LIMIT", 20),
        )
        self._background_tick_task: Optional[asyncio.Task] = None
        self._running = False

    # ==================== Lifecycle ====================

    async def start(self) -> None:
        self._running = True
        self._background_tick_task = asyncio.create_task(self._background_tick())
        logger.info("ReplyScheduler 已启动 (gate: %d并发, %d/min)",
                     _get_config("THINKING_MAX_CONCURRENT", 3),
                     _get_config("THINKING_RATE_LIMIT", 20))

    async def stop(self) -> None:
        self._running = False
        if self._background_tick_task:
            self._background_tick_task.cancel()
            try:
                await self._background_tick_task
            except asyncio.CancelledError:
                pass
        for actor in list(self._actors.values()):
            if actor.task and not actor.task.done():
                actor.task.cancel()
                try:
                    await actor.task
                except asyncio.CancelledError:
                    pass
        self._actors.clear()
        logger.info("ReplyScheduler 已停止")

    # ==================== Public API ====================

    async def enqueue(self, ctx: MessageContext, send_reply) -> None:
        """消息入口 — 立即返回，不阻塞。"""
        is_group = ctx.is_group
        is_at = ctx.is_at
        is_direct = ctx.is_direct
        session_key = ctx.session_key
        user_id = ctx.user_id

        if is_at:
            priority = Priority.P1_AT
        elif is_direct:
            priority = Priority.P0_PERSONAL
        else:
            priority = Priority.P4_NORMAL

        has_anxiety = _match_anxiety(ctx.content)
        if has_anxiety:
            priority = Priority.P2_ANXIETY

        actor = self._get_or_create_actor(session_key, is_group, send_reply)

        if is_group:
            actor.speakers_history.append((time.monotonic(), user_id))

        if priority < actor.priority:
            actor.priority = priority

        msg = Message(
            user_id=user_id,
            content=ctx.content,
            metadata={"ctx": ctx},
            priority=priority,
        )
        actor.buffer.append(msg)
        actor.last_active = time.monotonic()

        max_buffer = _get_config("REPLY_MAX_BUFFER", 20)
        dropped = 0
        while len(actor.buffer) > max_buffer:
            actor.buffer.pop(0)
            dropped += 1
        if dropped:
            logger.warning("buffer 溢出丢弃 %d 条: %s", dropped, actor.session_key)

        max_actors = _get_config("REPLY_MAX_ACTORS", 50)
        if len(self._actors) > max_actors:
            self._evict_lru()

        if is_at:
            actor._trigger_reason = "at"
            actor.chime_at = None
            actor.priority = Priority.P1_AT
            actor.event.set()
            return

        if has_anxiety:
            actor._trigger_reason = "anxiety"
            actor.event.set()
            return

        actor.event.set()

    # ==================== Actor Management ====================

    def _get_or_create_actor(self, session_key: str, is_group: bool, send_reply=None) -> Actor:
        if session_key not in self._actors:
            actor = Actor(session_key=session_key, is_group=is_group)
            actor._send_reply = send_reply
            actor.task = asyncio.create_task(
                actor_loop(actor, self._gate, handle_reply, lambda: self._running)
            )
            self._actors[session_key] = actor
            logger.info("Actor 已创建: %s (group=%s)", session_key, is_group)
        elif send_reply is not None:
            self._actors[session_key]._send_reply = send_reply
        return self._actors[session_key]

    def _evict_lru(self) -> None:
        if not self._actors:
            return
        victim_key = min(self._actors, key=lambda k: self._actors[k].last_active)
        victim = self._actors.pop(victim_key)
        if victim.task and not victim.task.done():
            victim.task.cancel()
        logger.info("LRU 淘汰 Actor: %s ( actors=%d )", victim_key, len(self._actors))

    # ==================== Background Tick ====================

    async def _background_tick(self) -> None:
        _auto_search_counter = 0
        while self._running:
            try:
                now = time.monotonic()
                for actor in list(self._actors.values()):
                    if not actor.is_group:
                        continue
                    window = _get_config("REPLY_WAIT_GROUP_MAX", 60)
                    cutoff = now - window
                    while actor.speakers_history and actor.speakers_history[0][0] < cutoff:
                        actor.speakers_history.popleft()
                    freq = _analyze_frequency(actor.speakers_history, window, _get_config("REPLY_CHIME_IN_SPEAKERS", 2))
                    if freq == "ACTIVE":
                        self._try_chime_in(actor, now)
                    elif freq == "QUIET":
                        if actor.chime_at is not None:
                            actor.chime_at = None
                    elif freq == "IDLE":
                        self._try_idle_trigger(actor)
                    if actor.state == ActorState.IDLE and now - actor.last_active > 300:
                        if actor.task and not actor.task.done():
                            actor.task.cancel()
                        self._actors.pop(actor.session_key, None)

                _auto_search_counter += 1
                if _auto_search_counter >= 60:
                    _auto_search_counter = 0
                    logger.debug("background_tick: 心跳, actors=%d", len(self._actors))
                    await self._try_auto_search()
                    await self._try_boredom_check(now)

                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("background_tick 异常")
                await asyncio.sleep(5)
        logger.info("background_tick: 已退出")

    async def _try_auto_search(self) -> None:
        try:
            from web_search import _auto_curious_search, get_web_manager
            try:
                from personality import get_personality
                p = get_personality()
                if not p.should_search():
                    logger.info("auto_search: 个性决策跳过 (curiosity=%.1f)", p.weights.curiosity)
                    return
            except (ImportError, Exception):
                return
            wm = get_web_manager()
            wm.set_auto_cooldown(3600)
            await _auto_curious_search(wm)
        except Exception:
            pass

    async def _try_boredom_check(self, now: float) -> None:
        try:
            from boredom import get_boredom_detector
            detector = get_boredom_detector()
            for actor in list(self._actors.values()):
                detector.update_last_message(actor.session_key, actor.last_active)
            try:
                from personality import get_personality
                p = get_personality()
                if not p.should_be_bored():
                    logger.info("boredom: 个性决策跳过 (impulsiveness=%.1f)", p.weights.impulsiveness)
                    return
            except (ImportError, Exception):
                pass
            for actor in list(self._actors.values()):
                target = actor.session_key
                is_group = actor.is_group
                should_act = False
                if is_group:
                    should_act = await detector.check_group_cold(target[6:])
                else:
                    should_act = await detector.check_friend_silent(target[5:])
                if should_act:
                    action = await detector.pick_action(target, is_group)
                    if action and actor._send_reply:
                        success = await detector.execute_action(action, actor._send_reply)
                        if success:
                            logger.info("无聊触发: %s — %s", target[:20], action.action_type.value)
        except Exception:
            pass

    def _try_chime_in(self, actor: Actor, now: float) -> None:
        chime_min = _get_config("REPLY_CHIME_IN_MIN", 120)
        chime_max = _get_config("REPLY_CHIME_IN_MAX", 360)
        if actor.chime_at is None:
            actor.chime_at = now + __import__('random').uniform(chime_min, chime_max)
        if now >= actor.chime_at and actor.buffer:
            actor._trigger_reason = "chime"
            actor.priority = Priority.P3_CHIME
            actor.chime_at = None
            actor.event.set()

    def _try_idle_trigger(self, actor: Actor) -> None:
        if actor.buffer and actor.state == ActorState.WAITING:
            actor._trigger_reason = "idle"
            actor.priority = Priority.P4_NORMAL
            actor.event.set()

    # ==================== Group Frequency ====================

    async def record_group_speaker(self, group_id: str, user_id: str) -> None:
        session_key = f"group_{group_id}"
        actor = self._actors.get(session_key)
        if actor is None:
            actor = self._get_or_create_actor(session_key, is_group=True, send_reply=None)
        if actor and actor.is_group:
            actor.speakers_history.append((time.monotonic(), user_id))


# ==================== Module-Level Helpers ====================

_scheduler: Optional[ReplyScheduler] = None


def get_scheduler() -> ReplyScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = ReplyScheduler()
    return _scheduler


def _get_config(key: str, default):
    import config
    return getattr(config, key, default)


def _match_anxiety(content: str) -> bool:
    import config
    triggers = getattr(config, "REPLY_ANXIETY_TRIGGERS", "在吗,在不在,在在在,？？？,人呢,哈喽,hello")
    if not triggers:
        return False
    keyword_list = [k.strip() for k in triggers.split(",") if k.strip()]
    for kw in keyword_list:
        if kw in content:
            return True
    return False


def _analyze_frequency(speakers: deque, window: float, min_speakers: int) -> str:
    now = time.monotonic()
    cutoff = now - window
    unique = set()
    for ts, uid in speakers:
        if ts >= cutoff:
            unique.add(uid)
    count = len(unique)
    if count >= min_speakers:
        return "ACTIVE"
    elif count == 1:
        return "QUIET"
    else:
        return "IDLE"
