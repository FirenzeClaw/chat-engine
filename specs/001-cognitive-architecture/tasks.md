# Tasks: 认知架构 — Phase 1 记忆引擎

> **Feature**: 001-cognitive-architecture | **Phase**: 1/3 | **Status**: ✅ Complete
> **Completed**: 2026-07-08 | **Input**: `spec.md` (7 user stories), `plan.md` (8 sub-modules), `data-model.md`, `contracts/memory-api.md`

---

## Dependency Graph

```
Phase 2 (Foundational)
    │
    ├──► Phase 3 US-1 检索 ────────────────┐
    ├──► Phase 4 US-2 纠错                  │
    ├──► Phase 5 US-3 跨场景                 │
    │                                       ▼
    └────────────────────────────► Phase 6 US-6+US-7 关联+实体
                                        │
                                        ▼
                                   Phase 7 集群+Salience
```

US-1/US-2/US-3 在 Foundational 完成后可并行实现。US-6/US-7 依赖 US-1 的检索管道。

---

## Phase 1: Setup

- [X] T001 [P] Create migration infrastructure in `memory_store.py`: add `_run_migration()` function stub and call it from `init()` after table creation
- [X] T002 [P] Add Phase 1 config entries to `config.py`: `DECAY_GIST_DAYS=90`, `DECAY_DETAIL_DAYS=30`, `AUTO_MIGRATE_DAYS=60`, `ACCESS_BOOST_DAYS=7`, `ACCESS_BOOST_MIN=3`, `CLUSTER_TRIGGER_DAYS=14`, `CLUSTER_TRIGGER_MIN_ACCESS=3`, `MAX_RETRIEVAL_CANDIDATES=20`, `MAX_RETRIEVAL_RESULTS=5`
- [X] T003 [P] Add new config keys to `.env.example`

## Phase 2: Foundational — Schema Migration

- [X] T004 Add columns to `entries` table in `memory_store._run_migration()`: `memory_layer`, `decay_curve`, `decay_start`, `auto_migrate`, `salience`, `corrected`, `superseded_by`, `correction_reason`, `entity_type`, `topic_tags`, `about_person`, `source`, `group_id`, `participants`, `emotion_at_encoding`
- [X] T005 Create `memory_links` table in `memory_store._run_migration()`: from_id, to_id, relation_type, strength, source, created_at
- [X] T006 [P] Create `memory_clusters` table in `memory_store._run_migration()`: id, name, decay_curve_override, member_ids, created_at
- [X] T007 [P] Create `cluster_members` table in `memory_store._run_migration()`: cluster_id, entry_id, joined_at
- [X] T008 [P] Create `access_log` table in `memory_store._run_migration()`: id, entry_id, accessed_at, context
- [X] T009 Migrate existing data in `memory_store._run_migration()`: set `memory_layer='gist'` for conversations namespace entries, `memory_layer='detail'` for facts namespace entries
- [X] T010 Wire `_run_migration()` into `memory_store.init()` after CREATE TABLE statements with try/except for idempotency

## Phase 3: US-1 — Bot 根据当前话题提取相关记忆

**Goal**: 用户说"我家猫胃口不好"时，Bot 能从记忆中检索到"橘子"并注入上下文，而非泛泛回复

**Independent Test**: 调用 `retrieve_relevant("我家猫胃口不好", user_id)` 验证返回记忆包含"橘子"相关条目

- [X] T011 [US1] Implement `_extract_keywords()` in `engine.py`: rule-based Chinese keyword extraction using character-trigram segmentation and stopword filtering
- [X] T012 [US1] Implement `_llm_extract_keywords()` in `engine.py`: call LLM_FAST_MODEL to extract keywords + topic_tags + resolve entity references
- [X] T013 [US1] Add routing logic in `engine.py`: use `_extract_keywords()`, fallback to `_llm_extract_keywords()` when result <3 and message >10 chars
- [X] T014 [US1] Implement `retrieve_relevant()` in `memory_store.py`: FTS5 MATCH with namespace filter → top-20 candidates per `MAX_RETRIEVAL_CANDIDATES`
- [X] T015 [US1] Implement `_llm_rank_memories()` in `memory_store.py`: LLM_FAST_MODEL rank candidates to top-5; 1s timeout with FTS5 rank fallback
- [X] T016 [US1] Wire retrieval into `engine._assemble_system_prompt()`: call `retrieve_relevant()`, inject top-5 as "相关记忆:" section in system prompt
- [X] T017 [US1] Add retrieval logging in `engine.py`: latency, candidate count, selected count at INFO level

## Phase 4: US-2 — Bot 纠正记忆并记住纠错历史

**Goal**: 用户纠正后旧记忆不被删除，新记忆标记为 corrected chain

**Independent Test**: `correct_entry("user/X/facts", "宠物名", "橘子", "用户纠正")` → 验证旧条目 corrected=1 superseded_by=新ID

- [X] T018 [US2] Implement `correct_entry()` in `memory_store.py` per `contracts/memory-api.md`: mark old entry corrected=1, write new entry with salience+3, create corrected_by link
- [X] T019 [US2] Handle `action='correct'` in `orchestrator._async_handle()`: extract old/new value from brain memory_update, call `memory_store.correct_entry()`
- [X] T020 [US2] Filter corrected in `memory_store.build_index()`: default WHERE corrected=0, include corrected=1 when query has "以前"/"纠正" keywords
- [X] T021 [US2] Set `decay_curve='none'` on corrected entries in `memory_store.correct_entry()`: corrected memories never decay

