"""
记忆存储引擎 — SQLite 驱动的异步记忆系统

替代 botuser.py 的 JSON I/O，提供：
- 三层命名空间记忆存储 (user/{uid}/*, group/{gid}/*, global/*)
- FTS5 全文搜索
- 惰性索引注入
- 记忆衰减与过期管理

命名空间约定：
    user/{uid}/profile       — 用户资料
    user/{uid}/facts         — 用户相关事实
    user/{uid}/conversations — 对话摘要
    group/{gid}/info         — 群信息
    global/persona           — Bot 性格设定
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from config import DB_PATH

logger = logging.getLogger("memory_store")
_memory_handler = logging.StreamHandler()
_memory_handler.setFormatter(logging.Formatter("[memory] %(message)s"))
logger.addHandler(_memory_handler)
logger.setLevel(logging.INFO)

_db: Optional[aiosqlite.Connection] = None
_db_path: str = ""
_migration_version: int = 0  # schema version, bumped on each migration
_key_locks: dict[str, asyncio.Lock] = {}  # per-namespace+key concurrency guard


def _lock_key(namespace: str, key: str) -> asyncio.Lock:
    """获取指定 namespace+key 的锁，不存在则创建。"""
    lock_key = f"{namespace}//{key}"
    if lock_key not in _key_locks:
        _key_locks[lock_key] = asyncio.Lock()
    return _key_locks[lock_key]


async def _run_migration() -> None:
    """执行 schema 迁移，确保数据库结构与最新版本一致。

    迁移采用 ALTER TABLE ADD COLUMN 方式，保持向后兼容。
    每个迁移步骤用 try/except 包裹，支持幂等重入。
    完成后更新 _migration_version。
    """
    global _migration_version
    db = await _ensure_db()

    # Schema version tracking table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    cursor = await db.execute("SELECT MAX(version) FROM schema_version")
    row = await cursor.fetchone()
    current = row[0] if row[0] is not None else 0
    _migration_version = current

    # Migration v1 → v2: Phase 1 记忆引擎新列 + 新表
    if current < 2:
        await _migrate_v2(db)
        current = 2

    # 约束修复：无论版本号，每次都检查并修复 UNIQUE 约束
    # （因为表重建在旧 DB 上可能因数据量大而失败，需要多次重试）
    await _ensure_partial_unique_index(db)

    _migration_version = current
    logger.info("Schema 迁移完成，当前版本: v%d", _migration_version)


async def _migrate_v2(db: aiosqlite.Connection) -> None:
    """v1 → v2: 添加 Phase 1 记忆引擎所需的列和表。

    每个 ALTER/ADD 独立 try/except，允许重复执行。
    """
    now = datetime.now(timezone.utc).isoformat()

    # --- entries 表新列 ---
    new_columns = [
        ("memory_layer", "TEXT DEFAULT 'gist'"),
        ("decay_curve", "TEXT DEFAULT 'standard'"),
        ("decay_start", "TEXT"),
        ("auto_migrate", "INTEGER DEFAULT 0"),
        ("salience", "REAL DEFAULT 0"),
        ("corrected", "INTEGER DEFAULT 0"),
        ("superseded_by", "INTEGER DEFAULT NULL"),
        ("correction_reason", "TEXT DEFAULT NULL"),
        ("entity_type", "TEXT DEFAULT NULL"),
        ("topic_tags", "TEXT DEFAULT NULL"),
        ("about_person", "TEXT DEFAULT NULL"),
        ("source", "TEXT DEFAULT 'private'"),
        ("group_id", "TEXT DEFAULT NULL"),
        ("participants", "TEXT DEFAULT NULL"),
        ("emotion_at_encoding", "TEXT DEFAULT NULL"),
    ]
    for col_name, col_def in new_columns:
        try:
            await db.execute(
                f"ALTER TABLE entries ADD COLUMN {col_name} {col_def}"
            )
        except Exception:
            pass  # column already exists, idempotent

    # --- 替换 UNIQUE(namespace, key) 为部分唯一索引 ---
    await _ensure_partial_unique_index(db)

    # --- memory_links 表 ---
    await db.execute("""
        CREATE TABLE IF NOT EXISTS memory_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id INTEGER NOT NULL REFERENCES entries(id),
            to_id INTEGER NOT NULL REFERENCES entries(id),
            relation_type TEXT NOT NULL,
            strength REAL DEFAULT 1.0,
            source TEXT DEFAULT 'rule',
            created_at TEXT NOT NULL,
            UNIQUE(from_id, to_id, relation_type)
        )
    """)

    # --- memory_clusters 表 ---
    await db.execute("""
        CREATE TABLE IF NOT EXISTS memory_clusters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            decay_curve_override TEXT DEFAULT 'deep',
            member_ids TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # --- cluster_members 表 ---
    await db.execute("""
        CREATE TABLE IF NOT EXISTS cluster_members (
            cluster_id INTEGER NOT NULL REFERENCES memory_clusters(id),
            entry_id INTEGER NOT NULL REFERENCES entries(id),
            joined_at TEXT NOT NULL,
            PRIMARY KEY (cluster_id, entry_id)
        )
    """)

    # --- access_log 表 ---
    await db.execute("""
        CREATE TABLE IF NOT EXISTS access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL REFERENCES entries(id),
            accessed_at TEXT NOT NULL,
            context TEXT DEFAULT NULL
        )
    """)

    # --- 索引 ---
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_links_from ON memory_links(from_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_links_to ON memory_links(to_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_access_log_entry ON access_log(entry_id)"
    )

    # --- 迁移现有数据 ---
    # conversations namespace → gist 层
    try:
        await db.execute(
            "UPDATE entries SET memory_layer='gist' WHERE namespace LIKE '%/conversations' AND memory_layer='gist'"
        )
    except Exception:
        pass
    # facts namespace → detail 层
    try:
        await db.execute(
            "UPDATE entries SET memory_layer='detail' WHERE namespace LIKE '%/facts' AND memory_layer='gist'"
        )
    except Exception:
        pass

    # 记录 schema 版本
    await db.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
        (2, now),
    )
    await db.commit()
    logger.info("迁移 v2 完成: 新增 15 列 + 4 表")


async def _ensure_partial_unique_index(db: aiosqlite.Connection) -> None:
    """确保 entries 表使用部分唯一索引代替 UNIQUE 约束。

    每次 init 时调用，幂等——检查旧约束是否存在，存在则重建表。
    新 DB（init() 中已无 UNIQUE）直接跳过。
    """
    try:
        # 检查旧约束是否存在
        cursor = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='entries'"
        )
        row = await cursor.fetchone()
        if row and "UNIQUE(namespace, key)" in (row[0] or ""):
            logger.info("检测到旧 UNIQUE(namespace,key) 约束，正在重建表...")
            # 重建表移除 UNIQUE 约束
            await db.execute("ALTER TABLE entries RENAME TO entries_old")
            await db.execute("""
                CREATE TABLE entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    version INTEGER DEFAULT 1,
                    expired INTEGER DEFAULT 0,
                    access_count INTEGER DEFAULT 0,
                    last_access TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    memory_layer TEXT DEFAULT 'gist',
                    decay_curve TEXT DEFAULT 'standard',
                    decay_start TEXT,
                    auto_migrate INTEGER DEFAULT 0,
                    salience REAL DEFAULT 0,
                    corrected INTEGER DEFAULT 0,
                    superseded_by INTEGER DEFAULT NULL,
                    correction_reason TEXT DEFAULT NULL,
                    entity_type TEXT DEFAULT NULL,
                    topic_tags TEXT DEFAULT NULL,
                    about_person TEXT DEFAULT NULL,
                    source TEXT DEFAULT 'private',
                    group_id TEXT DEFAULT NULL,
                    participants TEXT DEFAULT NULL,
                    emotion_at_encoding TEXT DEFAULT NULL
                )
            """)
            # 复制数据（使用 LIMIT 分批避免大表超时）
            offset = 0
            batch_size = 1000
            total = 0
            while True:
                cursor = await db.execute(
                    "SELECT * FROM entries_old LIMIT ? OFFSET ?",
                    (batch_size, offset),
                )
                batch = await cursor.fetchall()
                if not batch:
                    break
                for r in batch:
                    rd = dict(r)
                    await db.execute(
                        """INSERT INTO entries (id, namespace, key, value, version, expired,
                           access_count, last_access, created_at, updated_at,
                           memory_layer, decay_curve, decay_start, auto_migrate,
                           salience, corrected, superseded_by, correction_reason,
                           entity_type, topic_tags, about_person, source, group_id,
                           participants, emotion_at_encoding)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                   ?, ?, ?, ?, ?, ?, ?, ?,
                                   ?, ?, ?, ?, ?, ?, ?)""",
                        (rd["id"], rd["namespace"], rd["key"], rd["value"],
                         rd["version"], rd["expired"], rd["access_count"],
                         rd["last_access"], rd["created_at"], rd["updated_at"],
                         rd.get("memory_layer", "gist"), rd.get("decay_curve", "standard"),
                         rd.get("decay_start"), rd.get("auto_migrate", 0),
                         rd.get("salience", 0), rd.get("corrected", 0),
                         rd.get("superseded_by"), rd.get("correction_reason"),
                         rd.get("entity_type"), rd.get("topic_tags"),
                         rd.get("about_person"), rd.get("source", "private"),
                         rd.get("group_id"), rd.get("participants"),
                         rd.get("emotion_at_encoding")),
                    )
                    total += 1
                offset += batch_size
            await db.execute("DROP TABLE entries_old")
            logger.info("表重建完成: %d 条记录已迁移", total)
    except Exception as e:
        logger.warning("部分唯一索引检查/修复失败: %s", e)

    # 创建部分唯一索引（幂等）
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_active_entry ON entries(namespace, key) WHERE corrected=0 AND expired=0"
    )


