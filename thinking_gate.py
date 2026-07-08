"""
ThinkingGate — 全局主脑调度门控

控制 API 并发（Semaphore）+ 速率（Token Bucket）+ 优先级队列。
从 reply_scheduler.py 提取为独立深模块。
"""

import asyncio
import time
from enum import IntEnum


class Priority(IntEnum):
    """回复优先级：小值优先（PriorityQueue 按第一个元素升序）"""
    P0_PERSONAL = 0   # 私聊（真人在等）
    P1_AT = 1         # 群聊 @（被点名）
    P2_ANXIETY = 2    # 含焦虑词（用户急）
    P3_CHIME = 3      # 群聊随机插话
    P4_NORMAL = 4     # 群聊自然触发（超时）


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

        1. P0/P1 (私聊/@) 跳过速率限制，仅受并发控制
        2. Token Bucket 检查 — 全局速率限制 (P2-P4)
        3. Semaphore 检查 — 并发控制 (所有优先级)
        4. 均通过后才真正"消费"token

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

        # P0/P1: 跳过速率限制，仅受并发控制
        skip_rate_limit = priority <= Priority.P1_AT

        if not skip_rate_limit:
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

        # Consume token (P0/P1 不消耗，留给低优先级消息用)
        if not skip_rate_limit:
            self._bucket_tokens -= 1.0
        return True

    def release(self):
        """释放信号量（调用 engine.chat() 完成后）"""
        self._semaphore.release()
