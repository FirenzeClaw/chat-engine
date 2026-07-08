# PRD: 多脑协调 QQ Bot — 记忆升级 + 异步追答

> 状态: `ready-for-agent` | 日期: 2026-07-08

---

## 问题陈述

当前 QQ Bot 只有单一 AI 模型做回复，交互扁平：一问一答、没有性格、没有回忆感、不会主动追答。用户想要一个"感觉像真人在思考"的聊天体验——回复快、有个性、记得过去、会说"对了还有……"。

## 解决方案

分两阶段实现：

**Phase A — 记忆基础设施升级**：用 SQLite 替代 JSON，实现三层命名空间记忆、惰性索引注入、QQ 社交信息采集。

**Phase B — 异步双主脑追答**：辅脑快速回复→立即发送，双主脑异步评估→必要时追加追答，同时管理长期记忆的更新/模糊化/遗忘。

---

## 用户故事

### Phase A

- 作为 Bot 用户，我希望 Bot 记得我们之前的对话内容，而不是每次都是陌生人
- 作为 Bot 用户，我希望 Bot 能用我的昵称称呼我，而不是一串 ID
- 作为开发者，我希望记忆系统支持模糊查询，而不是只能精确匹配 key
- 作为开发者，我希望记忆有版本管理和过期机制，可以标记旧记忆为"过时"
- 作为开发者，我希望配置加载只在一处完成，所有模块共享

### Phase B

- 作为 Bot 用户，我希望回复在 200ms 内出现，让我不觉得在等
- 作为 Bot 用户，我希望偶尔收到追加回复，像真人在思考后补充
- 作为 Bot 用户，我希望 Bot 偶尔纠正自己，而不是永远"正确"
- 作为开发者，我希望添加新 AI 后端只需实现一个接口

---

## 实现决策

### 记忆系统（Phase A）

**存储引擎**：SQLite，文件 `botuser/memory.db`，WAL 模式。

**表结构**：
```sql
CREATE TABLE memory_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  namespace TEXT NOT NULL,     -- "user/{uid}", "group/{gid}", "global"
  key TEXT NOT NULL,
  value TEXT NOT NULL,         -- JSON 字符串
  version INTEGER DEFAULT 1,
  expired INTEGER DEFAULT 0,
  created_at TEXT,
  updated_at TEXT,
  UNIQUE(namespace, key)
);
```

**命名空间设计**：
| 命名空间 | 内容 | 示例 |
|---------|------|------|
| `user/{uid}/profile` | 用户资料 | `{"nickname":"张三","notes":"喜欢猫"}` |
| `user/{uid}/facts` | 用户相关事实 | `{"key":"宠物","value":"养了一只橘猫"}` |
| `user/{uid}/conversations` | 对话摘要 | `[{"date":"...","summary":"聊了..."}]` |
| `global/bot_persona` | Bot 性格设定 | 双主脑的系统 prompt |

**新模块**：`memory_store.py`
- `get(namespace, key) -> dict|None`
- `set(namespace, key, value, expire=False)`
- `list(namespace_prefix) -> list[str]`
- `search(query, namespace) -> list[dict]` （全文搜索，sqlite FTS5）
- `status() -> dict`

**与现有 botuser.py 的关系**：`memory_store.py` 替代 `botuser.py` 的 JSON I/O。`botuser.py` 保留作为用户目录管理，但内部委托给 `memory_store`。

**惰性注入**（给辅脑的 system prompt）：
```
你的记忆索引: user/xxx/profile (昵称:张三), user/xxx/facts (3条),
user/xxx/conversations (最近5次摘要)
使用 memory_get() 按需读取详细内容。
```

### QQ 社交信息采集（Phase A）

新增 `social.py` 模块，通过 QQ REST API 获取：
- 用户昵称 → `GET /v2/users/{openid}` → 缓存到 `user/{uid}/profile`
- 群名称 → `GET /v2/groups/{group_openid}` → 缓存到 `group/{gid}/info`

首次交互时异步获取，后续从缓存读取。过期时间：昵称 24h，群名 1h。

### 辅脑路由（Phase B）

