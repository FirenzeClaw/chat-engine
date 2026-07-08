# Phase 0 Research: 回复调度器技术选型

## R1: 速率限制实现 — Token Bucket vs Sliding Window

**决策**: Token Bucket

**理由**:
- Token Bucket 允许短时突发（burst），适合"同时思考、然后等待"的 LLM 调用模式
- 实现简单：`tokens = min(max_tokens, tokens + rate * elapsed)` + `tokens -= 1`
- Sliding Window 需要存储每次请求时间戳，内存开销更大
- Token Bucket 是 asyncio 社区的速率限制标准方案

**替代**: 引入 `aiolimiter` 第三方库 → 拒绝，零 CLI 依赖原则

## R2: asyncio.PriorityQueue 排序语义

**决策**: 使用 `asyncio.PriorityQueue` + `IntEnum` 优先级

**理由**:
- `PriorityQueue` 按 tuple 第一元素排序，小值优先
- `IntEnum` 的 `P0_PERSONAL=0` 自然排在 `P4_NORMAL=4` 之前
- 同优先级条目按插入顺序（FIFO），符合公平性

**注意事项**: 不能直接用 `(priority, actor_key)` 作为队列元素排序 — 同 priority 时会按 actor_key 字符串排序破坏 FIFO。需用 `(priority, monotonic_timestamp, actor_key)`。

## R3: AsyncOpenAI 并发调用安全性

**决策**: 通过，官方支持

**理由**:
- `openai` >= 1.0 的 `AsyncOpenAI` 实例本身线程安全，支持并发 `chat.completions.create()`
- 同一 `AsyncOpenAI` 实例的多个并发请求由 `httpx` 连接池管理
- 项目中已使用 `AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)`，每个调用创建一次实例 → 改为模块级复用单例，减少连接开销

**实现变更**: `engine.py` 中提取 `_fast_client` 和 `_strong_client` 模块级单例

## R4: Actor 超时清理

**决策**: 后台 tick 任务每 60s 扫描，移除 last_active > 5min 的 IDLE Actor

**理由**:
- 防止群聊退群后 Actor 永久驻留
- 不持久化 → 无代价重建
