"""
Actor — 消息 Actor 状态机

管理单个会话（私聊 user_xxx 或群聊 group_xxx）的回复节奏。
从 reply_scheduler.py 提取为独立深模块。
"""

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from thinking_gate import Priority


class ActorState(Enum):
    """Actor 状态机"""
    IDLE = "idle"
    WAITING = "waiting"
    QUEUED = "queued"
    THINKING = "thinking"
    COOLDOWN = "cooldown"


@dataclass
class Message:
    """缓冲消息"""
    user_id: str
    content: str
    metadata: dict
    received_at: float = field(default_factory=time.monotonic)
    priority: Priority = Priority.P4_NORMAL


@dataclass
class Actor:
    """消息 Actor — 管理单个会话的回复节奏"""
    session_key: str
    is_group: bool
    buffer: list[Message] = field(default_factory=list)
    state: ActorState = ActorState.IDLE
    event: asyncio.Event = field(default_factory=asyncio.Event)
    last_active: float = field(default_factory=time.monotonic)
    chime_at: Optional[float] = None
    speakers_history: deque = field(default_factory=lambda: deque(maxlen=200))
    task: Optional[asyncio.Task] = None
    priority: Priority = Priority.P4_NORMAL
    _trigger_reason: str = ""
    _send_reply = None  # async callable(content: str)


def _get_config(key: str, default):
    """延迟导入配置，避免循环依赖"""
    import config
    return getattr(config, key, default)


async def actor_loop(actor: Actor, gate, reply_fn, running_check) -> None:
    """Actor 主循环 — 状态机驱动

    IDLE → WAITING → QUEUED → THINKING → COOLDOWN → IDLE/WAITING

    私聊 Actor 由 enqueue 事件驱动（每收到消息 event.set()）
    群聊 Actor 由 enqueue 事件 + _background_tick 共同驱动

    Args:
        actor: 要运行的 Actor
        gate: ThinkingGate 实例
        reply_fn: async callable(actor) — 执行回复的函数
        running_check: callable() -> bool — 检查是否仍在运行
    """
    import logging
    logger = logging.getLogger("scheduler")

    while running_check():
        try:
            # === IDLE: 等待首次消息 ===
            actor.state = ActorState.IDLE
            await actor.event.wait()
            actor.event.clear()

            while actor.buffer:
                # === 确定等待窗口 ===
                if actor.is_group:
                    wait_min = _get_config("REPLY_WAIT_GROUP_MIN", 15)
                    wait_max = _get_config("REPLY_WAIT_GROUP_MAX", 60)
                    cooldown_sec = _get_config("REPLY_COOLDOWN_GROUP", 30)
                else:
                    wait_min = _get_config("REPLY_WAIT_PRIVATE_MIN", 3)
                    wait_max = _get_config("REPLY_WAIT_PRIVATE_MAX", 8)
                    cooldown_sec = _get_config("REPLY_COOLDOWN_PRIVATE", 5)

                # === 检查跳过等待条件 ===
                skip_wait = False
                trigger = actor._trigger_reason

                if trigger == "at":
                    skip_wait = True
                elif trigger == "anxiety":
                    skip_wait = True
                elif trigger == "chime":
                    skip_wait = True
                elif trigger == "idle":
                    skip_wait = True

                if not skip_wait:
                    # === WAITING: 防抖窗口 ===
                    actor.state = ActorState.WAITING
                    wait_until = time.monotonic() + random.uniform(wait_min, wait_max)
                    logger.debug("WAITING: key=%s window=%.1fs queue=%d",
                                 actor.session_key, wait_until - time.monotonic(),
                                 len(actor.buffer))

                    while time.monotonic() < wait_until:
                        remaining = wait_until - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            await asyncio.wait_for(actor.event.wait(), timeout=remaining)
                            actor.event.clear()
                            wait_until = time.monotonic() + random.uniform(wait_min, wait_max)
                            trigger = actor._trigger_reason
                            if trigger in ("at", "anxiety"):
                                break
                        except asyncio.TimeoutError:
                            break

                # === 确定进入队列的优先级 ===
                if trigger == "at":
                    actor.priority = Priority.P1_AT
                elif trigger == "anxiety":
                    actor.priority = Priority.P2_ANXIETY
                elif trigger == "chime":
                    actor.priority = Priority.P3_CHIME
                elif trigger == "idle":
                    actor.priority = Priority.P4_NORMAL

                actor._trigger_reason = ""

                # === QUEUED: 排队等待 Gate ===
                actor.state = ActorState.QUEUED
                logger.debug("QUEUED: key=%s pri=%s", actor.session_key, actor.priority.name)

                if actor.priority == Priority.P0_PERSONAL:
                    gate_timeout = 5.0
                elif actor.priority <= Priority.P2_ANXIETY:
                    gate_timeout = 0.0
                elif actor.priority == Priority.P3_CHIME:
                    gate_timeout = float(_get_config("THINKING_QUEUE_TIMEOUT_P3", 5))
                else:
                    gate_timeout = float(_get_config("THINKING_QUEUE_TIMEOUT_P4", 10))

                acquired = await gate.acquire(actor.priority, gate_timeout)

                if not acquired:
                    logger.warning("Gate 获取超时: %s (priority=%s)",
                                   actor.session_key, actor.priority.name)
                    actor.buffer.clear()
                    actor.state = ActorState.COOLDOWN
                else:
                    try:
                        # === THINKING: 调用 LLM ===
                        actor.state = ActorState.THINKING
                        logger.info("THINKING: key=%s pri=%s queue=%d",
                                    actor.session_key, actor.priority.name,
                                    len(actor.buffer))
                        await reply_fn(actor)
                    finally:
                        actor.buffer.clear()
                        actor.state = ActorState.COOLDOWN
                        gate.release()

                # === COOLDOWN: 冷却期 ===
                actor.priority = Priority.P4_NORMAL
                c_start = time.monotonic()
                while time.monotonic() - c_start < cooldown_sec:
                    remaining = cooldown_sec - (time.monotonic() - c_start)
                    if remaining <= 0:
                        break
                    try:
                        await asyncio.wait_for(actor.event.wait(), timeout=remaining)
                        actor.event.clear()
                        if actor._trigger_reason == "at":
                            break
                    except asyncio.TimeoutError:
                        break

                # === 循环判断 ===
                if not actor.buffer:
                    actor.state = ActorState.IDLE
                    actor.priority = Priority.P4_NORMAL
                    break

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Actor loop 异常: %s", actor.session_key)
            await asyncio.sleep(1)
