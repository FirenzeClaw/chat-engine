# Memory Retrieval Contract

## `retrieve_relevant(query, user_id, context)` → list[dict]

### Input

```python
{
    "query": str,          # 当前用户消息文本
    "user_id": str,        # 检索的命名空间 owner
    "context": {           # 可选上下文
        "source": "private" | "group",
        "group_id": str | None,
        "about_person": str | None,   # 指定检索关于谁
        "emotion_state": dict | None  # 当前情绪向量 (用于调制排序)
    }
}
```

### Output

```python
[
    {
        "entry_id": int,
        "namespace": str,
        "key": str,
        "value": str,           # JSON string
        "memory_layer": "gist" | "detail",
        "salience": float,
        "relevance_score": float,  # 0-1, 由精排产生
        "entity_type": str,
        "topic_tags": [str],
        "about_person": str | None,
        "linked_memories": [int],   # 关联扩散的 entry_id
    }
]
# 最多 5 条
```

### Error Modes

| 场景 | 行为 |
|------|------|
| query 空字符串 | 返回 [] |
| user_id 无记忆 | 返回 [] |
| FTS5 粗筛无结果 | 跳过精排, 返回 [] |
| LLM 精排超时(1s) | 降级为 FTS5 排序 top-5 |
| memory_store 未初始化 | 返回 [] (不抛异常) |

## `correct_entry(namespace, key, new_value, reason)` → dict

### Input

```python
{
    "namespace": str,
    "key": str,
    "new_value": str,      # 新的事实值
    "reason": str,         # 纠正原因
}
```

### Output

```python
{
    "old_entry_id": int,
    "new_entry_id": int,
    "old_corrected": True,
    "version_chain": [int, int]  # 完整版本链 entry_id 列表
}
```

### Error Modes

| 场景 | 行为 |
|------|------|
| namespace/key 不存在 | 创建新条目 (不标记为纠正) |
| new_value 与当前值相同 | 返回当前 entry, 不创建新版本 |
