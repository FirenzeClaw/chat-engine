# Tasks: 真人化回复节奏

> **Feature**: 002-reply-scheduler | **Input**: `plan.md`, `data-model.md`, `contracts/scheduler-api.md`, `research.md`
> **Created**: 2026-07-08

---

## Dependency Graph

```
Phase 2 (Setup: config + engine singles)
    │
    ├──► Phase 3 US-1 核心Actor+私聊防抖 ────────┐
    ├──► Phase 4 US-2 ThinkingGate               │ (并行)
    │                                            ▼
    └────────────────────────────► Phase 5 US-3 群聊频率+插话
                                      │
                                      ▼
                                 Phase 6 US-4 对接收尾
```

US-1 和 US-2 在 Setup 完成后可并行实现。US-3 依赖 US-1 的 Actor 基础设施。

---

## Phase 1: Setup

- [X] T001 [P] Add 16 REPLY_* / THINKING_* config entries to `config.py` per plan.md §2.2
- [X] T002 [P] Extract `_fast_client` and `_strong_client` module-level singletons in `engine.py` (per R3: reuse AsyncOpenAI instances)
- [X] T003 [P] Add config keys to `.env.example`

## Phase 2: US-1 — 核心 Actor + 私聊防抖

**Goal**: 私聊消息不再立即回复，而是等待 3-8s 窗口，连续消息积累后一次性回复。焦虑词如「在吗」立即触发。

**Independent Test**: 快速连续 3 条私聊消息 → 验证只在最后一条后触发一次 engine.chat()，且 buffer 含全部 3 条。

- [X] T004 [US1] Create `reply_scheduler.py`: define `class Priority(IntEnum)`, `class ActorState(Enum)`
- [X] T005 [US1] Create dataclasses `Message` and `Actor` in `reply_scheduler.py` per data-model.md
- [X] T006 [US1] Implement `ReplyScheduler` class skeleton in `reply_scheduler.py`: `__init__`, `get_scheduler()` module-level singleton
- [X] T007 [US1] Implement `ReplyScheduler._get_or_create_actor(session_key, is_group)` in `reply_scheduler.py`: create Actor for private sessions (`user_{uid}`, `is_group=False`)
- [X] T008 [US1] Implement `ReplyScheduler.enqueue(user_id, content, metadata, send_reply)` in `reply_scheduler.py`: wrap message as `Message`, append to actor buffer, `actor.event.set()`
- [X] T009 [US1] Implement `ReplyScheduler._actor_loop(actor, send_reply)` in `reply_scheduler.py`: state machine (IDLE→WAITING→THINKING→COOLDOWN→IDLE) per design §1
- [X] T010 [US1] Implement private wait window in `_actor_loop()`: random between `REPLY_WAIT_PRIVATE_MIN` and `REPLY_WAIT_PRIVATE_MAX`; reset timer on new message
- [X] T011 [US1] Implement `_match_anxiety(content)` in `reply_scheduler.py`: check content against `REPLY_ANXIETY_TRIGGERS` comma-separated list
- [X] T012 [US1] Wire anxiety detection in `enqueue()`: if anxiety match → set priority P2, skip wait, trigger immediately
- [X] T013 [US1] Implement cooldown in `_actor_loop()`: after reply, sleep `REPLY_COOLDOWN_PRIVATE` seconds; if buffer has new messages → goto WAITING, else IDLE
- [X] T014 [US1] Implement `ReplyScheduler.start()` in `reply_scheduler.py`: ensure background tick task is created
- [X] T015 [US1] Implement `max_buffer` cap in `enqueue()`: if `len(buffer) > REPLY_MAX_BUFFER`, drop oldest message

## Phase 3: US-2 — ThinkingGate 主脑调度

**Goal**: 全局信号量 + 优先级队列 + Token Bucket 速率限制，防止辅脑多开冲击 API。

**Independent Test**: 同时 5 个 Actor 进入 QUEUED → 验证最多 3 个并发，私聊优先于群聊。

- [X] T016 [US2] Implement `class ThinkingGate` in `reply_scheduler.py`: `__init__(max_concurrent, rate_limit)`, `_semaphore`, `_bucket_tokens`, `_bucket_last`
- [X] T017 [US2] Implement `ThinkingGate.acquire(priority, timeout)` in `reply_scheduler.py`: asyncio.Semaphore + token bucket check (per R1: `tokens = min(max, tokens + rate * elapsed)`)
- [X] T018 [US2] Implement priority queue dispatch in `acquire()`: use `asyncio.PriorityQueue` with `(priority, monotonic_timestamp, actor_key)` tuple (per R2)
- [X] T019 [US2] Integrate `ThinkingGate` into `_actor_loop()`: add QUEUED state, call `await gate.acquire(priority, timeout)` before entering THINKING
- [X] T020 [US2] Handle acquire timeout: if False → log warning, discard buffer, goto COOLDOWN
- [X] T021 [US2] Implement P0/P1 skip-queue in `enqueue()`: private/AT messages pass `timeout=0` to gate, group normal passes configured timeout

