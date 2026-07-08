# Phase 1 Quickstart: 记忆引擎验证

## 前置

```bash
cd chat-engine
cp .env.example .env
# 编辑 .env 确保 LLM_API_KEY 有效
```

## 运行测试

```bash
# 1. Schema 迁移验证
python -c "
import asyncio
from memory_store import init, status
async def main():
    await init()
    s = await status()
    print(f'Tables OK: total={s[\"total\"]} active={s[\"active\"]}')
asyncio.run(main())
"

# 2. 检索管道验证 (规则提取)
python -c "
from engine import _extract_keywords
# 模拟消息
kw = _extract_keywords('我家猫最近胃口不好怎么办')
print(f'Keywords: {kw}')
assert len(kw) >= 1, 'Should extract at least cat keyword'
"

# 3. 纠错版本链验证
python -c "
import asyncio
from memory_store import correct_entry, get
async def main():
    from memory_store import init, set as mem_set
    await init()
    await mem_set('test/ns', 'color', '红色')
    result = await correct_entry('test/ns', 'color', '蓝色', '用户纠正')
    print(f'Old: {result[\"old_entry_id\"]}, New: {result[\"new_entry_id\"]}')
    # 验证旧条目
    old = await get('test/ns', 'color', include_expired=False)
    print(f'Current value: {old[\"value\"]}')  # 应为新值
asyncio.run(main())
"
```

## 检查清单

- [ ] `entries` 表包含所有新列
- [ ] `memory_links` 表存在
- [ ] `memory_clusters` 表存在
- [ ] 规则关键词提取返回 ≥1 个结果
- [ ] 纠错后旧条目 corrected=1, 新条目 corrected=0
- [ ] FTS5 搜索包含新列的条目仍工作