async def init(db_path: str = "") -> None:
    """初始化数据库，创建表和索引，启用 FTS5。

    Args:
        db_path: SQLite 数据库文件路径，默认使用 config.DB_PATH。
    """
    global _db, _db_path

    _db_path = db_path or DB_PATH

    # 确保父目录存在
    Path(_db_path).parent.mkdir(parents=True, exist_ok=True)

    _db = await aiosqlite.connect(_db_path)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA foreign_keys=ON")

    # 主表（不含 UNIQUE 约束 — 纠正链需要多版本同 key）
    await _db.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            namespace TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            version INTEGER DEFAULT 1,
            expired INTEGER DEFAULT 0,
            access_count INTEGER DEFAULT 0,
            last_access TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    await _db.execute(
        "CREATE INDEX IF NOT EXISTS idx_namespace ON entries(namespace)"
    )
    await _db.execute(
        "CREATE INDEX IF NOT EXISTS idx_expired ON entries(expired, created_at)"
    )

    # FTS5 全文搜索虚拟表（内部内容模式，支持 INSERT/DELETE）
    await _db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
            value, tokenize='unicode61'
        )
    """)

    await _db.commit()

    # 执行 schema 迁移（幂等）
    try:
        await _run_migration()
    except Exception:
        logger.exception("schema 迁移失败，继续使用现有结构")

    logger.info("记忆数据库已初始化: %s", _db_path)


async def _ensure_db() -> aiosqlite.Connection:
    """确保数据库已初始化。"""
    global _db
    if _db is None:
        await init()
    return _db  # type: ignore[return-value]


# ==================== Memory CRUD ====================

async def set(
    namespace: str,
    key: str,
    value: str,
    expire: bool = False,
    source: str = "private",
    group_id: Optional[str] = None,
    participants: Optional[str] = None,
    salience: Optional[float] = None,
) -> int:
    """写入/更新记忆。返回 version 号。

    如果 (namespace, key) 已存在，更新值并增加 version。
    如果不存在，插入新记录，version=1。

    Args:
        namespace: 命名空间
        key: 键
        value: 值。⚠️ 若为 JSON，须使用 json.dumps(data, ensure_ascii=False)，
               否则中文被转义为 \\uXXXX 将导致 FTS5 和 LIKE 检索失效。
        expire: 是否标记过期
        source: 来源 "private" | "group"
        group_id: 群 ID
        participants: JSON array of user_ids
        salience: 重要性评分 0-10 (Phase 1)
    """
    async with _lock_key(namespace, key):
        return await _set_impl(namespace, key, value, expire, source, group_id, participants, salience)


async def _set_impl(
    namespace: str,
    key: str,
    value: str,
    expire: bool = False,
    source: str = "private",
    group_id: Optional[str] = None,
    participants: Optional[str] = None,
    salience: Optional[float] = None,
) -> int:
    """set() 的实际实现（调用方需先获取锁）。"""
    db = await _ensure_db()
    now = datetime.now(timezone.utc).isoformat()

    cursor = await db.execute(
        "SELECT version, expired FROM entries WHERE namespace=? AND key=? ORDER BY corrected ASC, version DESC LIMIT 1",
        (namespace, key),
    )
    existing = await cursor.fetchone()

    if existing:
        new_version = existing["version"] + 1
        await db.execute(
            """UPDATE entries
               SET value=?, version=?, expired=?, updated_at=?,
                   source=COALESCE(?, source), group_id=COALESCE(?, group_id),
                   participants=COALESCE(?, participants),
                   salience=COALESCE(?, salience)
               WHERE namespace=? AND key=?""",
            (value, new_version, int(expire), now, source, group_id, participants, salience, namespace, key),
        )
    else:
        new_version = 1
        await db.execute(
            """INSERT INTO entries (namespace, key, value, version, expired,
               created_at, updated_at, source, group_id, participants, salience)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (namespace, key, value, new_version, int(expire), now, now, source, group_id, participants, salience or 0),
        )

    # 同步 FTS 索引
    row_id = (await db.execute(
        "SELECT id FROM entries WHERE namespace=? AND key=?",
        (namespace, key),
    ))
    row = await row_id.fetchone()
    if row:
        await db.execute(
            "INSERT OR REPLACE INTO entries_fts(rowid, value) VALUES (?, ?)",
            (row["id"], value),
        )

    # T026: 规则建边 — 同日+同namespace → same_day 边
    try:
        entry_id = (await (await db.execute(
            "SELECT id FROM entries WHERE namespace=? AND key=?",
            (namespace, key),
        )).fetchone())["id"]
        today_prefix = now[:10]  # YYYY-MM-DD
        cursor = await db.execute(
            """SELECT id FROM entries
               WHERE namespace=? AND created_at LIKE ? AND id != ?
               LIMIT 5""",
            (namespace, f"{today_prefix}%", entry_id),
        )
        same_day = await cursor.fetchall()
        for other in same_day:
            await db.execute(
                """INSERT OR IGNORE INTO memory_links
                   (from_id, to_id, relation_type, strength, source, created_at)
                   VALUES (?, ?, 'same_day', 0.5, 'rule', ?)""",
                (entry_id, other["id"], now),
            )
    except Exception:
        pass

    # T030: 规则实体预标 — namespace 映射 + 内容关键词
    try:
        entity_type = None
        topic_tags_list = []

        # namespace 映射
        if "/profile" in namespace:
            entity_type = "person_attribute"
        elif "/facts" in namespace:
            entity_type = "factual_knowledge"
        elif "/conversations" in namespace:
            entity_type = "event"

        # 内容关键词检测 topic_tags
        try:
            v_data = json.loads(value)
            text = v_data.get("summary", str(v_data))
        except (json.JSONDecodeError, TypeError):
            text = value

        # 简单话题标签检测
        topic_keywords = {
            "猫": "宠物", "狗": "宠物", "宠物": "宠物",
            "喜欢": "偏好", "爱好": "偏好", "讨厌": "偏好",
            "工作": "职业", "上班": "职业", "公司": "职业",
            "家": "家庭", "父母": "家庭", "孩子": "家庭",
            "医院": "医疗", "病": "医疗", "药": "医疗",
            "游戏": "娱乐", "电影": "娱乐", "音乐": "娱乐",
            "学习": "教育", "学校": "教育", "老师": "教育",
            "吃": "饮食", "食物": "饮食", "饭": "饮食",
        }
        for kw, tag in topic_keywords.items():
            if kw in text and tag not in topic_tags_list:
                topic_tags_list.append(tag)

        if entity_type:
            await db.execute(
                "UPDATE entries SET entity_type=? WHERE id=?",
                (entity_type, entry_id),
            )
        if topic_tags_list:
            await db.execute(
                "UPDATE entries SET topic_tags=? WHERE id=?",
                (json.dumps(topic_tags_list), entry_id),
            )
    except Exception:
        pass

    await db.commit()
    return new_version


