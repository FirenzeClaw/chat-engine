# Reply Scheduler Contract

## `get_scheduler()` → ReplyScheduler

获取全局单例调度器。

```python
from reply_scheduler import get_scheduler
scheduler = get_scheduler()
await scheduler.start()
```

## `scheduler.enqueue(user_id, content, metadata, send_reply)` → None

消息入口。立即返回，不阻塞。

```python
# 私聊
await scheduler.enqueue(
    user_id="abc123",
    content="你好呀",
    metadata={"msg_type": "DIRECT_MESSAGE_CREATE"},
    send_reply=send_qq_message,
)

# 群聊 @
await scheduler.enqueue(
    user_id="def456",
    content="小夏帮我看一下这个",
    metadata={"msg_type": "GROUP_AT_MESSAGE_CREATE", "group_id": "g1"},
    send_reply=send_qq_message,
)
```

### Behavior

| msg_type | 行为 |
|----------|------|
| `DIRECT_MESSAGE_CREATE` / `C2C_MESSAGE_CREATE` | 私聊防抖，3-8s 窗口 |
| `GROUP_AT_MESSAGE_CREATE` / `AT_MESSAGE_CREATE` | 立即触发（P1 优先级） |
| `MESSAGE_CREATE` (群聊) | 不经过 scheduler，由 `_passive_observe` 处理 |

### Priority Assignment

```
DIRECT/C2C          → Priority.P0  (真人在等)
AT_MESSAGE (@)      → Priority.P1  (被点名)
含焦虑词             → Priority.P2  (用户急)
群聊插话触发         → Priority.P3  (随机)
群聊超时触发         → Priority.P4  (自然)
```

### send_reply Callback

Actor 准备好回复后回调：

```python
async def send_reply(reply_data: dict) -> None:
    # reply_data = {"type": "reply", "user_id": ..., "content": ..., ...}
    await send_qq_message(reply_data)
```

## `scheduler.stop()` → None

优雅关闭：取消所有 Actor task，等待完成。

## Error Modes

| 场景 | 行为 |
|------|------|
| engine.chat() 超时 | 回复 "[超时]" → COOLDOWN → IDLE |
| engine.chat() 异常 | 回复 "[错误]" → COOLDOWN → IDLE |
| ThinkingGate.acquire() 超时 | 丢弃 buffer → COOLDOWN → IDLE |
| Actor 超限 (50) | LRU 淘汰 → 被淘汰的 Actor 消息丢失 |
