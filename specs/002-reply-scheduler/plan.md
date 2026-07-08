# Implementation Plan: 真人化回复节奏

> **Feature**: 002-reply-scheduler | **Input**: `docs/superpowers/specs/2026-07-08-reply-scheduler-design.md`
> **Created**: 2026-07-08

---

## Technical Context

| 维度 | 决策 |
|------|------|
| 语言 | Python 3.12 |
| 并发 | asyncio (Task + Event + Semaphore + PriorityQueue) |
| LLM SDK | openai >= 1.0 (AsyncOpenAI) — 现有 |
| 数据库 | aiosqlite — 现有（频率分析用内存 deque，不持久化） |
| 消息入口 | orchestrator.process_qq_message() → ReplyScheduler.enqueue() |
| 速率限制 | 自实现 token bucket（不引入第三方） |

## Constitution Check

| 规范 | 合规 | 说明 |
|------|:---:|------|
| 零 CLI 依赖 | ✅ | 全 asyncio 标准库，不引入新依赖 |
| 单进程入口 | ✅ | ReplyScheduler 为模块级单例，main.py 启动时初始化 |
| SQLite WAL 模式 | ✅ | 不变，内存状态不落盘 |
| 异步优先 | ✅ | 全部 async/await，Actor 为独立 Task |
| 模块职责单一 | ✅ | reply_scheduler 管理节奏，orchestrator 管理路由，互不侵入 |

## Gates

| 关卡 | 条件 | 状态 |
|------|------|:---:|
| G1 | 不阻塞消息接收主路径 | ✅ enqueue() 立即返回，Actor 后台运行 |
| G2 | 私聊延迟 <10s（用户感知） | ✅ 窗口 3-8s |
| G3 | API 并发可控 | ✅ ThinkingGate max 3 并发 + 20/min |
| G4 | 与现有代码侵入最小 | ✅ orchestrator 仅改 2 行调用 |

---

## Phase 1: Research

- R1: asyncio 速率限制实现 — token bucket 还是 sliding window
- R2: asyncio.PriorityQueue 的 priority 排序语义
- R3: engine.chat() 并发调用安全性（AsyncOpenAI 线程安全验证）

## Phase 2: Core Implementation

### 2.1: reply_scheduler.py 核心

**新增文件**: `reply_scheduler.py` (~350 行)

```
class Priority(IntEnum):
    P0_PERSONAL = 0
    P1_AT = 1
    P2_ANXIETY = 2
    P3_CHIME = 3
    P4_NORMAL = 4

class ActorState(Enum):
    IDLE = "idle"
    WAITING = "waiting"
    QUEUED = "queued"
    THINKING = "thinking"
    COOLDOWN = "cooldown"

@dataclass
class Message:
    user_id: str
    content: str
    metadata: dict
    received_at: float

@dataclass  
class Actor:
    session_key: str
    is_group: bool
    buffer: list[Message]
    state: ActorState
    event: asyncio.Event
    last_active: float
    chime_at: float | None
    speakers_history: deque[tuple[float, str]]
    task: asyncio.Task | None

class ThinkingGate:
    _semaphore: asyncio.Semaphore
    _bucket_tokens: float
    _bucket_last: float
    _max_tokens: int
    _refill_rate: float

    async def acquire(self, priority: Priority, timeout: float) -> bool

class ReplyScheduler:
    _actors: dict[str, Actor]
    _gate: ThinkingGate
    _background_tick_task: asyncio.Task | None

    async def start(self) -> None
    async def enqueue(self, user_id, content, metadata, send_reply) -> None
    async def _actor_loop(self, actor: Actor, send_reply) -> None
    def _get_or_create_actor(self, session_key, is_group) -> Actor
    def _analyze_frequency(self, actor: Actor) -> str
    def _should_chime_in(self, actor: Actor) -> bool
    def _match_anxiety(self, content: str) -> bool
    def _evict_lru(self) -> None
    async def _background_tick(self) -> None
```

### 2.2: config.py 新增配置

```python
REPLY_WAIT_PRIVATE_MIN = int(os.getenv("REPLY_WAIT_PRIVATE_MIN", "3"))
REPLY_WAIT_PRIVATE_MAX = int(os.getenv("REPLY_WAIT_PRIVATE_MAX", "8"))
REPLY_WAIT_GROUP_MIN = int(os.getenv("REPLY_WAIT_GROUP_MIN", "15"))
REPLY_WAIT_GROUP_MAX = int(os.getenv("REPLY_WAIT_GROUP_MAX", "60"))
REPLY_CHIME_IN_MIN = int(os.getenv("REPLY_CHIME_IN_MIN", "120"))
REPLY_CHIME_IN_MAX = int(os.getenv("REPLY_CHIME_IN_MAX", "360"))
REPLY_CHIME_IN_SPEAKERS = int(os.getenv("REPLY_CHIME_IN_SPEAKERS", "2"))
REPLY_COOLDOWN_PRIVATE = int(os.getenv("REPLY_COOLDOWN_PRIVATE", "5"))
REPLY_COOLDOWN_GROUP = int(os.getenv("REPLY_COOLDOWN_GROUP", "30"))
REPLY_MAX_BUFFER = int(os.getenv("REPLY_MAX_BUFFER", "20"))
REPLY_MAX_ACTORS = int(os.getenv("REPLY_MAX_ACTORS", "50"))
REPLY_ANXIETY_TRIGGERS = os.getenv("REPLY_ANXIETY_TRIGGERS", "在吗,在不在,在在在,？？？,人呢,哈喽,hello")

THINKING_MAX_CONCURRENT = int(os.getenv("THINKING_MAX_CONCURRENT", "3"))
THINKING_RATE_LIMIT = int(os.getenv("THINKING_RATE_LIMIT", "20"))
THINKING_QUEUE_TIMEOUT_P3 = int(os.getenv("THINKING_QUEUE_TIMEOUT_P3", "5"))
THINKING_QUEUE_TIMEOUT_P4 = int(os.getenv("THINKING_QUEUE_TIMEOUT_P4", "10"))
```

### 2.3: orchestrator.py 改造

`process_qq_message()` 简化为调度入口：

```python
async def process_qq_message(user_id, content, msg_metadata, send_reply) -> str:
    msg_type = msg_metadata.get("msg_type", "")
    is_group = "GROUP" in msg_type
    is_at = "AT_MESSAGE" in msg_type
    is_direct = "DIRECT" in msg_type or "C2C" in msg_type

    if is_group and not is_at and not is_direct:
        asyncio.create_task(_passive_observe(user_id, content, msg_metadata))
        return ""

    from reply_scheduler import get_scheduler
    scheduler = get_scheduler()
    await scheduler.enqueue(user_id, content, msg_metadata, send_reply)
    return ""
```

### 2.4: main.py 初始化

```python
from reply_scheduler import get_scheduler
scheduler = get_scheduler()
await scheduler.start()
```

---

## Phase 3: Config & Docs

- `.env.example` 新增配置段
- 添加集成测试：验证防抖、插话、优先级队列

---

## Risk Items

| 风险 | 概率 | 缓解 |
|------|:---:|------|
| engine.chat() 并发调用线程安全问题 | 低 | AsyncOpenAI 官方支持并发；测试验证 |
| Actor 泄漏（不释放） | 中 | idle 超时清理（5min 无活动 → 移除） |
| 群聊频率误判（2 人刷屏 vs 2 人闲聊） | 低 | 窗口可配置，默认 30s |
| orchestrator 改造破坏现有私聊 | 低 | 仅改入口，engine.chat() 调用逻辑不变 |