async def get(
    namespace: str, key: str, include_expired: bool = False
) -> Optional[dict]:
    """读取单条记忆。返回 dict 或 None。

    读取时会更新 access_count 和 last_access。
    """
    db = await _ensure_db()

    if include_expired:
        cursor = await db.execute(
            "SELECT * FROM entries WHERE namespace=? AND key=? ORDER BY corrected ASC, version DESC LIMIT 1",
            (namespace, key),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM entries WHERE namespace=? AND key=? AND expired=0 ORDER BY corrected ASC, version DESC LIMIT 1",
            (namespace, key),
        )

    row = await cursor.fetchone()
    if row is None:
        return None

    # 更新访问计数
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE entries SET access_count=access_count+1, last_access=? WHERE id=?",
        (now, row["id"]),
    )
    await db.commit()

    return dict(row)


async def delete(namespace: str, key: str) -> bool:
    """删除记忆。返回是否成功。

    同时清理对应的 FTS5 索引条目。
    """
    db = await _ensure_db()

    # 获取 id 用于清理 FTS
    cursor = await db.execute(
        "SELECT id FROM entries WHERE namespace=? AND key=?",
        (namespace, key),
    )
    row = await cursor.fetchone()
    if row is None:
        return False

    entry_id = row["id"]
    await db.execute("DELETE FROM entries WHERE id=?", (entry_id,))
    await db.execute("DELETE FROM entries_fts WHERE rowid=?", (entry_id,))
    await db.commit()
    return True


async def list_keys(prefix: str) -> list[str]:
    """列出命名空间前缀下的所有键名。

    Args:
        prefix: 命名空间前缀，如 "user/uid123"。用 LIKE prefix% 匹配。
    """
    db = await _ensure_db()
    cursor = await db.execute(
        "SELECT key FROM entries WHERE namespace LIKE ? AND expired=0",
        (f"{prefix}%",),
    )
    rows = await cursor.fetchall()
    return [r["key"] for r in rows]


# ==================== Search ====================

async def search(query: str, namespace: str = "") -> list[dict]:
    """FTS5 全文搜索。返回匹配的记忆列表。

    先用 FTS5 MATCH 搜索（支持英文），若结果为空则降级为 LIKE 模糊匹配
    （覆盖中文等 unicode61 tokenizer 不支持的场景）。

    Args:
        query: 搜索词，支持 FTS5 语法。
        namespace: 可选，限定命名空间前缀。
    """
    import time
    t_start = time.monotonic()

    db = await _ensure_db()

    # 第一步：FTS5 MATCH（英文/ASCII 有效）
    if namespace:
        sql = """
            SELECT e.* FROM entries e
            INNER JOIN entries_fts f ON e.id = f.rowid
            WHERE entries_fts MATCH ? AND e.namespace LIKE ? AND e.expired = 0
            ORDER BY rank
            LIMIT 50
        """
        cursor = await db.execute(sql, (query, f"{namespace}%"))
    else:
        sql = """
            SELECT e.* FROM entries e
            INNER JOIN entries_fts f ON e.id = f.rowid
            WHERE entries_fts MATCH ? AND e.expired = 0
            ORDER BY rank
            LIMIT 50
        """
        cursor = await db.execute(sql, (query,))

    rows = await cursor.fetchall()

    # 第二步：FTS5 无结果时降级为 LIKE 模糊匹配（覆盖中文等非拉丁语言）
    if not rows:
        like_pattern = f"%{query}%"
        if namespace:
            cursor = await db.execute(
                "SELECT * FROM entries WHERE expired=0 AND namespace LIKE ? AND value LIKE ? LIMIT 50",
                (f"{namespace}%", like_pattern),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM entries WHERE expired=0 AND value LIKE ? LIMIT 50",
                (like_pattern,),
            )
        rows = await cursor.fetchall()

    latency_ms = (time.monotonic() - t_start) * 1000
    if latency_ms > 100:
        logger.warning(
            "记忆搜索延迟超标: %dms (SLO: <100ms), query=%s, namespace=%s",
            int(latency_ms), query, namespace
        )

    return [dict(r) for r in rows]


# ==================== Phase 1: 记忆检索 ====================

async def retrieve_relevant(
    query: str,
    user_id: str,
    context: Optional[dict] = None,
) -> list[dict]:
    """根据查询检索相关记忆。

    流程：
    1. FTS5 粗筛 top-20 候选
    2. 候选 >MAX_RETRIEVAL_RESULTS → LLM 精排 top-N
    3. 候选 ≤MAX_RETRIEVAL_RESULTS → 直接返回

    Args:
        query: 搜索查询文本
        user_id: 用户 ID（用于 namespace 过滤）
        context: 可选上下文 {source, group_id, about_person, emotion_state}

    Returns:
        [{entry_id, namespace, key, value, memory_layer, salience,
          relevance_score, entity_type, topic_tags, about_person,
          linked_memories}], 最多 MAX_RETRIEVAL_RESULTS 条
    """
    from config import MAX_RETRIEVAL_CANDIDATES, MAX_RETRIEVAL_RESULTS

    t_start = time.monotonic()
    context = context or {}
    source = context.get("source", "private")
    group_id = context.get("group_id")

    if not query.strip():
        return []

    db = await _ensure_db()

    # Step 1: FTS5 粗筛 → top-N 候选
    candidate_limit = MAX_RETRIEVAL_CANDIDATES

    # 构建 namespace 过滤条件
    ns_patterns = [f"user/{user_id}/%"]
    if group_id:
        ns_patterns.append(f"group/{group_id}/%")
    ns_conditions = " OR ".join(["e.namespace LIKE ?"] * len(ns_patterns))
    ns_params = list(ns_patterns)

    # FTS5 MATCH + namespace 过滤
    try:
        sql = f"""
            SELECT e.*, f.rank FROM entries e
            INNER JOIN entries_fts f ON e.id = f.rowid
            WHERE entries_fts MATCH ? AND ({ns_conditions}) AND e.expired = 0 AND e.corrected = 0
            ORDER BY rank
            LIMIT ?
        """
        cursor = await db.execute(sql, (query, *ns_params, candidate_limit))
        rows = await cursor.fetchall()
    except Exception:
        # FTS5 MATCH 失败（如特殊字符），降级为 LIKE
        rows = []

    # LIKE 降级（FTS5 无结果时）：逐个关键词独立匹配
    if not rows:
        # 拆分 query 为独立关键词，做 OR LIKE 匹配
        keywords = [kw for kw in query.split() if len(kw) >= 1]
        if keywords:
            like_conditions = " OR ".join(["e.value LIKE ?"] * len(keywords))
            like_params = [f"%{kw}%" for kw in keywords]
            sql = f"""
                SELECT e.* FROM entries e
                WHERE ({ns_conditions}) AND e.expired = 0 AND e.corrected = 0 AND ({like_conditions})
                ORDER BY e.updated_at DESC
                LIMIT ?
            """
            cursor = await db.execute(sql, (*ns_params, *like_params, candidate_limit))
            rows = await cursor.fetchall()
        else:
            like_pattern = f"%{query}%"
            sql = f"""
                SELECT e.* FROM entries e
                WHERE ({ns_conditions}) AND e.expired = 0 AND e.corrected = 0 AND e.value LIKE ?
                ORDER BY e.updated_at DESC
                LIMIT ?
            """
            cursor = await db.execute(sql, (*ns_params, like_pattern, candidate_limit))
            rows = await cursor.fetchall()

    candidates = [dict(r) for r in rows]
    candidate_count = len(candidates)

    # Step 2: 精排 or 直接返回
    if candidate_count == 0:
        logger.debug("检索无结果: query=%s, user=%s", query[:20], user_id[:12])
        return []

    if candidate_count <= MAX_RETRIEVAL_RESULTS:
        selected = candidates
        relevance_method = "direct"
    else:
        selected = await _llm_rank_memories(candidates, query)
        relevance_method = "llm_rank"

    # Step 3.5: T029 扩散激活 — 从 top-5 沿 memory_links 扩散 ≤2 条
    if selected and candidate_count > 0:
        selected = await _spreading_activation(db, selected, max_linked=2)

    # Step 3.6: T033 多跳检索 — about_person → topic_tags → related persons
    if context.get("about_person"):
        hop_memories = await _multi_hop_retrieve(
            db, selected, context["about_person"], max_hops=3
        )
        if hop_memories:
            existing_ids = {m["id"] for m in selected}
            for hm in hop_memories:
                if hm["id"] not in existing_ids:
                    hm["_relevance"] = hm.get("_relevance", 0.5) * 0.7  # 多跳衰减
                    selected.append(hm)

    # Step 3.7: T043 集群 boost — 选中记忆的集群成员获得加分
    if selected:
        selected = await _cluster_boost(db, selected)

    # Step 3: 场景权重调整（在所有扩展之后，保证一致性）
    if source and selected:
        selected = _apply_scene_weighting(selected, source, group_id)

    # Step 4: 格式化输出
    result = []
    for entry in selected:
        topic_tags = []
        try:
            if entry.get("topic_tags"):
                topic_tags = json.loads(entry["topic_tags"])
        except (json.JSONDecodeError, TypeError):
            pass

        result.append({
            "entry_id": entry["id"],
            "namespace": entry["namespace"],
            "key": entry["key"],
            "value": entry["value"],
            "memory_layer": entry.get("memory_layer", "gist"),
            "salience": entry.get("salience", 0),
            "relevance_score": entry.get("_relevance", 1.0),
            "entity_type": entry.get("entity_type"),
            "topic_tags": topic_tags,
            "about_person": entry.get("about_person"),
            "linked_memories": [],
        })

    latency_ms = (time.monotonic() - t_start) * 1000
    logger.info(
        "检索完成: query=%s, candidates=%d, selected=%d, method=%s, latency=%dms",
        query[:20], candidate_count, len(result), relevance_method, int(latency_ms),
    )
    return result