扩展 `ai_adapter.py`，新增 `DeepSeekAdapter`：
```python
class DeepSeekAdapter(AIAdapter):
    async def reply(self, user_id, prompt) -> str: ...
```

`AIAdapter` 接口不变。Orchestrator 根据策略选择适配器。

### 异步追答（Phase B）

新增 `orchestrator.py` 模块：
```python
async def process_message(msg: UnifiedMessage) -> ReplyMessage:
    # 1. 记忆检索 + 惰性注入
    context = await memory.inject(user_id)
    # 2. 辅脑快速回复
    reply = await fast_brain.reply(user_id, msg.content, context)
    # 3. 立即发送
    await send_qq_message(reply)
    # 4. 异步: 双主脑评估
    asyncio.create_task(_async_evaluate(msg, reply, user_id))

async def _async_evaluate(msg, reply, user_id):
    rational = await master_brain.evaluate(reply, "rational")
    emotional = await master_brain.evaluate(reply, "emotional")
    decision = _fuse(rational, emotional)
    if decision.should_follow_up:
        follow_up = await master_brain.follow_up(msg, reply, decision.reason)
        await send_qq_message(follow_up)
    if decision.memory_update:
        await memory.update(user_id, decision.memory_update)
```

**融合决策**（`_fuse`）：确定性规则 + LLM 轻量判断的混合。
- 规则：如果辅脑回复包含"我不知道"→ 高概率追答；如果回复 <10 字 → 高概率追答
- LLM：双主脑各输出 `{score: 0-10, should_follow_up: bool, reason: str, memory_update: dict|null}`，取加权平均

### 追答频率控制

QQ C2C 主动消息限制：每月 4 条/用户。追答使用被动回复（`ref_msg_id` 指向原消息），不受主动消息限制。但受"60 分钟内最多回复 5 次"限制——追答作为第 2 条回复，在限制内。

### 修改的文件

| 模块 | 变更类型 | 说明 |
|------|---------|------|
| `memory_store.py` | **新建** | SQLite 记忆存储 |
| `social.py` | **新建** | QQ 社交信息采集 |
| `orchestrator.py` | **新建** | 双脑协调器（Phase B） |
| `ai_adapter.py` | 修改 | 新增 DeepSeekAdapter |
| `botuser.py` | 修改 | 委托给 memory_store |
| `bridge.py` | 修改 | 替换为 orchestrator |
| `server.py` | 修改 | `send_qq_message` 支持异步追答 |
| `config.py` | 修改 | 新增记忆/社交配置项 |
| `requirements.txt` | 修改 | 移除 openai/cryptography 死依赖 |

### 不修改的文件

- `qq_protocol.py` — QQ WS 协议不变
- `schema.py` — 消息格式不变，可能新增 `FollowUpMessage` 类型
- `manage.sh` — 启动方式不变
- `index.html` — 前端不变

---

## 测试决策

**测试接缝**：
- `memory_store.py` — 纯函数式接口，独立单元测试（内存 SQLite）
- `AIAdapter.reply()` — mock 子进程/HTTP，验证适配器选择逻辑
- `orchestrator._fuse()` — 纯函数，规则逻辑 100% 可测
- `social.py` — mock aiohttp，验证缓存策略

**测试策略**：
- Phase A 先行：记忆模块独立测试通过后再接入 bridge
- 每条 rule 逻辑有对应 test case
- 集成测试：模拟一条完整消息流（QQ → orchestrator → reply → QQ），验证不丢消息

---

## 不在范围内

- 多模态输入/输出（图片、语音）
- 群组独立记忆空间（v2）
- 自定义模型训练/fine-tuning
- 其他 IM 平台（微信、Discord）
- 前端网页的大幅改版

---

## 补充说明

- Phase A 先做，预计改动 ~500 行，现有消息流不受影响
- Phase B 在 A 稳定后启动，核心风险点是异步追答的 QQ API 限频
- `kimi-debug-tunnel` 的惰性索引注入模式直接复用，但 NPM/SQLite 套件需本地 Python 实现（aiosqlite）
- 双主脑"融合决策"先不做纯 LLM 投票，用 rule + LLM 混合降低成本
