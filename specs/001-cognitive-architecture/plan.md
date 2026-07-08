# Implementation Plan: 认知架构 — Phase 1 记忆引擎

> **Feature**: 001-cognitive-architecture | **Phase**: 1/3
> **Input**: `docs/superpowers/specs/2026-07-08-cognitive-architecture-design.md`
> **Spec**: `specs/001-cognitive-architecture/spec.md`

---

## Technical Context

| 维度 | 决策 |
|------|------|
| 语言 | Python 3.12 |
| LLM SDK | openai >= 1.0 (AsyncOpenAI) |
| 数据库 | SQLite (aiosqlite) + FTS5 |
| HTTP | aiohttp (现有) |
| 消息总线 | asyncio.Queue + asyncio.create_task |
| 部署 | 单进程 localhost:18090 (`main.py`) |
| Token 估算 | 规则：CJK 1 字≈1 token, 英文 1 词≈1.3 token |

## Constitution Check

| 规范 | 合规 | 说明 |
|------|:---:|------|
| 零 CLI 依赖 | ✅ | 全 HTTP 调用 OpenAI API |
| 单进程入口 | ✅ | main.py 统一启动 |
| SQLite WAL 模式 | ✅ | 现有 memory_store 已启用 |
| 异步优先 | ✅ | 全部 async/await |
| 模块职责单一 | ✅ | engine 管上下文, orchestrator 管路, memory_store 管存储 |

## Gates

| 关卡 | 条件 | 状态 |
|------|------|:---:|
| G1 | 所有 FR 有验收标准 | ✅ |
| G2 | schema 变更向后兼容 | ⚠️ 需迁移脚本 |
| G3 | 不阻塞消息处理主路径 | ✅ 检索/精排全在 async task 中 |

---

## Phase 1 Implementation Tasks

### 1.1: 双层记忆模型 + Schema 变更

**目标**: 区分模糊层(gist)和精确层(detail), 精确层支持艾宾浩斯衰减和自动模糊化

**文件**: `memory_store.py`

**任务**:
- [ ] 1.1.1 `entries` 表增加列: `memory_layer` TEXT('gist'\|'detail'), `decay_curve` TEXT, `decay_start` TEXT, `auto_migrate` INT
- [ ] 1.1.2 新增 `memory_clusters` 表: id, name, decay_curve_override, member_ids JSON, created_at
- [ ] 1.1.3 新增 `cluster_members` 表: cluster_id, entry_id, joined_at
- [ ] 1.1.4 `build_index()` 返回区分 gist 层（仅 conversations 的摘要）+ 标记 detail 可用性
- [ ] 1.1.5 `apply_decay()` 增加艾宾浩斯曲线逻辑: 精确层 30 天半衰, 末期(days>60)自动迁移到模糊层
- [ ] 1.1.6 迁移脚本: 现有 `conversations` → gist 层, `facts` → detail 层

### 1.2: FR-1 线索提取检索

**目标**: 规则提取关键词 + LLM 精排选出 top-5 记忆

**文件**: `engine.py`, `memory_store.py`

**任务**:
- [ ] 1.2.1 新建 `_extract_keywords(content)` — 规则：简单分词 + 关键词检测
- [ ] 1.2.2 新建 `_llm_extract_keywords(content)` — LLM_FAST 补全关键词+话题标签+指代消解
- [ ] 1.2.3 触发条件: 规则结果 <3 且消息 >10 字 → LLM 补全
- [ ] 1.2.4 `build_index()` → `retrieve_relevant(query, user_id)` — FTS5 粗筛 20 条
- [ ] 1.2.5 候选 >5 → `_llm_rank_memories(candidates, query)` 精排 top-5
- [ ] 1.2.6 `_assemble_system_prompt()` 注入 top-5 记忆到 system prompt

### 1.3: FR-2 重要性加权

**目标**: salience 综合情感分 + 对话深度 + 纠错 boost + 集群 boost

**文件**: `memory_store.py`, `brain.py`

**任务**:
- [ ] 1.3.1 `entries` 表增加 `salience` REAL DEFAULT 0
- [ ] 1.3.2 `brain.evaluate()` 返回 `salience_score` 字段
- [ ] 1.3.3 `process_qq_message()` 存储对话轮数到新 fact/event 时计算 salience
- [ ] 1.3.4 `apply_decay()` 用 salience 调制衰减速度: 高 salience 衰减慢