async def _llm_rank_memories(
    candidates: list[dict],
    query: str,
    timeout: float = 1.0,
) -> list[dict]:
    """LLM 精排候选记忆。

    输入候选记忆简要信息 + 当前消息 → 输出 top-N 索引。
    超时降级为 FTS5 rank 排序。

    Args:
        candidates: 候选记忆列表
        query: 当前查询文本

    Returns:
        精排后的记忆列表，最多 MAX_RETRIEVAL_RESULTS 条
    """
    from config import MAX_RETRIEVAL_RESULTS
    from openai import AsyncOpenAI
    from config import LLM_API_KEY, LLM_BASE_URL, LLM_FAST_MODEL

    if len(candidates) <= MAX_RETRIEVAL_RESULTS:
        return candidates

    # 构建候选摘要
    items_text = []
    for i, c in enumerate(candidates):
        try:
            val = c.get("value", "")
            if len(val) > 80:
                val = val[:80] + "..."
        except Exception:
            val = ""
        items_text.append(f"[{i}] {val}")

    prompt = f"""从以下候选记忆中选择与查询最相关的 {MAX_RETRIEVAL_RESULTS} 条。

查询: {query}

候选:
{chr(10).join(items_text)}

输出 JSON: {{"selected": [索引列表], "reason": "简述"}}"""

    try:
        import asyncio as _asyncio
        client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
        response = await _asyncio.wait_for(
            client.chat.completions.create(
                model=LLM_FAST_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.1,
            ),
            timeout=timeout,
        )
        raw = response.choices[0].message.content or ""
        # 解析 JSON
        try:
            if "```json" in raw:
                raw = raw[raw.index("```json") + 7:]
                if "```" in raw:
                    raw = raw[:raw.index("```")]
            elif "```" in raw:
                raw = raw[raw.index("```") + 3:]
                if "```" in raw:
                    raw = raw[:raw.index("```")]
            data = json.loads(raw.strip())
            indices = data.get("selected", [])
            # 验证索引
            selected = []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(candidates):
                    candidates[idx]["_relevance"] = 1.0 - (len(selected) * 0.1)
                    selected.append(candidates[idx])
            if selected:
                return selected[:MAX_RETRIEVAL_RESULTS]
        except (json.JSONDecodeError, ValueError, KeyError):
            pass
    except asyncio.TimeoutError:
        logger.debug("LLM 精排超时，降级为 FTS5 rank")
    except Exception:
        pass

    # 降级：按 FTS5 rank（或 salience）排序
    sorted_candidates = sorted(
        candidates,
        key=lambda c: c.get("salience", 0),
        reverse=True,
    )
    return sorted_candidates[:MAX_RETRIEVAL_RESULTS]


def _apply_scene_weighting(
    entries: list[dict],
    source: str,
    group_id: Optional[str],
) -> list[dict]:
    """场景权重调整：同场景 > 私聊 > 其他群。

    在内存中排序，不改数据库。
    """
    def _weight(entry: dict) -> float:
        entry_source = entry.get("source", "private")
        entry_group = entry.get("group_id", "")

        if source == "private":
            if entry_source == "private":
                return 1.0
            elif entry_source == "group":
                return 0.8
            return 0.6
        elif source == "group":
            if entry_source == "group" and entry_group == group_id:
                return 1.0
            elif entry_source == "private":
                return 0.8
            elif entry_source == "group":
                return 0.6
            return 0.5
        return 1.0

    for e in entries:
        e["_relevance"] = e.get("_relevance", 1.0) * _weight(e)

    entries.sort(key=lambda e: e.get("_relevance", 0), reverse=True)
    return entries


async def _spreading_activation(
    db: aiosqlite.Connection,
    selected: list[dict],
    max_linked: int = 2,
) -> list[dict]:
    """T029: 从选定记忆沿关联边扩散，添加关联记忆。

    从 selected 中的 top 条目出发，沿 memory_links 查找关联条目，
    最多额外添加 max_linked 条。

    Returns:
        扩展后的 selected 列表
    """
    now = datetime.now(timezone.utc).isoformat()
    if not selected:
        return selected

    # 获取 top-5 的 id
    top_ids = [m["id"] for m in selected[:5]]
    linked_memories: dict[int, dict] = {}

    for entry_id in top_ids:
        if len(linked_memories) >= max_linked:
            break
        # 查找关联边（双向）
        cursor = await db.execute(
            """SELECT to_id, from_id, relation_type, strength FROM memory_links
               WHERE (from_id=? OR to_id=?)
               ORDER BY strength DESC
               LIMIT ?""",
            (entry_id, entry_id, max_linked),
        )
        links = await cursor.fetchall()
        for link in links:
            linked_id = link["to_id"] if link["to_id"] != entry_id else link["from_id"]
            if linked_id not in linked_memories and linked_id not in top_ids:
                # 获取关联记忆
                cursor2 = await db.execute(
                    "SELECT * FROM entries WHERE id=? AND expired=0 AND corrected=0",
                    (linked_id,),
                )
                row = await cursor2.fetchone()
                if row and len(linked_memories) < max_linked:
                    linked_entry = dict(row)
                    linked_entry["_relevance"] = link["strength"] * 0.5
                    linked_memories[linked_id] = linked_entry
                    # 记录扩散访问到 access_log
                    await db.execute(
                        "INSERT INTO access_log (entry_id, accessed_at, context) VALUES (?, ?, 'spreading')",
                        (linked_id, now),
                    )

    # 标记链接关系
    for m in selected:
        m["linked_memories"] = []

    for linked_id, linked_entry in linked_memories.items():
        # 记录在第一个命中的 entry 的 linked_memories 中
        if selected:
            selected[0].setdefault("linked_memories", []).append(linked_id)

    selected.extend(linked_memories.values())
    return selected


