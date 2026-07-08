# 真人化回复节奏 — 完整设计

> 日期: 2026-07-08 | 状态: design-approved | 方案: C (Actor模式 + 主脑调度)

---

## 概述

当前 chat-engine 对每条消息立即调用 LLM 回复，缺乏「真人感」的回复节奏。本设计引入：

- **私聊防抖**：连续消息积累后一次性回复，支持焦虑词优先触发
- **群聊插话**：监控聊天频率，群友火热时随机插话，冷场时自然回复
- **主脑调度**：全局信号量 + 优先级队列，防止辅脑多开冲击 API/成本

---

## 一、Actor 状态机

每个会话（私聊用户 / 群聊）分配一个轻量 `asyncio.Task` Actor，独立状态机：

```
          ┌──────────┐
          │  空闲     │  ← 初始 / 冷却结束
          └─────┬─────┘
                │ 收到消息
                ▼
          ┌──────────┐
    ┌────→│  等待中   │←── 持续收到消息，追加 buffer，重置计时
    │     └─────┬─────┘
    │          │ 超时（私聊3-8s / 群聊15-60s）
    │          │ 或 焦虑词匹配（私聊立即触发）
    │          │ 或 随机插话到期（群聊）
    │          │ 或 群聊 @ 消息 → 打断冷却，立即触发
    │          ▼
    │     ┌──────────┐
    │     │  排队中   │  ThinkingGate.acquire(priority, timeout)
    │     └─────┬─────┘
    │          │ 获得许可
    │          ▼
    │     ┌──────────┐
    │     │  思考中   │  engine.chat(buffer) → 回复
    │     └─────┬─────┘
    │          │
    │          ▼
    │     ┌──────────┐
    │     │  冷却中   │  私聊5s / 群聊30s
    │     └─────┬─────┘
    │          │ 冷却结束 + buffer 有消息 → 回到"等待中"
    └──────────┘ 冷却结束 + buffer 空 → "空闲"
```

---

## 二、群聊频率分析

每秒 tick 一次，滑动窗口（= `wait_window`）内计算活跃发言人数：

```
状态判定:
  ACTIVE  — 发言人数 >= 2  → 群友在聊，随机插话可能触发
  QUIET   — 发言人数 == 1  → 延长等待
  IDLE    — 发言人数 == 0  → 触发思考回复
```

**随机插话算法（仅 ACTIVE 状态）：**

```
激活条件:
  - 当前状态 == ACTIVE
  - 上一个回复距今 > cooldown

触发时刻:
  首次进入 ACTIVE 时，在 [2min, 6min] 随机取一点 T
  到达 T 时若仍是 ACTIVE → 触发插话
  到达 T 前转为 QUIET/IDLE → 取消，走正常超时触发
```

---

## 三、主脑调度：ThinkingGate

防止 50 个 Actor 并发 LLM 调用冲击 API 限制和成本。

### 优先级队列

```
P0: 私聊消息（真人在等）      → 立即
P1: 群聊 @ 消息（被点名）     → 立即
P2: 私聊焦虑词触发            → ≤200ms
P3: 群聊插话（随机）          → 排队 ≤5s
P4: 群聊正常超时              → 排队 ≤10s
```

### 信号量 + 速率限制

```
max_concurrent = 3          # 同时最多 3 个 LLM 调用
global_rate    = 20/min     # 全局每分钟上限
```

超时未获许可 → 放弃本轮，清空 buffer，回到 WAITING 状态。

---

## 四、配置参数

