# Phase 1 Research: 记忆引擎技术选型

## 决策记录

### R1: 艾宾浩斯遗忘曲线的离散化实现

**决策**: 分段函数, 非连续数学公式

**理由**:
- 连续公式在 SQLite 中难以高效查询（需 JOIN 访问日志计算精确间隔）
- 分段函数: 0-7天(保持), 7-30天(线性衰减), 30-60天(加速衰减), >60天(自动模糊化)
- SQLite 可用 `julianday('now') - julianday(created_at)` 做整数天数比较
- 覆盖 95% 的真实遗忘曲线形状

**替代**: tiktoken 精确 token 计数 — 但需额外依赖, 且对非英文支持差

### R2: Token 估算方法

**决策**: 规则估算 (CJK 1字≈1 token, 英文 1词≈1.3 token)

**理由**: 不需额外依赖, 精度 ±15% 对上下文修剪足够准确

### R3: FTS5 中文检索增强

**决策**: 保留现有 unicode61 tokenizer + LIKE 降级, 新增 jieba 可选增强

**理由**:
- unicode61 对中文分词弱, 但现有 LIKE 降级已覆盖
- jieba 需要额外依赖, 标记为 CONDITIONAL (存在时启用, 不存在时回退 LIKE)
- 可接受: 粗筛阶段漏掉部分中文匹配, 由 LLM 精排补救

### R4: 每日批量任务调度

**决策**: asyncio.create_task + sleep, 复用 main.py 的 daily_decay 模式

**理由**: 现有项目已用此模式, 不引入 Celery/APScheduler

### R5: LLM 精排 prompt 设计

**决策**: 单次调用, 输入候选记忆简要信息 + 当前消息, 输出 top-5 索引

**理由**: 减少 token 消耗, prompt 长度 <500 tokens