async def _cluster_boost(
    db: aiosqlite.Connection,
    selected: list[dict],
) -> list[dict]:
    """T043: 集群成员 boost — 选中记忆的集群成员获得额外加分。

    查询 selected 中每条记忆是否属于某个集群，
    若是则将该集群的其他成员也加入结果（最多额外 3 条）。
    """
    if not selected:
        return selected

    now = datetime.now(timezone.utc).isoformat()
    entry_ids = [m["id"] for m in selected]
    placeholders = ",".join("?" * len(entry_ids))
    cursor = await db.execute(
        f"""SELECT cm.cluster_id, cm.entry_id, mc.name
           FROM cluster_members cm
           JOIN memory_clusters mc ON cm.cluster_id = mc.id
           WHERE cm.entry_id IN ({placeholders})""",
        entry_ids,
    )
    cluster_rows = [dict(r) for r in await cursor.fetchall()]

    if not cluster_rows:
        return selected

    # 收集集群中的其他成员
    cluster_ids = set(r["cluster_id"] for r in cluster_rows)
    cluster_extra: dict[int, dict] = {}
    existing_ids = set(entry_ids)

    for cid in list(cluster_ids)[:3]:  # 最多处理 3 个集群
        cursor = await db.execute(
            """SELECT e.*, cm.cluster_id FROM entries e
               JOIN cluster_members cm ON e.id = cm.entry_id
               WHERE cm.cluster_id=?
               ORDER BY e.salience DESC
               LIMIT 5""",
            (cid,),
        )
        members = [dict(r) for r in await cursor.fetchall()]
        for m in members:
            mid = m["id"]
            if mid not in existing_ids and mid not in cluster_extra:
                m["_relevance"] = 0.9  # 集群成员高分 boost
                cluster_extra[mid] = m
                # 记录集群访问到 access_log
                await db.execute(
                    "INSERT INTO access_log (entry_id, accessed_at, context) VALUES (?, ?, 'cluster')",
                    (mid, now),
                )

    # 标记 linked_memories
    for extra_id in cluster_extra:
        if selected:
            selected[0].setdefault("linked_memories", []).append(extra_id)

    selected.extend(list(cluster_extra.values())[:3])
    return selected


async def _multi_hop_retrieve(
    db: aiosqlite.Connection,
    selected: list[dict],
    about_person: str,
    max_hops: int = 3,
) -> list[dict]:
    """T033: 多跳检索 — about_person → topic_tags → related persons → memories.

    从关于特定人的记忆出发，沿话题标签扩散到关联人，再检索关联人的记忆。
    最大深度 3 跳，每跳相关性衰减。

    Returns:
        额外检索到的记忆列表
    """
    if max_hops <= 0:
        return []

    result: list[dict] = []
    seen_ids = set()

    # Hop 1: 从已有结果提取话题标签
    all_tags = set()
    for m in selected:
        try:
            tags = json.loads(m.get("topic_tags", "[]"))
            if isinstance(tags, list):
                for t in tags:
                    all_tags.add(t)
        except (json.JSONDecodeError, TypeError):
            pass

    if not all_tags and max_hops < 2:
        return result

    # Hop 2: 查找有相同话题标签的其他条目（关于其他人的）
    if all_tags:
        tag_condition = " OR ".join(["topic_tags LIKE ?"] * len(all_tags))
        tag_params = [f"%{t}%" for t in all_tags]
        # 同时也查看 about_person 匹配的条目
        cursor = await db.execute(
            f"""SELECT * FROM entries
               WHERE ({tag_condition})
               AND expired=0 AND corrected=0 AND about_person IS NOT NULL
               LIMIT 5""",
            tag_params,
        )
        hop2 = [dict(r) for r in await cursor.fetchall()]
        for h in hop2:
            if h["id"] not in seen_ids:
                h["_relevance"] = 0.6  # 第 2 跳衰减
                seen_ids.add(h["id"])
                result.append(h)

        if max_hops < 3:
            return result

    # Hop 3: 从 hop2 中提取相关人的 user_id，检索他们的记忆
    related_persons = set()
    for h in result:
        ap = h.get("about_person")
        if ap and ap != about_person:
            related_persons.add(ap)

    for person_id in list(related_persons)[:3]:
        ns_like = f"user/{person_id}/%"
        cursor = await db.execute(
            """SELECT * FROM entries
               WHERE namespace LIKE ? AND expired=0 AND corrected=0
               ORDER BY salience DESC
               LIMIT 5""",
            (ns_like,),
        )
        hop3 = [dict(r) for r in await cursor.fetchall()]
        for h in hop3:
            if h["id"] not in seen_ids:
                h["_relevance"] = 0.4  # 第 3 跳衰减
                seen_ids.add(h["id"])
                result.append(h)

    return result[:5]


# ==================== Phase 1: 记忆纠错 ====================


async def correct_entry(
    namespace: str,
    key: str,
    new_value: str,
    reason: str = "",
) -> dict:
    """纠正记忆：标记旧条目 → 写入新条目 → 建 corrected_by 链接。

    旧条目不会被删除，而是标记 corrected=1 + superseded_by=新ID。
    新条目 salience = 原 salience + 3（纠错 boost）。

    Args:
        namespace: 命名空间
        key: 键
        new_value: 新的事实值
        reason: 纠正原因

    Returns:
        {old_entry_id, new_entry_id, old_corrected, version_chain}
    """
    async with _lock_key(namespace, key):
        return await _correct_entry_impl(namespace, key, new_value, reason)


async def _correct_entry_impl(
    namespace: str,
    key: str,
    new_value: str,
    reason: str = "",
) -> dict:
    """correct_entry 的实际实现（调用方需先获取锁）。"""
    db = await _ensure_db()
    now = datetime.now(timezone.utc).isoformat()

    # 查找当前活跃条目
    cursor = await db.execute(
        "SELECT * FROM entries WHERE namespace=? AND key=? AND expired=0 AND corrected=0",
        (namespace, key),
    )
    old = await cursor.fetchone()

    if old is None:
        # 不存在 → 直接创建新条目
        new_id = await _set_impl(namespace, key, new_value)
        return {
            "old_entry_id": None,
            "new_entry_id": new_id,
            "old_corrected": False,
            "version_chain": [new_id],
        }

    old_dict = dict(old)
    old_id = old_dict["id"]
    old_salience = old_dict.get("salience", 0)

    # 如果新值与当前值相同，不创建新版本
    if old_dict["value"] == new_value:
        return {
            "old_entry_id": old_id,
            "new_entry_id": old_id,
            "old_corrected": False,
            "version_chain": [old_id],
        }

    # 标记旧条目为已纠正
    await db.execute(
        """UPDATE entries SET corrected=1, expired=0, decay_curve='none',
           updated_at=? WHERE id=?""",
        (now, old_id),
    )

    # 写入新条目
    cursor = await db.execute(
        """INSERT INTO entries (namespace, key, value, version, expired,
           created_at, updated_at, memory_layer, decay_curve, salience,
           correction_reason, source, group_id, participants)
           VALUES (?, ?, ?, ?, 0, ?, ?, ?, 'none', ?, ?, ?, ?, ?)""",
        (
            namespace, key, new_value, old_dict["version"] + 1,
            now, now,
            old_dict.get("memory_layer", "gist"),
            old_dict.get("salience", 0) + 3,  # 纠错 boost
            reason,
            old_dict.get("source", "private"),
            old_dict.get("group_id"),
            old_dict.get("participants"),  # 继承旧条目的参与者信息
        ),
    )
    new_id = cursor.lastrowid

    # 更新旧条目的 superseded_by
    await db.execute(
        "UPDATE entries SET superseded_by=? WHERE id=?",
        (new_id, old_id),
    )

    # 同步 FTS 索引
    await db.execute(
        "INSERT OR REPLACE INTO entries_fts(rowid, value) VALUES (?, ?)",
        (new_id, new_value),
    )

    # 建立 corrected_by 边
    await db.execute(
        """INSERT OR IGNORE INTO memory_links
           (from_id, to_id, relation_type, strength, source, created_at)
           VALUES (?, ?, 'corrected_by', 1.0, 'rule', ?)""",
        (old_id, new_id, now),
    )

    # 构建完整版本链
    chain = [old_id, new_id]
    try:
        chain_cursor = await db.execute(
            "SELECT id, superseded_by FROM entries WHERE namespace=? AND key=? ORDER BY version",
            (namespace, key),
        )
        all_versions = await chain_cursor.fetchall()
        chain = [v["id"] for v in all_versions]
    except Exception:
        pass

    await db.commit()
    logger.info(
        "记忆纠正: %s/%s → corrected=%d new=%d",
        namespace, key, old_id, new_id,
    )
    return {
        "old_entry_id": old_id,
        "new_entry_id": new_id,
        "old_corrected": True,
        "version_chain": chain,
    }


