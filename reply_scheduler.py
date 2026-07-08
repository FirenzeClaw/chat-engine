"""
回复调度器 — 真人化回复节奏

管理消息回复的节奏和优先级：
- US-1: Actor 状态机 + 私聊防抖（3-8s 窗口，焦虑词立即触发）
- US-2: ThinkingGate 全局信号量 + 优先级队列 + Token Bucket 速率限制
- US-3: 群聊频率分析 + 随机插话 + @ 打断

与现有系统的关系：
- orchestrator 将 @/私聊消息委托给 scheduler.enqueue()
- _actor_loop 内置 engine.chat() + brain.evaluate() + 追答
- main.py 启动时调用 scheduler.start()
"""

import asyncio
import json
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Optional

logger = logging.getLogger("scheduler")
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("[scheduler] %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ==================== Enums ====================

class Priority(IntEnum):
    """回复优先级：小值优先（PriorityQueue 按第一个元素升序）"""
    P0_PERSONAL = 0   # 私聊（真人在等）
    P1_AT = 1         # 群聊 @（被点名）
    P2_ANXIETY = 2    # 含焦虑词（用户急）
    P3_CHIME = 3      # 群聊随机插话
    P4_NORMAL = 4     # 群聊自然触发（超时）


class ActorState(Enum):
    """Actor 状态机"""
    IDLE = "idle"
    WAITING = "waiting"
    QUEUED = "queued"
    THINKING = "thinking"
    COOLDOWN = "cooldown"


# ==================== Dataclasses ====================

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
    """消息 Actor — 管理单个会话（私聊 user_xxx 或群聊 group_xxx）的回复节奏"""
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
    _trigger_reason: str = ""  # "chime" / "idle" / "at" / "anxiety" / ""
    _send_reply = None  # async callable(reply_dict) — 由 enqueue 注入


# ==================== ThinkingGate ====================

class ThinkingGate:
    """全局主脑调度门控

    控制 API 并发（Semaphore）+ 速率（Token Bucket）+ 优先级队列。
    """

    def __init__(self, max_concurrent: int = 3, rate_limit: int = 20):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_tokens = rate_limit
        self._bucket_tokens = float(rate_limit)
        self._bucket_last = time.monotonic()
        self._refill_rate = rate_limit / 60.0  # tokens per second

    async def acquire(self, priority: Priority, timeout: float) -> bool:
        """尝试获取执行许可。

        1. Token Bucket 检查 — 全局速率限制
        2. Semaphore 检查 — 并发控制
        3. 均通过后才真正"消费"token

        Returns:
            True 如果获得许可，False 如果超时。
        """
        # Token bucket refill
        now = time.monotonic()
        elapsed = now - self._bucket_last
        self._bucket_tokens = min(
            float(self._max_tokens),
            self._bucket_tokens + self._refill_rate * elapsed,
        )
        self._bucket_last = now

        # Check rate limit
        if self._bucket_tokens < 1.0:
            if timeout <= 0:
                return False
            # 等待 token 恢复
            wait_time = (1.0 - self._bucket_tokens) / self._refill_rate
            if wait_time > timeout:
                return False
            await asyncio.sleep(wait_time)
            # 重新 refill
            now = time.monotonic()
            elapsed = now - self._bucket_last
            self._bucket_tokens = min(
                float(self._max_tokens),
                self._bucket_tokens + self._refill_rate * elapsed,
            )
            self._bucket_last = now
            if self._bucket_tokens < 1.0:
                return False

        # Try semaphore
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            return False

        # Consume token
        self._bucket_tokens -= 1.0
        return True

    def release(self):
        """释放信号量（调用 engine.chat() 完成后）"""
        self._semaphore.release()


# ==================== ReplyScheduler ====================

class ReplyScheduler:
    """回复调度器核心"""

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
        """启动调度器：初始化背景 tick 任务"""
        self._running = True
        self._background_tick_task = asyncio.create_task(self._background_tick())
        logger.info("ReplyScheduler 已启动 (gate: %d并发, %d/min)",
                     _get_config("THINKING_MAX_CONCURRENT", 3),
                     _get_config("THINKING_RATE_LIMIT", 20))

    async def stop(self) -> None:
        """优雅关闭：取消所有任务"""
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

    async def enqueue(
        self,
        user_id: str,
        content: str,
        metadata: dict,
        send_reply,
    ) -> None:
        """消息入口 — 立即返回，不阻塞。

        Args:
            user_id: QQ openid
            content: 消息文本
            metadata: QQ 消息元数据 (msg_type, group_id 等)
            send_reply: async callable(reply_dict) — 发送回复的回调
        """
        msg_type = metadata.get("msg_type", "")
        is_group = "GROUP" in msg_type
        is_at = "AT_MESSAGE" in msg_type
        is_direct = "DIRECT" in msg_type or "C2C" in msg_type

        # 确定 session_key
        if is_group:
            session_key = f"group_{metadata.get('group_id', user_id)}"
        else:
            session_key = f"user_{user_id}"

        # 确定优先级
        if is_at:
            priority = Priority.P1_AT
        elif is_direct:
            priority = Priority.P0_PERSONAL
        else:
            priority = Priority.P4_NORMAL

        # 焦虑词检测
        has_anxiety = _match_anxiety(content)
        if has_anxiety:
            priority = Priority.P2_ANXIETY

        # 获取或创建 Actor，注入 send_reply 回调
        actor = self._get_or_create_actor(session_key, is_group, send_reply)

        # 记录群聊发言人（频率分析用）
        if is_group:
            actor.speakers_history.append((time.monotonic(), user_id))

        # 更新最高优先级
        if priority < actor.priority:
            actor.priority = priority

        # 创建消息并入队
        msg = Message(
            user_id=user_id,
            content=content,
            metadata=metadata,
            priority=priority,
        )
        actor.buffer.append(msg)
        actor.last_active = time.monotonic()

        # 缓冲上限：丢弃最旧消息
        max_buffer = _get_config("REPLY_MAX_BUFFER", 20)
        while len(actor.buffer) > max_buffer:
            actor.buffer.pop(0)

        # LRU 淘汰：Actor 超限
        max_actors = _get_config("REPLY_MAX_ACTORS", 50)
        if len(self._actors) > max_actors:
            self._evict_lru()

        # @ 打断处理：跳过冷却，立即触发
        if is_at:
            actor._trigger_reason = "at"
            actor.chime_at = None
            actor.priority = Priority.P1_AT
            actor.event.set()
            return

        # 焦虑词：跳过等待，立即触发
        if has_anxiety:
            actor._trigger_reason = "anxiety"
            actor.event.set()
            return

        # 普通入队：唤醒等待中的 actor
        actor.event.set()

    # ==================== Actor Management ====================

    def _get_or_create_actor(self, session_key: str, is_group: bool, send_reply=None) -> Actor:
        """获取或创建 Actor，如果 Actor 不存在则启动 _actor_loop"""
        if session_key not in self._actors:
            actor = Actor(session_key=session_key, is_group=is_group)
            actor._send_reply = send_reply
            actor.task = asyncio.create_task(self._actor_loop(actor))
            self._actors[session_key] = actor
            logger.debug("Actor 已创建: %s (group=%s)", session_key, is_group)
        elif send_reply is not None:
            # 仅在传入有效回调时更新（防止 record_group_speaker 的 None 覆盖已存回调）
            self._actors[session_key]._send_reply = send_reply
        return self._actors[session_key]

    def _evict_lru(self) -> None:
        """LRU 淘汰：移除 last_active 最小的 Actor"""
        if not self._actors:
            return
        victim_key = min(self._actors, key=lambda k: self._actors[k].last_active)
        victim = self._actors.pop(victim_key)
        if victim.task and not victim.task.done():
            victim.task.cancel()
        logger.debug("LRU 淘汰 Actor: %s", victim_key)

    # ==================== Actor Loop (State Machine) ====================

    async def _actor_loop(self, actor: Actor) -> None:
        """Actor 主循环 — 状态机驱动

        IDLE → WAITING → QUEUED → THINKING → COOLDOWN → IDLE/WATING

        私聊 Actor 由 enqueue 事件驱动（每收到消息 event.set()）
        群聊 Actor 由 enqueue 事件 + _background_tick 共同驱动
        """
        while self._running:
            try:
                # === IDLE: 等待首次消息 ===
                actor.state = ActorState.IDLE
                actor.event.clear()
                await actor.event.wait()

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
                        skip_wait = True  # idle 已经等了很久

                    if not skip_wait:
                        # === WAITING: 防抖窗口 ===
                        actor.state = ActorState.WAITING
                        wait_until = time.monotonic() + random.uniform(wait_min, wait_max)

                        while time.monotonic() < wait_until:
                            actor.event.clear()
                            remaining = wait_until - time.monotonic()
                            if remaining <= 0:
                                break
                            try:
                                await asyncio.wait_for(actor.event.wait(), timeout=remaining)
                                # 新消息到达 → 重置等待窗口
                                wait_until = time.monotonic() + random.uniform(wait_min, wait_max)
                                trigger = actor._trigger_reason
                                # 检查是否因焦虑/@ 应跳过等待
                                if trigger in ("at", "anxiety"):
                                    break
                            except asyncio.TimeoutError:
                                break  # 等待窗口超时

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

                    # 高优先级（P0-P2）不排队等待
                    if actor.priority <= Priority.P2_ANXIETY:
                        gate_timeout = 0.0
                    elif actor.priority == Priority.P3_CHIME:
                        gate_timeout = float(_get_config("THINKING_QUEUE_TIMEOUT_P3", 5))
                    else:
                        gate_timeout = float(_get_config("THINKING_QUEUE_TIMEOUT_P4", 10))

                    acquired = await self._gate.acquire(actor.priority, gate_timeout)

                    if not acquired:
                        logger.warning("Gate 获取超时: %s (priority=%s)",
                                       actor.session_key, actor.priority.name)
                        actor.buffer.clear()
                        actor.state = ActorState.COOLDOWN
                    else:
                        try:
                            # === THINKING: 调用 LLM ===
                            actor.state = ActorState.THINKING
                            await self._do_reply(actor)
                        finally:
                            actor.buffer.clear()
                            actor.state = ActorState.COOLDOWN
                            # 关键：释放 gate 信号量（在 finally 确保必定释放）
                            self._gate.release()

                    # === COOLDOWN: 冷却期 ===
                    actor.priority = Priority.P4_NORMAL  # 重置为最低优先级
                    actor.event.clear()
                    c_start = time.monotonic()
                    while time.monotonic() - c_start < cooldown_sec:
                        remaining = cooldown_sec - (time.monotonic() - c_start)
                        if remaining <= 0:
                            break
                        try:
                            await asyncio.wait_for(actor.event.wait(), timeout=remaining)
                            # 有新消息 → 冷却期间积累到 buffer，冷却后继续 WAITING
                            if actor._trigger_reason == "at":
                                break  # @ 打断冷却
                        except asyncio.TimeoutError:
                            break

                    # === 循环判断 ===
                    if not actor.buffer:
                        actor.state = ActorState.IDLE
                        actor.priority = Priority.P4_NORMAL
                        break  # 退出内层循环，回到 IDLE 等待

                    # buffer 还有消息 → 回到 WAITING
                    if actor.priority == Priority.P4_NORMAL:
                        # 如果没人设更高优先级，设为 P4
                        pass

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Actor loop 异常: %s", actor.session_key)
                await asyncio.sleep(1)  # 防止死循环

    async def _do_reply(self, actor: Actor) -> None:
        """执行一次完整回复：engine.chat() + brain.evaluate() + 追答

        从 actor.buffer 中合并消息文本，调用 engine，发送回复。
        """
        from engine import chat as engine_chat

        # 合并 buffer 消息
        combined_text = "\n".join(
            m.content for m in actor.buffer
        )
        # 取最后一条消息的 metadata 供 send_reply 使用
        last_meta = actor.buffer[-1].metadata if actor.buffer else {}

        # 提取 session_key 为 user_id（去掉前缀）
        raw_uid = actor.session_key
        for prefix in ("user_", "group_"):
            if raw_uid.startswith(prefix):
                raw_uid = raw_uid[len(prefix):]
                break

        user_id = last_meta.get("user_id", raw_uid)

        # 回调：发送回复
        send_reply = actor._send_reply
        async def _send(reply_content: str):
            try:
                reply_data = _build_reply_data(user_id, reply_content, last_meta)
                if send_reply:
                    await send_reply(reply_data)
            except Exception:
                logger.exception("发送回复失败: %s", actor.session_key)

        try:
            result = await engine_chat(
                session_id=raw_uid,
                user_message=combined_text,
                role="fast",
            )
            reply = result["reply"]
            await _send(reply)
            logger.info("回复已发送: %s (%dms)", actor.session_key[:20],
                         result.get("latency_ms", 0))
        except Exception:
            logger.exception("engine.chat() 失败: %s", actor.session_key)
            try:
                await _send("[错误] 回复生成失败，请稍后再试")
            except Exception:
                pass
            return

        # 异步评估 + 追答 + 记忆更新
        asyncio.create_task(self._evaluate_and_followup(
            raw_uid, combined_text, reply, last_meta, user_id, actor._send_reply
        ))

        # 保存对话摘要（沿用 orchestrator 逻辑）
        asyncio.create_task(self._save_conversation_summary(
            raw_uid, combined_text, reply, last_meta
        ))

    async def _evaluate_and_followup(
        self, session_id: str, user_message: str, fast_reply: str,
        msg_metadata: dict, user_id: str, send_reply=None,
    ) -> None:
        """异步双脑评估 + 追答 + 记忆更新（从 orchestrator._async_handle 迁移）"""
        try:
            from brain import evaluate as brain_eval

            persona_text = ""
            try:
                from memory_store import get as mem_get
                entry = await mem_get("global/persona", "core")
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

            # 追答
            if decision.get("should_follow_up"):
                follow_up_text = decision.get("follow_up_text", "")
                if follow_up_text:
                    try:
                        fu_data = _build_reply_data(user_id, follow_up_text, msg_metadata)
                        if send_reply:
                            await send_reply(fu_data)
                        logger.info("追答已发送: %s", session_id[:12])
                    except Exception:
                        logger.exception("发送追答失败")

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

    async def _save_conversation_summary(
        self, user_id: str, content: str, reply: str, msg_metadata: dict,
    ) -> None:
        """保存对话摘要到 memory_store（沿用 orchestrator 逻辑）"""
        try:
            from datetime import datetime, timezone
            from memory_store import set as mem_set

            msg_type = msg_metadata.get("msg_type", "")
            if "GROUP" in msg_type:
                source = "group"
                gid = msg_metadata.get("group_id", "")
            else:
                source = "private"
                gid = None

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

    # ==================== Background Tick (US-3) ====================

    async def _background_tick(self) -> None:
        """后台 tick：每 1s 分析群聊频率 + 清理空闲 Actor

        仅处理群聊 Actor（is_group=True），私聊 Actor 由 enqueue 驱动。
        """
        while self._running:
            try:
                now = time.monotonic()

                for actor in list(self._actors.values()):
                    if not actor.is_group:
                        continue

                    # 更新 speakers_history（清理过期条目）
                    window = _get_config("REPLY_WAIT_GROUP_MAX", 60)
                    cutoff = now - window
                    while actor.speakers_history and actor.speakers_history[0][0] < cutoff:
                        actor.speakers_history.popleft()

                    # 频率分析
                    freq = _analyze_frequency(actor.speakers_history, window, _get_config("REPLY_CHIME_IN_SPEAKERS", 2))

                    if freq == "ACTIVE":
                        self._try_chime_in(actor, now)
                    elif freq == "QUIET":
                        # 活跃度下降 → 取消插话计划
                        if actor.chime_at is not None:
                            actor.chime_at = None
                            logger.debug("插话取消 (QUIET): %s", actor.session_key)
                    elif freq == "IDLE":
                        self._try_idle_trigger(actor)

                    # 清理长时间 IDLE 的 Actor（300s）
                    if actor.state == ActorState.IDLE and now - actor.last_active > 300:
                        if actor.task and not actor.task.done():
                            actor.task.cancel()
                        self._actors.pop(actor.session_key, None)
                        logger.debug("清理空闲 Actor: %s", actor.session_key)

                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("background_tick 异常")
                await asyncio.sleep(5)

    def _try_chime_in(self, actor: Actor, now: float) -> None:
        """检查是否可以随机插话"""
        chime_min = _get_config("REPLY_CHIME_IN_MIN", 120)
        chime_max = _get_config("REPLY_CHIME_IN_MAX", 360)

        if actor.chime_at is None:
            # 首次检测到 ACTIVE → 设置随机插话时间
            actor.chime_at = now + random.uniform(chime_min, chime_max)
            logger.debug("插话计划: %s @ %.0fs 后", actor.session_key, actor.chime_at - now)

        if now >= actor.chime_at and actor.buffer:
            # 插话触发
            actor._trigger_reason = "chime"
            actor.priority = Priority.P3_CHIME
            actor.chime_at = None
            actor.event.set()
            logger.info("随机插话触发: %s", actor.session_key)

    def _try_idle_trigger(self, actor: Actor) -> None:
        """IDLE 状态触发：缓冲区有群聊消息时唤醒 Actor"""
        if actor.buffer and actor.state == ActorState.WAITING:
            # 如果已经有足够的群消息等待，触发自然回复
            # 只在 buffer 非空且等待时长已达一半以上时触发
            actor._trigger_reason = "idle"
            actor.priority = Priority.P4_NORMAL
            actor.event.set()
            logger.debug("IDLE 触发: %s (buffer=%d)", actor.session_key, len(actor.buffer))

    # ==================== Group Frequency ====================

    async def record_group_speaker(self, group_id: str, user_id: str) -> None:
        """记录群聊发言人（由 orchestrator 在处理群聊普通消息时调用）。

        如果群 Actor 不存在则自动创建（仅用于频率监控，不下发回复）。
        """
        session_key = f"group_{group_id}"
        actor = self._actors.get(session_key)
        if actor is None:
            actor = self._get_or_create_actor(session_key, is_group=True, send_reply=None)
        if actor and actor.is_group:
            actor.speakers_history.append((time.monotonic(), user_id))


# ==================== Module-Level Helpers ====================

_scheduler: Optional[ReplyScheduler] = None


def get_scheduler() -> ReplyScheduler:
    """获取全局单例调度器"""
    global _scheduler
    if _scheduler is None:
        _scheduler = ReplyScheduler()
    return _scheduler


def _get_config(key: str, default):
    """延迟导入配置，避免循环依赖"""
    import config
    return getattr(config, key, default)


def _build_reply_data(user_id: str, content: str, metadata: dict) -> dict:
    """构造统一格式的回复数据结构，消除 `_do_reply` 和 `_evaluate_and_followup` 中的重复."""
    return {
        "type": "reply",
        "user_id": user_id,
        "content": content,
        "group_id": metadata.get("group_id", ""),
        "channel_id": metadata.get("channel_id", ""),
        "guild_id": metadata.get("guild_id", ""),
        "ref_msg_id": metadata.get("ref_msg_id", ""),
        "msg_type": metadata.get("msg_type", ""),
    }


def _match_anxiety(content: str) -> bool:
    """检测消息是否含焦虑词"""
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
    """分析发言频率

    统计滑动窗口内不同发言人数量。

    Returns:
        "ACTIVE" — 发言人数 >= min_speakers
        "QUIET"  — 发言人数 == 1
        "IDLE"   — 发言人数 == 0
    """
    now = time.monotonic()
    cutoff = now - window
    # 统计窗口内的唯一发言人
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



