# Quickstart: 回复调度器验证

## 前置

```bash
cd chat-engine
cp .env.example .env
```

## 单元测试

```bash
# 1. Actor 状态机
python -c "
import asyncio
from reply_scheduler import Actor, ActorState, Message

async def test():
    actor = Actor(session_key='test_u1', is_group=False)
    assert actor.state == ActorState.IDLE
    print('Actor state machine OK')
asyncio.run(test())
"

# 2. 焦虑词检测
python -c "
from reply_scheduler import _match_anxiety
assert _match_anxiety('在吗')
assert _match_anxiety('在在在？？？')
assert _match_anxiety('人呢！')
assert not _match_anxiety('你在干嘛呢')
print('Anxiety detection OK')
"

# 3. 频率分析
python -c "
from collections import deque
from reply_scheduler import _analyze_frequency
import time

speakers = deque()
speakers.append((time.monotonic(), 'u1'))
speakers.append((time.monotonic(), 'u2'))
result = _analyze_frequency(speakers, window=30, min_speakers=2)
assert result == 'ACTIVE'
print('Frequency analysis OK')
"

# 4. Token bucket 速率限制
python -c "
import asyncio
from reply_scheduler import ThinkingGate

async def test():
    gate = ThinkingGate(max_concurrent=3, rate_limit=20)
    assert await gate.acquire(0, timeout=0.1)
    print('Token bucket OK')
asyncio.run(test())
"
```

## 集成测试

```bash
# 私聊防抖：连续发 3 条消息，验证只在最后一条后 3-8s 触发一次回复
# 群聊插话：模拟 2 人发言，验证 2-6min 内触发插话
# 优先级：私聊 + 群聊 @ 同时到达，验证私聊先回复
```

## 检查清单

- [ ] 私聊消息 3-8s 窗口内防抖
- [ ] 焦虑词立即触发
- [ ] 群聊 @ 打断冷却
- [ ] 群聊频率分析正确（ACTIVE/QUIET/IDLE）
- [ ] 群聊随机插话在 2-6min 内
- [ ] ThinkingGate 并发 ≤3
- [ ] ThinkingGate 速率 ≤20/min
- [ ] Actor 超限 LRU 淘汰