# ==================== Index Injection ====================

async def build_index(user_id: str) -> str:
    """为惰性注入生成记忆索引文本。

    格式: "你的记忆索引: profile (昵称:张三), facts (3条), conversations (最近5次摘要)"

    Args:
        user_id: QQ 用户 openid。
    """
    db = await _ensure_db()
    parts: list[str] = []

    # 用户资料
    profile = await get(f"user/{user_id}/profile", "profile")
    if profile:
        try:
            pv = json.loads(profile["value"])
            nickname = pv.get("nickname", "")
            if nickname:
                parts.append(f"profile (昵称:{nickname})")
            else:
                parts.append("profile (1条)")
        except (json.JSONDecodeError, KeyError):
            parts.append("profile (1条)")

    # 用户事实（过滤已纠正条目）
    cursor = await db.execute(
        "SELECT COUNT(*) as cnt FROM entries WHERE namespace=? AND expired=0 AND corrected=0",
        (f"user/{user_id}/facts",),
    )
    facts_count = (await cursor.fetchone())["cnt"]
    if facts_count > 0:
        parts.append(f"facts ({facts_count}条)")

    # 对话摘要（过滤已纠正条目）
    cursor = await db.execute(
        "SELECT value FROM entries WHERE namespace=? AND expired=0 AND corrected=0 "
        "ORDER BY created_at DESC LIMIT 5",
        (f"user/{user_id}/conversations",),
    )
    conv_rows = await cursor.fetchall()
    if conv_rows:
        summaries = []
        for r in conv_rows:
            try:
                cv = json.loads(r["value"])
                summaries.append(cv.get("summary", "")[:60])
            except (json.JSONDecodeError, KeyError):
                pass
        if summaries:
            parts.append(f"conversations (最近{len(summaries)}次: {'; '.join(summaries)})")

    if not parts:
        return "你的记忆索引: (空)"

    return "你的记忆索引: " + ", ".join(parts)


# ==================== Phase 1: 每日批处理 ====================

async def _daily_link_scan() -> dict:
    """每日扫描新建记忆，通过 LLM 批量发现语义关联边。

    扫描最近 24h 的新建条目，批量调用 LLM_FAST_MODEL 判断语义关联。
    """
    db = await _ensure_db()
    now = datetime.now(timezone.utc).isoformat()
    day_ago = datetime.now(timezone.utc)
    day_ago = day_ago.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    # 获取最近 24h 的新增条目
    cursor = await db.execute(
        """SELECT id, namespace, key, value, topic_tags, about_person
           FROM entries WHERE created_at >= ? AND expired=0 AND corrected=0
           LIMIT 100""",
        (day_ago,),
    )
    new_entries = [dict(r) for r in await cursor.fetchall()]

    if len(new_entries) < 2:
        return {"links_created": 0}

    # 构建 LLM prompt
    items_text = []
    for e in new_entries:
        try:
            val = e.get("value", "")
            if len(val) > 60:
                val = val[:60] + "..."
        except Exception:
            val = ""
        items_text.append(f"[{e['id']}] {e['namespace']}/{e['key']}: {val}")

    from config import LLM_API_KEY, LLM_BASE_URL, LLM_FAST_MODEL
    from openai import AsyncOpenAI

    prompt = f"""从以下记忆条目中发现语义关联对。

条目:
{chr(10).join(items_text)}

输出 JSON: {{"links": [{{"from": id1, "to": id2, "type": "same_topic|extends|contradicts"}}]}}

规则:
- same_topic: 讨论同一话题
- extends: 一条是另一条的延伸/补充
- contradicts: 内容矛盾
- 仅标记高度相关的对"""

    try:
        client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=LLM_FAST_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.1,
            ),
            timeout=10.0,
        )
        raw = response.choices[0].message.content or ""
        try:
            if "```json" in raw:
                raw = raw[raw.index("```json") + 7:]
                if "```" in raw:
                    raw = raw[:raw.index("```")]
            elif "```" in raw:
                raw = raw[raw.index("```") + 3:]
                if "```" in raw:
                    raw = raw[:raw.index("```")]
            data = json.loads(raw.strip())
            links = data.get("links", [])

            count = 0
            for link in links:
                try:
                    await db.execute(
                        """INSERT OR IGNORE INTO memory_links
                           (from_id, to_id, relation_type, strength, source, created_at)
                           VALUES (?, ?, ?, 1.0, 'llm', ?)""",
                        (link["from"], link["to"], link.get("type", "same_topic"), now),
                    )
                    count += 1
                except Exception:
                    pass

            await db.commit()
            logger.info("每日关联扫描: 生成 %d 条语义边", count)
            return {"links_created": count}
        except (json.JSONDecodeError, ValueError, KeyError):
            return {"links_created": 0}
    except asyncio.TimeoutError:
        logger.warning("每日关联扫描超时 — LLM 未在 10s 内响应")
        return {"links_created": 0}
    except Exception:
        logger.warning("每日关联扫描失败", exc_info=True)
        return {"links_created": 0}


