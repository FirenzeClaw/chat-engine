# Phase 1 Data Model

## Schema Changes

### entries 表 (扩展)

```sql
-- 新增列 (ALTER TABLE ADD COLUMN)
ALTER TABLE entries ADD COLUMN memory_layer TEXT DEFAULT 'gist';
  -- 'gist' | 'detail'

ALTER TABLE entries ADD COLUMN decay_curve TEXT DEFAULT 'standard';
  -- 'standard' | 'deep' (集群覆盖) | 'none' (纠错链)

ALTER TABLE entries ADD COLUMN decay_start TEXT;
  -- ISO datetime, 衰减计时起点

ALTER TABLE entries ADD COLUMN auto_migrate INTEGER DEFAULT 0;
  -- 1 = 衰减到阈值时自动模糊化

ALTER TABLE entries ADD COLUMN salience REAL DEFAULT 0;
  -- 0-10, 综合重要性

ALTER TABLE entries ADD COLUMN corrected INTEGER DEFAULT 0;
  -- 1 = 已被纠正

ALTER TABLE entries ADD COLUMN superseded_by INTEGER DEFAULT NULL;
  -- FK → entries.id, 被哪条新事实替代

ALTER TABLE entries ADD COLUMN correction_reason TEXT DEFAULT NULL;

ALTER TABLE entries ADD COLUMN entity_type TEXT DEFAULT NULL;
  -- 'person_attribute' | 'factual_knowledge' | 'event' | 'relationship'

ALTER TABLE entries ADD COLUMN topic_tags TEXT DEFAULT NULL;
  -- JSON array: ["猫","宠物","医疗"]

ALTER TABLE entries ADD COLUMN about_person TEXT DEFAULT NULL;
  -- user_id, 此记忆关于谁

ALTER TABLE entries ADD COLUMN source TEXT DEFAULT 'private';
  -- 'private' | 'group'

ALTER TABLE entries ADD COLUMN group_id TEXT DEFAULT NULL;
  -- 群 ID

ALTER TABLE entries ADD COLUMN participants TEXT DEFAULT NULL;
  -- JSON array of user_ids

ALTER TABLE entries ADD COLUMN emotion_at_encoding TEXT DEFAULT NULL;
  -- JSON: {"joy":0.7, "surprise":0.3} (Phase 2 写入, Phase 1 预留)
```

### memory_links 表 (新建)

```sql
CREATE TABLE memory_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id INTEGER NOT NULL REFERENCES entries(id),
    to_id INTEGER NOT NULL REFERENCES entries(id),
    relation_type TEXT NOT NULL,
      -- 'same_topic' | 'contradicts' | 'extends' | 'same_day' | 'corrected_by'
      -- 'social_colleague' | 'social_friend' | 'social_family'
    strength REAL DEFAULT 1.0,
    source TEXT DEFAULT 'rule',  -- 'rule' | 'llm'
    created_at TEXT NOT NULL,
    UNIQUE(from_id, to_id, relation_type)
);
```

### memory_clusters 表 (新建)

```sql
CREATE TABLE memory_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    decay_curve_override TEXT DEFAULT 'deep',
      -- 集群内所有记忆衰减极慢
    member_ids TEXT NOT NULL,
      -- JSON array of entry ids
    created_at TEXT NOT NULL
);
```

### cluster_members 表 (新建)

```sql
CREATE TABLE cluster_members (
    cluster_id INTEGER NOT NULL REFERENCES memory_clusters(id),
    entry_id INTEGER NOT NULL REFERENCES entries(id),
    joined_at TEXT NOT NULL,
    PRIMARY KEY (cluster_id, entry_id)
);
```

### access_log 表 (新建)

```sql
CREATE TABLE access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES entries(id),
    accessed_at TEXT NOT NULL,
    context TEXT DEFAULT NULL  -- 检索时的上下文(消息摘要)
);
```

## Entity State Transitions

```
Entry memory_layer:
  gist ────────────────────────────────────► (永久保留)
  detail ──[60天+无访问]──► auto_migrate=1 ──► gist

Entry corrected:
  corrected=0 ──[纠正事件]──► corrected=1, superseded_by=新ID
  新ID: corrected=0, salience += 3 (纠错boost)

Entry decay_curve:
  'standard' ──[集群建立]──► 'deep' (覆盖)
  'standard' ──[纠错链中]──► 'none' (不过期)
```