## Phase 4: US-3 — 群聊频率分析 + 随机插话

**Goal**: 群聊消息监控发言频率，ACTIVE 状态随机插话，IDLE 状态自然触发。

**Independent Test**: 模拟 2 人交替发言 → 验证进入 ACTIVE → 2-6min 内随机触发一次 engine.chat()。

- [X] T022 [US3] Implement `_analyze_frequency(actor)` in `reply_scheduler.py`: sliding window over `speakers_history` deque, window size = `REPLY_WAIT_GROUP_MAX` seconds (与群聊等待窗口共用), count unique speakers, return ACTIVE/QUIET/IDLE when speakers >= CHIME_IN_SPEAKERS / == 1 / == 0
- [X] T023 [US3] Implement `_background_tick()` in `reply_scheduler.py`: every 1s iterate all WAITING group actors (`is_group=True` only; private actors are driven by `enqueue`+`event`), update speakers_history (prune old entries), call `_analyze_frequency()`
- [X] T024 [US3] Implement `_should_chime_in(actor)` in `reply_scheduler.py`: on first ACTIVE detection, set `chime_at = monotonic + random(CHIME_IN_MIN, CHIME_IN_MAX)`; clear on QUIET/IDLE
- [X] T025 [US3] Wire chime-in trigger in `_background_tick()`: if `monotonic >= chime_at` and still ACTIVE → set `actor._trigger_reason = "chime"`, `actor.event.set()` (wakes `_actor_loop` which reads `_trigger_reason` to assign priority P3)
- [X] T026 [US3] Wire IDLE trigger in `_background_tick()`: if IDLE with buffered group messages → set `actor._trigger_reason = "idle"`, `actor.event.set()` (wakes `_actor_loop` which assigns P4)
- [X] T027 [US3] Wire @ message interrupt in `enqueue()`: if `is_at` → set priority P1, clear chime_at, force trigger (skip cooldown)
- [X] T028 [US3] Implement group wait window in `_actor_loop()`: random between `REPLY_WAIT_GROUP_MIN` and `REPLY_WAIT_GROUP_MAX`; reset on new message

## Phase 5: US-4 — 系统对接

**Goal**: orchestrator 改为委托 scheduler，main.py 初始化，配置文档更新。

**Independent Test**: 发送私聊消息 → 验证经过 scheduler 防抖后才回复，不再立即回复。

- [X] T029 [US4] Rewrite `orchestrator.process_qq_message()`: group normal messages still go to `_passive_observe`; @/direct/C2C messages call `scheduler.enqueue()` instead of `await engine.chat()` per plan.md §2.3
- [X] T030 [US4] Move `_async_handle()` logic (brain evaluation + follow-up) into `_evaluate_and_followup()` in `reply_scheduler.py`: after engine.chat() reply, call `brain.evaluate()` and send follow-up if needed
- [X] T031 [US4] Initialize ReplyScheduler in `main.py`: import `get_scheduler`, call `await scheduler.start()` before QQ loop
- [X] T032 [US4] Update `.env.example` with new REPLY_* / THINKING_* config section

## Phase 6: Polish

- [X] T033 Implement idle Actor cleanup in `_background_tick()`: remove actors with `state == IDLE` and `last_active > 300s` ago (per R4)
- [X] T034 Implement `ReplyScheduler.stop()` in `reply_scheduler.py`: cancel background tick, cancel all actor tasks, wait for completion
- [X] T035 Implement LRU eviction in `enqueue()`: if `len(_actors) > REPLY_MAX_ACTORS`, remove the actor with smallest `last_active`
- [X] T036 Wire `scheduler.stop()` into `engine.shutdown()` in `engine.py` for graceful exit

---

## Parallel Opportunities

```
Phase 1 (T001-T003): 3-way parallel (config.py / engine.py / .env.example)
Phase 2 (US-1) ←→ Phase 3 (US-2): 不同 class，可并行
Phase 4 (US-3): 依赖 US-1 Actor 基础设施
Phase 5 (US-4) ←→ Phase 6 (Polish) T033-T034: 可部分并行
```

## MVP Scope

最小可验证增量 = Setup + US-1 (私聊防抖): T001-T015，共 15 个任务。Bot 能做到私聊连续消息积累后一次性回复。

## Task Summary

| Phase | Tasks | Coverage |
|-------|:-----:|----------|
| Setup | T001-T003 | config + engine singleton |
| US-1 Actor+防抖 | T004-T015 | 私聊防抖、焦虑词、冷却 |
| US-2 ThinkingGate | T016-T021 | 信号量、优先级队列、速率限制 |
| US-3 群聊频率 | T022-T028 | 频率分析、随机插话、@ 打断 |
| US-4 对接 | T029-T032 | orchestrator + main.py + .env |
| Polish | T033-T036 | 清理、LRU、关闭 |
| **Total** | **36** | ✅ All Complete |