async def _daily_batch_tag() -> dict:
    """每日批量补标未标记的记忆条目。

    扫描 entity_type/topic_tags 为空的新条目，LLM 批量标注。
    同时发现社交关系边 (T034)。
    """
    db = await _ensure_db()
    now = datetime.now(timezone.utc).isoformat()

    # 扫描未标记条目（entity_type 为空）
    cursor = await db.execute(
        """SELECT id, namespace, key, value, topic_tags
           FROM entries WHERE (entity_type IS NULL OR topic_tags IS NULL)
           AND expired=0 AND corrected=0
           LIMIT 50""",
    )
    unlabeled = [dict(r) for r in await cursor.fetchall()]

    if not unlabeled:
        return {"tagged": 0, "relationships": 0}

    items_text = []
    for e in unlabeled:
        try:
            val = e.get("value", "")
            if len(val) > 80:
                val = val[:80] + "..."
        except Exception:
            val = ""
        items_text.append(f"[{e['id']}] {e['namespace']}/{e['key']}: {val}")

    from config import LLM_API_KEY, LLM_BASE_URL, LLM_FAST_MODEL
    from openai import AsyncOpenAI

    prompt = f"""为以下记忆条目标注实体类型和话题标签。

条目:
{chr(10).join(items_text)}

输出 JSON: {{"entries": [{{"id": id, "entity_type": "...", "topic_tags": [...], "about_person": null, "relationships": []}}]}}

entity_type 选项: person_attribute | factual_knowledge | event | relationship
topic_tags 示例: ["宠物","猫","医疗"]
relationships 可选: [{{"from": "entry_id", "to": "entry_id", "type": "social_colleague|social_friend|social_family"}}]"""

    try:
        client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=LLM_FAST_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
                temperature=0.1,
            ),
            timeout=15.0,
        )
        raw = response.choices[0].message.content or ""
        try:
            if "```json" in raw:
                raw = raw[raw.index("```json") + 7:]
                if "```" in raw:
                    raw = raw[:raw.index("```")]
            elif "```" in raw:
                raw = raw[raw.index("```") + 3:]
                if "```" in raw:
                    raw = raw[:raw.index("```")]
            data = json.loads(raw.strip())
            entries_data = data.get("entries", [])

            tagged = 0
            rel_count = 0
            for ed in entries_data:
                eid = ed.get("id")
                if eid:
                    entity_type = ed.get("entity_type")
                    topic_tags = ed.get("topic_tags")
                    about_person = ed.get("about_person")
                    if entity_type:
                        await db.execute(
                            "UPDATE entries SET entity_type=? WHERE id=?",
                            (entity_type, eid),
                        )
                    if topic_tags:
                        await db.execute(
                            "UPDATE entries SET topic_tags=? WHERE id=?",
                            (json.dumps(topic_tags), eid),
                        )
                    if about_person:
                        await db.execute(
                            "UPDATE entries SET about_person=? WHERE id=?",
                            (about_person, eid),
                        )
                    tagged += 1

                    # T034: 社交关系边
                    for rel in ed.get("relationships", []):
                        try:
                            await db.execute(
                                """INSERT OR IGNORE INTO memory_links
                                   (from_id, to_id, relation_type, strength, source, created_at)
                                   VALUES (?, ?, ?, 0.8, 'llm', ?)""",
                                (rel["from"], rel["to"], rel.get("type", "social_colleague"), now),
                            )
                            rel_count += 1
                        except Exception:
                            pass

            await db.commit()
            logger.info("每日批量标注: tagged=%d, relationships=%d", tagged, rel_count)
            return {"tagged": tagged, "relationships": rel_count}
        except (json.JSONDecodeError, ValueError, KeyError):
            return {"tagged": 0, "relationships": 0}
    except asyncio.TimeoutError:
        logger.warning("每日批量标注超时 — LLM 未在 15s 内响应")
        return {"tagged": 0, "relationships": 0}
    except Exception:
        logger.warning("每日批量标注失败", exc_info=True)
        return {"tagged": 0, "relationships": 0}


# ==================== Phase 1: 深刻记忆集群 ====================

async def _check_cluster_trigger() -> dict:
    """T039: 检查触发集群条件的记忆。

    查询 access_log 中指定窗口内同 topic_tags 条目被访问次数 >= CLUSTER_TRIGGER_MIN_ACCESS。
    """
    from config import CLUSTER_TRIGGER_DAYS, CLUSTER_TRIGGER_MIN_ACCESS

    db = await _ensure_db()
    now = datetime.now(timezone.utc).isoformat()

    # 查找窗口内频繁访问的条目
    cursor = await db.execute(
        """SELECT e.id, e.topic_tags, COUNT(al.id) as access_count
           FROM entries e
           INNER JOIN access_log al ON e.id = al.entry_id
           WHERE al.accessed_at >= datetime(?, ?)
           AND e.expired = 0 AND e.corrected = 0
           AND e.topic_tags IS NOT NULL AND e.topic_tags != '[]'
           GROUP BY e.id
           HAVING access_count >= ?""",
        (now, f"-{CLUSTER_TRIGGER_DAYS} days", CLUSTER_TRIGGER_MIN_ACCESS),
    )
    frequent = [dict(r) for r in await cursor.fetchall()]

    if len(frequent) < 2:
        return {"clusters_created": 0}

    # 按相同 topic_tags 分组
    tag_groups: dict[str, list] = {}
    for entry in frequent:
        try:
            tags = json.loads(entry["topic_tags"])
            tag_key = ",".join(sorted(tags))
            if tag_key not in tag_groups:
                tag_groups[tag_key] = []
            tag_groups[tag_key].append(entry["id"])
        except (json.JSONDecodeError, TypeError):
            pass

    # 筛选有 ≥2 个成员的组
    candidates = {k: v for k, v in tag_groups.items() if len(v) >= 2}
    if not candidates:
        return {"clusters_created": 0}

    # T040: LLM 确认
    clusters_created = await _llm_confirm_cluster(candidates)
    return {"clusters_created": clusters_created}


async def _llm_confirm_cluster(candidates: dict[str, list]) -> int:
    """T040: LLM 语义验证候选集群是否形成深层关联。

    Args:
        candidates: {tag_key: [entry_ids]}

    Returns:
        创建的集群数
    """
    db = await _ensure_db()
    now = datetime.now(timezone.utc).isoformat()
    clusters_created = 0

    from config import LLM_API_KEY, LLM_BASE_URL, LLM_FAST_MODEL
    from openai import AsyncOpenAI

    for tag_key, entry_ids in candidates.items():
        # 获取条目内容
        placeholders = ",".join("?" * len(entry_ids))
        cursor = await db.execute(
            f"SELECT id, value FROM entries WHERE id IN ({placeholders}) AND expired=0",
            entry_ids,
        )
        entries = [dict(r) for r in await cursor.fetchall()]
        if len(entries) < 2:
            continue

        items_text = []
        for e in entries:
            try:
                val = e.get("value", "")
                if len(val) > 60:
                    val = val[:60] + "..."
            except Exception:
                val = ""
            items_text.append(f"[{e['id']}] {val}")

        prompt = f"""判断以下记忆是否形成深层语义关联（深刻记忆集群）。

话题标签: {tag_key}

候选记忆:
{chr(10).join(items_text)}

输出 JSON: {{"is_cluster": true/false, "name": "集群名称", "reason": "判断依据"}}

条件:
- is_cluster=true: 3条以上高度相关/反复讨论同一主题/具有情感纽带
- is_cluster=false: 仅是偶然的标签重叠"""

        try:
            client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=LLM_FAST_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=150,
                    temperature=0.1,
                ),
                timeout=5.0,
            )
            raw = response.choices[0].message.content or ""
            try:
                if "```json" in raw:
                    raw = raw[raw.index("```json") + 7:]
                    if "```" in raw:
                        raw = raw[:raw.index("```")]
                elif "```" in raw:
                    raw = raw[raw.index("```") + 3:]
                    if "```" in raw:
                        raw = raw[:raw.index("```")]
                data = json.loads(raw.strip())
                if data.get("is_cluster"):
                    # T041: 写入集群
                    name = data.get("name", f"集群_{tag_key}")
                    member_ids_json = json.dumps(entry_ids)
                    cursor_c = await db.execute(
                        """INSERT INTO memory_clusters (name, decay_curve_override, member_ids, created_at)
                           VALUES (?, 'deep', ?, ?)""",
                        (name, member_ids_json, now),
                    )
                    cluster_id = cursor_c.lastrowid

                    for eid in entry_ids:
                        await db.execute(
                            """INSERT OR IGNORE INTO cluster_members (cluster_id, entry_id, joined_at)
                               VALUES (?, ?, ?)""",
                            (cluster_id, eid, now),
                        )
                        # 更新条目的衰减曲线
                        await db.execute(
                            "UPDATE entries SET decay_curve='deep' WHERE id=?",
                            (eid,),
                        )

                    await db.commit()
                    clusters_created += 1
                    logger.info("深刻记忆集群建立: %s (%d 条)", name, len(entry_ids))
            except (json.JSONDecodeError, ValueError, KeyError):
                pass
        except asyncio.TimeoutError:
            pass
        except Exception:
            logger.exception("集群确认失败")

    return clusters_created


# ==================== Expiry ====================

async def mark_expired(namespace: str, key: str) -> None:
    """标记记忆为过期。"""
    db = await _ensure_db()
    await db.execute(
        "UPDATE entries SET expired=1, updated_at=? WHERE namespace=? AND key=?",
        (datetime.now(timezone.utc).isoformat(), namespace, key),
    )
    await db.commit()


