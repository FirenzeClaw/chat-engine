# 认知人格一体化架构 — 完整设计

> 日期: 2026-07-08 | 状态: Phase 1 implemented ✅ | Phase 2-3 planned

---

## 概述

以人类认知心理学为参照，为 chat-engine QQ Bot 构建三层认知架构：

| Phase | 体系 | 上线效果 |
|-------|------|---------|
| P1: 记忆引擎 | 检索/纠错/双层/图谱/跨场景 | Bot 说出"我记得你上次..."且准确 |
| P2: 情绪+人格 | 情绪向量/人格三层/程序记忆 | Bot 有"脾气"，且性格稳定 |
| P3: 社交智能 | 审视度势/秘密/画像/打趣 | Bot 知道什么该说什么不该说 |

---

## Phase 1: 记忆引擎

### FR-1: 线索提取检索

```
消息 → 规则提取关键词(毫秒级)
  → 若结果 <3 且消息 >10 字 → LLM_FAST 补全关键词+话题标签+指代消解(~200ms)
  → 模糊层检索: FTS5 粗筛(100ms) → 候选 >5 时 LLM 精排 top-5 → 情绪调制排序
  → 精确层检索: 仅主脑按需触发, FTS5 精确匹配
```

### 双层记忆模型

```
模糊层(gist):
  存储: user/{uid}/conversations (LLM 压缩摘要)
  时间: 时间段 ("7月初的某天下午")
  衰减: 艾宾浩斯曲线前端(慢衰减)
  访问: 辅脑默认检索

精确层(detail):
  存储: user/{uid}/facts (原文)
  时间: 时间点 ("7月3日 16:22")
  衰减: 艾宾浩斯曲线全段(快衰减, 30天半衰)
  访问: 仅主脑按需
  末期: 衰减到阈值→自动模糊化迁移到 gist 层
```

### FR-2: 记忆重要性加权

salience = brain评估情感分(0-10) + 对话深度(轮数/5) + 纠错boost(+3) + 集群boost(+2)

### FR-3: 记忆纠错与版本链

三层纠错路径: 用户显式纠正 / 脑评估检测矛盾 / 追答自纠。纠正后旧条目 corrected=1, superseded_by→新条目。旧记忆 expired=0 永久保留。检索时默认返回 corrected=0 的当前事实。

### FR-4: 关联图

规则边: 同日+同namespace→same_day 边
LLM 边: 每日批量扫描, 发现语义关联
扩散: 检索时从命中记忆沿关联边扩散, 最多额外 2 条

### FR-5: 实体分类与图谱连锁

条目标记 entity_type(person_attribute/factual_knowledge/event/relationship) + topic_tags(多值)。支持人际关系边(同事/朋友/家人)。图谱多跳遍历最大深度 3, 每跳主题相关性衰减。检索时人优先于事(about_person → about_topic)。

### 深刻记忆集群

触发: 同一话题/同人记忆 N 天内访问 ≥M 次 + LLM 语义确认。集群内所有记忆共享极慢衰减曲线, 不可被普通过期, 作为整体参与检索。

### FR-9: 跨场景记忆索引

条目标记 source(private/group) + group_id + participants。检索优先级: 同场景 > 私聊 > 其他群。跨场景连续性: 标记 linked_private_session / linked_group_session。

---

## Phase 2: 情绪 + 人格

### FR-7: 情绪系统

10 维情绪向量: 高兴/悲伤/愤怒/恐惧/惊讶/厌恶/信任/期待/困惑/好奇

各自独立衰减半衰期:
- 惊讶:30s | 困惑:120s | 好奇:300s | 厌恶:300s
- 高兴:600s | 愤怒:600s | 恐惧:600s
- 悲伤:900s | 期待:1800s | 信任:3600s

辅脑/理性脑/感性脑三脑独立维护情绪向量。双脑分歧度>0.3 触发自省。

### FR-7.8: 情绪 × 人格调制

情绪表达 = 真实情绪 × 人格过滤器。真实情绪向量不变(记忆编码/检索调制仍用真实值)。表达仅在辅脑 reply 的 tone 层体现。

### FR-8: 叙事人格三层