## Phase 5: US-3 — Bot 在群聊和私聊中独立维护记忆

**Goal**: 私聊/群聊记忆独立存储，场景权重影响检索排序

**Independent Test**: 写入私聊+群聊记忆，私聊检索验证私聊记忆排前

- [X] T022 [US3] Extract `source`/`group_id` from `msg_metadata` in `orchestrator.process_qq_message()`
- [X] T023 [US3] Pass `source`/`group_id`/`participants` to `memory_store.set()` in `orchestrator.process_qq_message()`
- [X] T024 [US3] Implement scene weighting in `memory_store.retrieve_relevant()`: same scene ×1.0, private ×0.8, other group ×0.6
- [X] T025 [US3] Add `linked_private_session`/`linked_group_session` to summary JSON in `orchestrator.process_qq_message()`

## Phase 6: US-6 + US-7 — 记忆关联图 + 实体检索

**Goal**: 记忆间自动建边+检索扩散；entity_type/topic_tags 标记+多跳检索

**Independent Test**: 两条猫记忆 → 检索"兽医"验证扩散到猫记忆；about_person 过滤跨人检索

- [X] T026 [US6] Implement rule-based linking in `memory_store.set()`: create `same_day` edges for same-namespace+same-date entries in `memory_links` table
- [X] T027 [US6] Implement `_daily_link_scan()` in `memory_store.py`: discover `same_topic`/`extends`/`contradicts` edges via LLM_FAST_MODEL batch
- [X] T028 [US6] Schedule `_daily_link_scan()` in `main.py` alongside daily decay, 1h offset
- [X] T029 [US6] Add spreading activation to `memory_store.retrieve_relevant()`: follow memory_links from top-5, add ≤2 linked entries
- [X] T030 [US7] Implement rule-based entity pre-tagging in `memory_store.set()`: detect entity_type by namespace, extract topic_tags by keyword matching on value JSON
- [X] T031 [US7] Implement `_daily_batch_tag()` in `memory_store.py`: LLM_FAST_MODEL batch-tag unlabeled entries with entity_type/topic_tags/about_person
- [X] T032 [US7] Schedule `_daily_batch_tag()` in `main.py`, 2h offset from link scan
- [X] T033 [US7] Implement multi-hop retrieval in `memory_store.retrieve_relevant()`: about_person → topic_tags → related persons → max 3 hops
- [X] T034 [US7] Add social relationship edges in `memory_store._daily_batch_tag()`: social_colleague/social_friend/social_family via LLM inference

## Phase 7: Polish — Salience + 集群 + 衰减

**Goal**: 记忆重要性加权、深刻记忆集群、艾宾浩斯衰减集成

- [X] T035 Add `salience_score` field to `brain.evaluate()` return: rational+emotional importance (0-10)
- [X] T036 Wire salience in `orchestrator._async_handle()`: pass `salience_score` to `memory_store.set()` when saving conversations/facts
- [X] T037 Implement decay curves in `memory_store.apply_decay()`: detail 0-7d keep, 7-30d linear, 30-60d accelerated, >60d auto_migrate=1; gist 30d default with salience modulation; **FR-2.4 simple boost**: entries accessed ≥3 times within 7 days → extend `decay_start` by 15 days
- [X] T038 Implement auto-migration in `memory_store.apply_decay()`: auto_migrate=1 → `memory_layer='gist'`, convert timestamp to time period, append `[已模糊化]`
- [X] T039 Implement `_check_cluster_trigger()` in `memory_store.py`: query access_log for same topic_tags entries accessed ≥CLUSTER_TRIGGER_MIN_ACCESS times within CLUSTER_TRIGGER_DAYS
- [X] T040 Implement `_llm_confirm_cluster()` in `memory_store.py`: LLM validate semantic deep association for candidates
- [X] T041 Write confirmed clusters to `memory_clusters`/`cluster_members` in `memory_store.py`; update member entries' `decay_curve` to 'deep'
- [X] T042 Schedule cluster checks in `main.py`: daily `_check_cluster_trigger()` with separate offset from other batch tasks
- [X] T043 Update `memory_store.retrieve_relevant()` for clusters: boost cluster members when one member is retrieved

---

## Parallel Opportunities

```
Phase 2 完成后 (T004-T010):
  Phase 3 (US-1)    ←→  Phase 4 (US-2)  ←→  Phase 5 (US-3)
  (不同文件, engine.py vs memory_store.py vs orchestrator.py)

Phase 6 内 (T026-T034):
  T026, T030 可并行 (不同函数在 memory_store.py)
  T027, T031 可并行 (不同批处理任务)

Phase 7 内 (T035-T043):
  T035, T037 可并行 (不同文件 brain.py vs memory_store.py)
  T039, T040 顺序依赖
```

## MVP Scope

最小可验证增量 = Phase 2 (Schema) + Phase 3 (US-1 检索): Bot 能根据话题提取相关记忆。T004-T010 + T011-T017，共 14 个任务。

## Task Summary

| Phase | Tasks | US Coverage |
|-------|:-----:|------------|
| Setup | T001-T003 | — |
| Foundational | T004-T010 | — |
| US-1 检索 | T011-T017 | FR-1 |
| US-2 纠错 | T018-T021 | FR-3 |
| US-3 跨场景 | T022-T025 | FR-9 |
| US-6+US-7 关联+实体 | T026-T034 | FR-4, FR-5 |
| Polish 集群+Salience | T035-T043 | FR-2, Clusters |
| **Total** | **43** | — |