async def cleanup_expired(older_than_days: int = 90) -> int:
    """清理超期记忆。返回删除条数。

    同时清理对应的 FTS5 索引条目。

    Args:
        older_than_days: 过期超过 N 天的记录将被物理删除。
    """
    db = await _ensure_db()
    cutoff = datetime.now(timezone.utc).isoformat()

    # 先获取要删除的 id 列表
    cursor = await db.execute(
        """SELECT id FROM entries
           WHERE expired=1
           AND updated_at < datetime(?, ?)""",
        (cutoff, f"-{older_than_days} days"),
    )
    rows = await cursor.fetchall()
    ids = [r["id"] for r in rows]

    if ids:
        placeholders = ",".join("?" * len(ids))
        await db.execute(
            f"DELETE FROM entries WHERE id IN ({placeholders})", ids
        )
        await db.execute(
            f"DELETE FROM entries_fts WHERE rowid IN ({placeholders})", ids
        )

    await db.commit()
    return len(ids)


# ==================== Decay ====================

async def apply_decay() -> dict:
    """执行记忆衰减 (Phase 1 艾宾浩斯曲线)。

    规则：
    - detail 层: 0-7d 保持, 7-30d 线性衰减, 30-60d 加速, >60d auto_migrate
    - gist 层: 默认 90d, 受 salience 调制延长
    - deep 曲线(集群): 不衰减
    - none 曲线(纠错链): 不衰减
    - FR-2.4: 7天内访问>=3次 → decay_start 延长 15 天

    返回: {expired_count, boosted_count, migrated_count}
    """
    from config import DECAY_DETAIL_DAYS, DECAY_GIST_DAYS, AUTO_MIGRATE_DAYS, ACCESS_BOOST_DAYS, ACCESS_BOOST_MIN

    db = await _ensure_db()
    now_iso = datetime.now(timezone.utc).isoformat()

    expired_count = 0
    boosted_count = 0
    migrated_count = 0

    # --- detail 层衰减 ---
    # > AUTO_MIGRATE_DAYS → 自动模糊化（而非直接过期）
    cursor = await db.execute(
        """UPDATE entries SET auto_migrate=1, updated_at=?
           WHERE expired=0 AND corrected=0
           AND memory_layer='detail' AND decay_curve='standard'
           AND created_at < datetime(?, ?)""",
        (now_iso, now_iso, f"-{AUTO_MIGRATE_DAYS} days"),
    )
    migrated_count = cursor.rowcount

    # detail 层过期：超过 DECAY_DETAIL_DAYS 且有 auto_migrate 标记
    cursor = await db.execute(
        """UPDATE entries SET expired=1, updated_at=?
           WHERE expired=0 AND memory_layer='detail'
           AND created_at < datetime(?, ?)
           AND auto_migrate=1""",
        (now_iso, now_iso, f"-{DECAY_DETAIL_DAYS} days"),
    )
    # 注意：auto_migrate 的条目如果还没过期，先不标记 expired
    # 它们在 memory_layer 迁移到 gist 后会走 gist 的衰减规则

    # --- gist 层衰减 ---
    # salience > 5 → 衰减窗口延长 50%
    # salience > 7 → 衰减窗口加倍
    cursor = await db.execute(
        """UPDATE entries SET expired=1, updated_at=?
           WHERE expired=0 AND memory_layer='gist' AND decay_curve='standard'
           AND created_at < datetime(?, ?)
           AND salience <= 5""",
        (now_iso, now_iso, f"-{DECAY_GIST_DAYS} days"),
    )
    expired_count += cursor.rowcount

    cursor = await db.execute(
        """UPDATE entries SET expired=1, updated_at=?
           WHERE expired=0 AND memory_layer='gist' AND decay_curve='standard'
           AND created_at < datetime(?, ?)
           AND salience > 5 AND salience <= 7""",
        (now_iso, now_iso, f"-{int(DECAY_GIST_DAYS * 1.5)} days"),
    )
    expired_count += cursor.rowcount

    cursor = await db.execute(
        """UPDATE entries SET expired=1, updated_at=?
           WHERE expired=0 AND memory_layer='gist' AND decay_curve='standard'
           AND created_at < datetime(?, ?)
           AND salience > 7""",
        (now_iso, now_iso, f"-{DECAY_GIST_DAYS * 2} days"),
    )
    expired_count += cursor.rowcount

    # --- FR-2.4: 高频访问 boost ---
    # 7 天内访问 >= 3 次 → decay_start 延长 15 天
    cursor = await db.execute(
        """UPDATE entries SET updated_at=datetime(COALESCE(updated_at, created_at), '+15 days')
           WHERE expired=0 AND decay_curve='standard'
           AND access_count >= ?
           AND last_access >= datetime(?, ?)""",
        (ACCESS_BOOST_MIN, now_iso, f"-{ACCESS_BOOST_DAYS} days"),
    )
    boosted_count = cursor.rowcount

    # --- 自迁移：auto_migrate=1 的 detail 条目 → gist 层，转换时间描述 ---
    # 将 created_at 的 ISO 时间戳转为模糊时间段描述
    cursor = await db.execute(
        """SELECT id, created_at, value FROM entries
           WHERE auto_migrate=1 AND memory_layer='detail' AND expired=0""",
    )
    to_migrate = [dict(r) for r in await cursor.fetchall()]
    for entry in to_migrate:
        # 转换 value 追加模糊化标记
        try:
            val_data = json.loads(entry["value"])
            if isinstance(val_data, dict):
                val_data["_original_created"] = entry["created_at"]
                val_data["_fuzzy_time"] = _to_fuzzy_time(entry["created_at"])
                val_data["_migrated"] = True
                new_value = json.dumps(val_data, ensure_ascii=False)
            else:
                new_value = entry["value"] + " [已模糊化]"
        except (json.JSONDecodeError, TypeError):
            new_value = entry["value"] + " [已模糊化]"

        await db.execute(
            """UPDATE entries SET memory_layer='gist', value=?, updated_at=?
               WHERE id=?""",
            (new_value, now_iso, entry["id"]),
        )

    await db.commit()
    logger.info(
        "记忆衰减完成: expired=%d, boosted=%d, migrated=%d",
        expired_count, boosted_count, migrated_count,
    )
    return {"expired_count": expired_count, "boosted_count": boosted_count, "migrated_count": migrated_count}


def _to_fuzzy_time(iso_str: str) -> str:
    """将 ISO 时间戳转为模糊时间段描述。

    精度分级：
      0天 → 今天 | 1天 → 昨天 | 2-6天 → X天前
      7-27天 → X周前 | 28-59天 → 上个月
      60-179天 → 几个月前 | 180-364天 → 半年前
      365-729天 → 去年 | >2年 → X年前
    """
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff_days = (now - dt.replace(tzinfo=timezone.utc)).days

        if diff_days == 0:
            return "今天"
        elif diff_days == 1:
            return "昨天"
        elif diff_days < 7:
            return f"{diff_days}天前"
        elif diff_days < 28:
            weeks = diff_days // 7
            return f"大约{weeks}周前" if weeks <= 3 else "几周前"
        elif diff_days < 60:
            return "上个月"
        elif diff_days < 180:
            return "几个月前"
        elif diff_days < 365:
            return "半年前"
        elif diff_days < 730:
            return "去年"
        else:
            years = diff_days // 365
            return f"{years}年前"
    except Exception:
        return "以前"


# ==================== Status ====================

async def status() -> dict:
    """返回记忆库聚合状态。

    Returns:
        {total, active, expired, namespaces: {ns: count}}
    """
    db = await _ensure_db()

    cursor = await db.execute("SELECT COUNT(*) as cnt FROM entries")
    total = (await cursor.fetchone())["cnt"]

    cursor = await db.execute(
        "SELECT COUNT(*) as cnt FROM entries WHERE expired=0"
    )
    active = (await cursor.fetchone())["cnt"]

    expired = total - active

    cursor = await db.execute(
        "SELECT namespace, COUNT(*) as cnt FROM entries "
        "WHERE expired=0 GROUP BY namespace"
    )
    namespaces = {r["namespace"]: r["cnt"] for r in await cursor.fetchall()}

    return {
        "total": total,
        "active": active,
        "expired": expired,
        "namespaces": namespaces,
    }