```bash
# 私聊等待
REPLY_WAIT_PRIVATE_MIN=3          # 最短等待(s)
REPLY_WAIT_PRIVATE_MAX=8          # 最长等待(s)
REPLY_ANXIETY_TRIGGERS=在吗,在不在,在在在,？？？,人呢,哈喽,hello

# 群聊等待
REPLY_WAIT_GROUP_MIN=15           # 最短等待(s)
REPLY_WAIT_GROUP_MAX=60           # 最长等待(s)
REPLY_CHIME_IN_MIN=120            # 插话最短间隔(s) [2min]
REPLY_CHIME_IN_MAX=360            # 插话最长间隔(s) [6min]
REPLY_CHIME_IN_SPEAKERS=2         # 触发插话最少发言人数

# 冷却
REPLY_COOLDOWN_PRIVATE=5          # 私聊冷却(s)
REPLY_COOLDOWN_GROUP=30           # 群聊冷却(s)

# 调度器
REPLY_MAX_BUFFER=20               # 最大缓冲消息数
REPLY_MAX_ACTORS=50               # 最大并发 Actor 数

# 主脑调度
THINKING_MAX_CONCURRENT=3         # 最大并发 LLM 调用
THINKING_RATE_LIMIT=20            # 全局每分钟上限
THINKING_QUEUE_TIMEOUT_P3=5       # 群聊插话排队超时(s)
THINKING_QUEUE_TIMEOUT_P4=10      # 群聊普通排队超时(s)
```

---

## 五、文件结构

```
chat-engine/
├── reply_scheduler.py    # 新增: ReplyScheduler + Actor + ThinkingGate + 频率分析
├── orchestrator.py       # 修改: process_qq_message → 委托 reply_scheduler.enqueue()
├── config.py             # 修改: 新增 REPLY_* / THINKING_* 配置项
├── .env.example          # 修改: 新增配置说明
```

### reply_scheduler.py 内部结构

```
class Priority(Enum): P0_PERSONAL=0, P1_AT=1, P2_ANXIETY=2, P3_CHIME=3, P4_NORMAL=4

class ActorState(Enum): IDLE, WAITING, QUEUED, THINKING, COOLDOWN

@dataclass
class Actor:
    session_key: str
    is_group: bool
    buffer: list[dict]
    state: ActorState
    event: asyncio.Event
    last_active: float
    chime_at: float | None
    speakers_history: deque
    task: asyncio.Task | None

class ThinkingGate:
    _semaphore: asyncio.Semaphore
    _rate_limiter: ...          # token bucket (20/min)
    _queue: asyncio.PriorityQueue
    async def acquire(priority, timeout) → bool

class ReplyScheduler:
    _actors: dict[str, Actor]
    _gate: ThinkingGate

    async def enqueue(msg) → None
    async def _actor_loop(actor) → None      # 状态机主循环
    def _analyze_frequency(actor) → str       # → ACTIVE/QUIET/IDLE
    def _should_chime_in(actor) → bool        # 随机插话判定
    def _match_anxiety(content) → bool
    def _evict_lru() → None
```

---

## 六、边缘情况

| 场景 | 处理 |
|------|------|
| 等待窗口中反复撤回+重发 | 每条 append + reset_timer |
| 群聊 @ 时 Actor 在冷却 | 打断冷却 → 立即触发 |
| 思考中用户又发消息 | 思考完成检测 buffer → 跳过冷却直接下一轮 |
| Actor 超限 (50) | LRU 淘汰最久未活跃的 |
| engine.chat() 超时/失败 | 释放 Actor，下条消息重建 |
| 服务重启 | Actor 不持久化，重启从零开始 |
| ThinkingGate 排队超时 | 放弃本轮，清空 buffer，回 WAITING |
| 群聊仅一人自言自语 | 不进入 ACTIVE，走 QUIET→IDLE→触发 |

---

## 七、与现有系统的对接

| 位置 | 改动 |
|------|------|
| `main.py:on_qq_message()` | 不变 |
| `orchestrator.process_qq_message()` | 改为 `reply_scheduler.enqueue()`，移除直接 `await engine.chat()` |
| `orchestrator._async_handle()` | 移入 Actor：回复后做 `brain.evaluate()` + 追答 |
| `orchestrator._passive_observe()` | 保留，消息入 buffer 时立即写记忆 |

---

## 八、实现顺序

```
Phase 1: reply_scheduler.py 核心
  1.1 Actor 数据类 + 状态机
  1.2 ReplyScheduler.enqueue() + _actor_loop()
  1.3 私聊防抖 + 焦虑词检测
  1.4 群聊频率分析 + 随机插话

Phase 2: ThinkingGate
  2.1 全局 Semaphore + 优先级队列
  2.2 速率限制 (token bucket)

Phase 3: 对接
  3.1 orchestrator 改造
  3.2 config 配置项
  3.3 .env.example 更新
```
