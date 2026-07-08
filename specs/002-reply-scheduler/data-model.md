# Reply Scheduler Data Model

## Entity: Actor

内存态，不持久化到 SQLite。

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_key` | `str` | `user_{uid}` 或 `group_{gid}` |
| `is_group` | `bool` | 是否为群聊 |
| `buffer` | `list[Message]` | 积压消息队列，FIFO |
| `state` | `ActorState` | IDLE / WAITING / QUEUED / THINKING / COOLDOWN |
| `event` | `asyncio.Event` | 唤醒等待中 Actor |
| `last_active` | `float` | `time.monotonic()` 最后活跃时间 |
| `chime_at` | `float \| None` | 随机插话触发时刻（monotonic） |
| `speakers_history` | `deque[(timestamp, user_id)]` | 滑动窗口发言记录 |
| `task` | `asyncio.Task \| None` | 当前运行的 Actor 协程 |

## Entity: Message

| 字段 | 类型 | 说明 |
|------|------|------|
| `user_id` | `str` | 发送者 QQ openid |
| `content` | `str` | 消息文本 |
| `metadata` | `dict` | 原始 msg_metadata |
| `received_at` | `float` | `time.monotonic()` |

## Entity: ThinkingGate

| 字段 | 类型 | 说明 |
|------|------|------|
| `_semaphore` | `asyncio.Semaphore` | 并发控制（默认 3） |
| `_bucket_tokens` | `float` | 当前 token bucket 余量 |
| `_bucket_last` | `float` | 上次补充时间 |
| `_max_tokens` | `int` | 桶容量（= THINKING_RATE_LIMIT） |
| `_refill_rate` | `float` | 补充速率（= RATE_LIMIT / 60） |

## State Transitions

```
IDLE → [enqueue] → WAITING
WAITING → [timeout | anxiety | chime | @] → QUEUED
QUEUED → [acquired] → THINKING
QUEUED → [timeout] → COOLDOWN (丢弃 buffer，防止紧循环重试)
THINKING → [reply sent] → COOLDOWN
COOLDOWN → [expired + buffer empty] → IDLE
COOLDOWN → [expired + buffer has messages] → WAITING
ANY → [@ message] → WAITING (immediate, skip COOLDOWN)
```