```
core: 不可变内核, 开发者写入
  "我温暖、独立、诚实、不迎合"
self_knowledge: 仅主脑自省追加
  条件: 3次独立事件 + 不违反core + 24h冷却
  容忍矛盾共存
expression: 情境覆盖
  玩笑模式/安慰模式, 会话结束丢弃
```

主脑并行运行理性脑+感性脑+一致性脑。一致性检查评估回复是否与 core 一致, 是否需维护人格边界。

### FR-6: 程序记忆

记录 {strategy, outcome, user_id} → 累积数据指导 expression 层自动调整。

---

## Phase 3: 社交智能

### FR-10: 审视度势裁判引擎

四关判定, 顺序不可跳:

```
关卡 1: 所有权 → 记忆属于谁? 当前对话对象?
关卡 2: 已知性 → 对方已知/不知/装不知?
关卡 3: 意图 → 认真/开玩笑/试探?
关卡 4: 时机+氛围 → 严肃话题/公开场合?
输出: say | hint | silent | deflect | play_along
```

### 秘密系统

```
secret/{owner_id}/items/{secret_id}:
  owner_type: self | other
  source: user_told | observed | inferred
  visibility:
    level: strict | trusted | hintable | open
    shared_with: [uid_A]
    hinted_to: [uid_B]
    reveal_condition: 自然语言触发条件
  importance: 0-10
  emotional_weight: 文本描述
```

自身的秘密: Bot 知道但不说。他人的秘密: 所有权检查拦截, 不在相关人在场时泄露。

### FR-12: 打趣系统

```
Layer 1 检测: tone识别 → 善意调侃/自嘲/嘲讽他人/恶作剧
Layer 2 判断:
  氛围严肃→不开 | 陌生人→谨慎 | 涉及痛点→绝不开 | 曾不悦→降分
Layer 3 生成:
  配合演出/会心一笑/反将一军/拆穿(仅高亲密度)
  风格匹配人格(幽默Bot回敬, 稳重Bot微笑)
Layer 4 记忆:
  成功→banter_comfort↑ | 失败→记入avoid_topics
  重复出现的玩笑→标记running_joke
```

### FR-11: 人物画像

```
user/{uid}/portrait:
  basic: 昵称/群组/活跃时段
  traits: 幽默感/直率度/敏感度(累积推理)
  communication: 风格/句长/表情频率
  emotion: 基线/触发点/压力信号
  knowledge: 擅长话题
  social: 群内角色/人际关系图
  preferences: 触发话题/避开话题/玩笑接受度
  with_bot: 与Bot的关系/信任度/内部梗
  confidence: 画像可信度(0-1)
```

更新: 异步低权重追加, 多次交叉验证后提升。矛盾共存不覆盖。

---

## 数据实体汇总

| 实体 | Phase | 说明 |
|------|-------|------|
| Entry | P1 | 记忆条目, 含 salience/corrected/entity_type/topic_tags/emotion_at_encoding |
| MemoryLink | P1 | 关联边, 含 relation_type/strength/source |
| MemoryCluster | P1 | 深刻记忆集群, 共享衰减曲线 |
| Secret | P3 | 秘密, 含 visibility/importance/reveal_condition |
| Portrait | P3 | 人物画像, 聚合推理 |
| BanterState | P3 | 玩笑状态, 含 comfort/内部梗 |
| Session | P1+2 | 会话, 含三脑情绪向量 |
| Persona | P2 | 人格, core/self_knowledge/expression |
| ConversationSummary | P1+3 | 对话摘要, 含 source/group_id/participants |

---

## 实现顺序

```
Phase 1 (记忆引擎):
  1.1 双层记忆模型 + schema 变更
  1.2 FR-1 线索提取检索
  1.3 FR-2 重要性加权
  1.4 FR-3 纠错版本链
  1.5 FR-4 关联图
  1.6 FR-5 实体分类+图谱连锁
  1.7 深刻记忆集群
  1.8 FR-9 跨场景索引

Phase 2 (情绪+人格):
  2.1 FR-7 情绪向量+衰减
  2.2 FR-7.8 情绪×人格调制
  2.3 FR-8 叙事人格三层
  2.4 FR-6 程序记忆

Phase 3 (社交智能):
  3.1 FR-10 审视度势
  3.2 秘密系统
  3.3 FR-12 打趣系统
  3.4 FR-11 人物画像
```