### 1.4: FR-3 纠错版本链

**目标**: 支持 corrected/superseded_by 记忆纠正, 旧记忆不遗忘

**文件**: `memory_store.py`, `orchestrator.py`

**任务**:
- [ ] 1.4.1 `entries` 表增加列: `corrected` INT DEFAULT 0, `superseded_by` INT NULL, `correction_reason` TEXT
- [ ] 1.4.2 新增 `correct_entry(namespace, key, new_value, reason)` — 标记旧→写入新→建链接边
- [ ] 1.4.3 `orchestrator._async_handle()` 处理 action='correct' 的 memory_update
- [ ] 1.4.4 `build_index()` 默认过滤 `corrected=1`, 含"以前""纠正"关键词时返回完整链

### 1.5: FR-4 关联图

**目标**: 规则建边 + 每日 LLM 关联建边 + 检索扩散

**文件**: `memory_store.py`

**任务**:
- [ ] 1.5.1 新增 `memory_links` 表: from_id, to_id, relation_type, strength, source
- [ ] 1.5.2 规则建边: 同日+同 namespace → `same_day` 边, `set()` 时同步
- [ ] 1.5.3 异步任务 `_daily_link_scan()`: 扫描新增记忆, LLM 批量判断语义关联
- [ ] 1.5.4 `retrieve_relevant()` 从命中记忆沿关联边扩散, 限制深度 1+额外 2 条

### 1.6: FR-5 实体分类+图谱连锁

**目标**: 条目标记 entity_type/topic_tags/about_person, 多跳检索

**文件**: `memory_store.py`, `engine.py`

**任务**:
- [ ] 1.6.1 `entries` 表增加列: `entity_type` TEXT, `topic_tags` TEXT(JSON), `about_person` TEXT
- [ ] 1.6.2 规则预标: namespace 映射(`profile`→person_attribute) + 内容关键词检测
- [ ] 1.6.3 异步任务 `_daily_batch_tag()`: 每日扫描未标记记录, LLM 批量补标
- [ ] 1.6.4 `retrieve_relevant()` 支持多跳: about_person → topic_tags → 关联人 → 关联记忆
- [ ] 1.6.5 人际关系边: 新增 `social_colleague/social_friend/social_family` 等 relation_type

### 1.7: 深刻记忆集群

**目标**: 频率触发 + LLM 确认 → 集群共享极慢衰减

**文件**: `memory_store.py`

**任务**:
- [ ] 1.7.1 统计表 `access_log`: entry_id, accessed_at
- [ ] 1.7.2 `_check_cluster_trigger()`: 14 天内同 topic 访问 ≥3 次 → 候选
- [ ] 1.7.3 `_llm_confirm_cluster(candidates)`: LLM 语义判断是否形成深层关联
- [ ] 1.7.4 通过: 写入 `memory_clusters` + `cluster_members`, 覆盖 decay_curve 为 'deep'

### 1.8: FR-9 跨场景记忆索引

**目标**: 私聊/群聊标记, 同用户跨场景关联, 场景权重排序

**文件**: `memory_store.py`, `orchestrator.py`

**任务**:
- [ ] 1.8.1 `entries` 表增加列: `source` TEXT('private'\|'group'), `group_id` TEXT, `participants` TEXT(JSON)
- [ ] 1.8.2 `process_qq_message()` 传递 `source`/`group_id` 到记忆写入
- [ ] 1.8.3 `retrieve_relevant()` 场景权重: 同场景 > 私聊 > 其他群
- [ ] 1.8.4 跨场景连续性: 标记 `linked_private_session`/`linked_group_session`

---

## Dependencies

```
1.1 Schema → 1.2 检索, 1.3 salience, 1.4 纠错, 1.6 实体, 1.8 跨场景 (并行)
1.2 检索 → 1.5 关联图 (依赖检索路径)
1.5 关联图 → 1.6 图谱连锁 (依赖关系边)
1.5 关联图 → 1.7 集群 (依赖反复访问计数)
```

## Risk Items

| 风险 | 概率 | 缓解 |
|------|:---:|------|
| LLM 精排延迟影响回复速度 | 中 | 候选 ≤5 时跳过精排, 精排 timeout 1s 降级 |
| schema 迁移中断现有记忆 | 低 | 备份 memory.db + 事务迁移 |
| 每日批量任务 OOM | 低 | 分页处理, 每次 100 条 |
