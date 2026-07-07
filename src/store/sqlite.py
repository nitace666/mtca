"""MTCA SQLite 存储层（v0.4）

提供 6 张主表 + 1 张雾化控制表的 schema、L0 不可变触发器、
性能 PRAGMA 与统一的连接 / 执行 / 查询入口。

设计哲学：
- L0 不可变（用户 / AI 都无权删；用户授权下可物理擦除细节）
- 本地优先：单文件 SQLite（`~/.mtca/mtca.db`），WAL + mmap
- SQL 全部 ? 占位符，禁止 f-string 拼 SQL

公共 API：
- ``MTCA_DB_PATH`` 默认数据库路径常量
- ``init_db(path=None)`` 初始化或打开数据库，返回连接
- ``get_connection(path=None)`` 上下文管理器，自动 commit / rollback
- ``get_stats(path=None)`` 返回 {sessions, messages, segments, views} 行数
- ``execute(sql, params=(), path=None)`` 写操作，返回 lastrowid
- ``query(sql, params=(), path=None)`` 读操作，返回 list[dict]
- ``fog_permit(session_id, called_by='user')`` 授予雾化通道（仅 user）
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Union

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 默认数据库路径：用户目录下 ~/.mtca/mtca.db
MTCA_DB_PATH: Path = Path.home() / ".mtca" / "mtca.db"

# 性能 PRAGMA（M1 验收基线）
# - WAL：高并发读 + 单写者
# - cache_size=-64000：约 64MB 页缓存
# - mmap_size=256MB：读路径走 mmap
# - temp_store=MEMORY：临时表 / 排序走内存
# - synchronous=NORMAL：WAL 模式下安全且快
_PRAGMAS: tuple[str, ...] = (
    "journal_mode=WAL",
    "cache_size=-64000",
    "mmap_size=268435456",
    "temp_store=MEMORY",
    "synchronous=NORMAL",
    "foreign_keys=ON",
)

# ---------------------------------------------------------------------------
# Schema（DDL）
# ---------------------------------------------------------------------------

_SCHEMA_SQL: str = """
-- 表 1: sessions（L0-A 会话级，AI 不可变）
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    started_at     INTEGER NOT NULL,
    ended_at       INTEGER,
    topic_label    TEXT,
    message_count  INTEGER DEFAULT 0,
    token_estimate INTEGER DEFAULT 0,
    agent_source   TEXT,
    is_archived    INTEGER DEFAULT 0,
    is_important   INTEGER DEFAULT 0,
    cycle_tag      TEXT,
    current_score  REAL DEFAULT 100,
    created_at     INTEGER DEFAULT (strftime('%s','now') * 1000)
);

CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_score   ON sessions(current_score);

-- 表 2: messages（L0-A 消息级，AI 不可变 / 用户可雾化）
-- content 可空：雾化操作物理擦除后置 NULL（DATA_MODEL.md §2.3）
CREATE TABLE IF NOT EXISTS messages (
    message_id     TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    role           TEXT NOT NULL,
    content        TEXT,
    tool_calls     TEXT,
    tool_results   TEXT,
    token_count    INTEGER,
    created_at     INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);

-- FTS5 全文索引（unicode61 分词，兼容中日英）
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='rowid',
    tokenize='unicode61'
);

-- FTS5 同步触发器
CREATE TRIGGER IF NOT EXISTS messages_fts_ai
AFTER INSERT ON messages
BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_ad
AFTER DELETE ON messages
BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES('delete', old.rowid, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_au
AFTER UPDATE ON messages
BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES('delete', old.rowid, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;

-- 表 3: segments（L0-B 段落级，含 v0.4 雾化 / 静默 / 矛盾字段）
CREATE TABLE IF NOT EXISTS segments (
    segment_id           TEXT PRIMARY KEY,
    session_id           TEXT NOT NULL,
    start_msg_seq        INTEGER NOT NULL,
    end_msg_seq          INTEGER NOT NULL,
    start_at             INTEGER NOT NULL,
    end_at               INTEGER NOT NULL,
    gap_to_next          INTEGER,
    weak_merged          INTEGER DEFAULT 0,
    topic_label          TEXT,
    current_tier         TEXT DEFAULT 'L0',
    current_score        REAL DEFAULT 100,
    -- 雾化字段（v0.4 新增）
    fog_state            TEXT DEFAULT 'clear',
    fog_at               INTEGER,
    fog_anchor           TEXT,
    -- 静默态字段（v0.4 新增）
    silence_state        TEXT DEFAULT 'active',
    promoted_at          INTEGER,
    ref_count            INTEGER DEFAULT 0,
    last_ask_at          INTEGER,
    user_retention_days  INTEGER,
    long_silent          INTEGER DEFAULT 0,
    -- 矛盾检测字段（v0.4 新增）
    superseded_by        TEXT,
    supersedes_count     INTEGER DEFAULT 0,
    contradiction_level  TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_segments_session ON segments(session_id);
CREATE INDEX IF NOT EXISTS idx_segments_time    ON segments(start_at);
CREATE INDEX IF NOT EXISTS idx_segments_fog     ON segments(fog_state);
CREATE INDEX IF NOT EXISTS idx_segments_silence ON segments(silence_state);
CREATE INDEX IF NOT EXISTS idx_segments_super   ON segments(superseded_by);

-- 表 3.5: segment_relations（v0.4 关系图谱）
CREATE TABLE IF NOT EXISTS segment_relations (
    relation_id    TEXT PRIMARY KEY,
    seg_a_id       TEXT NOT NULL,
    seg_b_id       TEXT NOT NULL,
    relation_type  TEXT NOT NULL,
    weight         REAL DEFAULT 1.0,
    auto_created   INTEGER DEFAULT 0,
    created_at     INTEGER NOT NULL,
    expires_at     INTEGER,
    FOREIGN KEY (seg_a_id) REFERENCES segments(segment_id),
    FOREIGN KEY (seg_b_id) REFERENCES segments(segment_id)
);

CREATE INDEX IF NOT EXISTS idx_relations_a    ON segment_relations(seg_a_id);
CREATE INDEX IF NOT EXISTS idx_relations_b    ON segment_relations(seg_b_id);
CREATE INDEX IF NOT EXISTS idx_relations_type ON segment_relations(relation_type);

-- 表 4: views（L1/L2/L3 视图，可变；含 v0.4 重写字段）
CREATE TABLE IF NOT EXISTS views (
    view_id        TEXT PRIMARY KEY,
    segment_id     TEXT NOT NULL,
    tier           TEXT NOT NULL,
    content        TEXT NOT NULL,
    emotion_tag    TEXT,
    embedding      BLOB,
    created_at     INTEGER NOT NULL,
    expires_at     INTEGER,
    is_stale       INTEGER DEFAULT 0,
    stale_reason   TEXT,
    regen_count    INTEGER DEFAULT 0,
    FOREIGN KEY (segment_id) REFERENCES segments(segment_id)
);

CREATE INDEX IF NOT EXISTS idx_views_segment ON views(segment_id);
CREATE INDEX IF NOT EXISTS idx_views_tier    ON views(tier);
CREATE INDEX IF NOT EXISTS idx_views_stale   ON views(is_stale);

-- 表 5: score_events（打分审计，仅追加）
CREATE TABLE IF NOT EXISTS score_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id  TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    delta       REAL,
    old_score   REAL,
    new_score   REAL,
    reason      TEXT,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_score_events_segment
    ON score_events(segment_id, created_at);

-- 表 7: settings（C1 配置持久化：DB 优先，TOML 备份）
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_settings_key ON settings(key);

-- 表 8: facts（C1 LLM 提炼的事实，可变；非 L0）
CREATE TABLE IF NOT EXISTS facts (
    fact_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    segment_id  TEXT,
    content     TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'extracted',
    confidence  REAL DEFAULT 1.0,
    tags        TEXT,
    created_at  INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (segment_id) REFERENCES segments(segment_id)
);

CREATE INDEX IF NOT EXISTS idx_facts_session ON facts(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_facts_segment ON facts(segment_id);

-- facts FTS5（轻量级全文搜索，给 recall 用）
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    content,
    content='facts',
    content_rowid='rowid',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS facts_fts_ai
AFTER INSERT ON facts
BEGIN
    INSERT INTO facts_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS facts_fts_ad
AFTER DELETE ON facts
BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content)
    VALUES('delete', old.rowid, old.content);
END;

CREATE TRIGGER IF NOT EXISTS facts_fts_au
AFTER UPDATE ON facts
BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content)
    VALUES('delete', old.rowid, old.content);
    INSERT INTO facts_fts(rowid, content) VALUES (new.rowid, new.content);
END;

-- 表 6: fog_session（雾化通道控制表，授权期间存在记录即放行 UPDATE）
CREATE TABLE IF NOT EXISTS fog_session (
    session_id  TEXT,
    enabled_at  INTEGER,
    enabled_by  TEXT
);

-- 触发器 1：L0-细节不可变（AI 通道；fog 通道例外）
-- 使用 NEW.session_id 避免 SQLite 触发器对 messages.<col> 别名的解析问题
CREATE TRIGGER IF NOT EXISTS no_update_messages_ai
BEFORE UPDATE ON messages
WHEN NOT EXISTS (
    SELECT 1 FROM fog_session
    WHERE session_id = NEW.session_id AND session_id IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'L0 不可变，禁止 UPDATE messages（fog 通道除外）');
END;

-- 触发器 2：禁止删除消息
CREATE TRIGGER IF NOT EXISTS no_delete_messages
BEFORE DELETE ON messages
BEGIN
    SELECT RAISE(ABORT, 'L0 不可变，禁止 DELETE messages');
END;

-- 触发器 3：L0-骨架字段不可变（topic_label / start_at / fog_anchor）
CREATE TRIGGER IF NOT EXISTS no_update_skeleton
BEFORE UPDATE OF topic_label, start_at, fog_anchor ON segments
BEGIN
    SELECT RAISE(ABORT, 'L0-骨架 不可变，禁止修改 topic_label / start_at / fog_anchor');
END;
"""


# ---------------------------------------------------------------------------
# 工具：路径解析 + PRAGMA 应用
# ---------------------------------------------------------------------------


def _resolve_path(path: Optional[Union[Path, str]]) -> Path:
    """解析数据库路径；None 时返回 MTCA_DB_PATH。"""
    if path is None:
        return MTCA_DB_PATH
    return Path(path)


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """对一条连接依次应用 PRAGMA；失败忽略（部分 PRAGMA 不支持 :memory:）。"""
    for pragma in _PRAGMAS:
        try:
            conn.execute(f"PRAGMA {pragma}")
        except sqlite3.DatabaseError:
            # :memory: 等场景部分 PRAGMA 无法应用，跳过即可
            pass


# ---------------------------------------------------------------------------
# 工具：M2.5.1 schema 迁移（幂等 ALTER TABLE）
# ---------------------------------------------------------------------------


def _get_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    """返回指定表的列名集合（PRAGMA table_info）。"""
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """M2.5.1：segments 表加 5 个字段用于 4 象限评分（向后兼容）。

    新增列：
        - urgency_level    REAL    DEFAULT 0.5
        - importance_level REAL    DEFAULT 0.5
        - emotion_tag      TEXT    DEFAULT NULL
        - expires_at_ms    INTEGER DEFAULT NULL
        - urgent_state     TEXT    DEFAULT NULL

    索引：
        idx_segments_urgent：partial index（urgent_state IS NOT NULL），用于
        紧急跟踪模块的过期扫描查询。

    幂等：通过 PRAGMA 检查列是否存在，重复执行无副作用。
    """
    cols = _get_table_columns(conn, "segments")
    migrations: list[tuple[str, str]] = [
        ("urgency_level",   "REAL    DEFAULT 0.5"),
        ("importance_level", "REAL    DEFAULT 0.5"),
        ("emotion_tag",      "TEXT    DEFAULT NULL"),
        ("expires_at_ms",    "INTEGER DEFAULT NULL"),
        ("urgent_state",     "TEXT    DEFAULT NULL"),
    ]
    for col_name, col_def in migrations:
        if col_name not in cols:
            conn.execute(f"ALTER TABLE segments ADD COLUMN {col_name} {col_def}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_segments_urgent "
        "ON segments(urgent_state) WHERE urgent_state IS NOT NULL"
    )


def _migrate_to_v3_add_vector_cache(conn: sqlite3.Connection) -> None:
    """M3-5：加 vector_cache 表存 segment embedding（Sidecar cache,避免重复调 Ollama）。

    表结构：
        segment_id   TEXT    NOT NULL   -- 段落 ID
        chunk_idx    INTEGER NOT NULL   -- 长段切块后的块序号（从 0 开始）
        embedding    BLOB    NOT NULL   -- 768 floats × 4 字节 (struct.pack little-endian)
        model_hash   TEXT    NOT NULL   -- 模型版本指纹（升级 embedding 模型时区分旧/新数据）
        ts           INTEGER NOT NULL   -- 写入时间（毫秒）
        PRIMARY KEY (segment_id, chunk_idx)

    幂等：通过 _get_table_columns 检查 vector_cache 是否存在,重复执行无副作用。

    不动 v2 migration（向后兼容 M2.5.1 既有迁移路径）。
    """
    cols = _get_table_columns(conn, "vector_cache")
    if not cols:
        conn.execute(
            "CREATE TABLE vector_cache ("
            "segment_id TEXT NOT NULL, "
            "chunk_idx INTEGER NOT NULL, "
            "embedding BLOB NOT NULL, "
            "model_hash TEXT NOT NULL, "
            "ts INTEGER NOT NULL, "
            "PRIMARY KEY (segment_id, chunk_idx)"
            ")"
        )

def _migrate_to_v4_fts5_trigram(conn: sqlite3.Connection) -> None:
    """M3-5 Step 8 B2 修复：messages_fts 从 unicode61 改 trigram（中文 token 化生效）。

    背景：
    - unicode61 对中文按 Unicode word boundary 分词，但 CJK 连续字符无 word boundary，
      整段中文当 1 个 long token。FTS5 MATCH 要求 query 与 token 完全相等才能命中，
      导致中文 query 命中率 ~0。
    - trigram 是 FTS5 内置 tokenize（无需新依赖），按 3 字符 trigrams 索引中文字符串。
      query 切 trigrams 与 content trigrams 重叠即命中，query ≥ 3 字符即可工作。

    影响：
    - messages_fts 重建（DROP + CREATE + 从 messages 表 rebuild）
    - 附属表 messages_fts_data / messages_fts_idx / messages_fts_docsize / messages_fts_config
      也 DROP（外键依附）
    - 触发器 messages_fts_ai/_ad/_au 也重建（DELETE-INSERT 同步逻辑不变）

    幂等：检查现有 messages_fts 的 CREATE TABLE SQL 是否含 tokenize='trigram'。
    - 已是 trigram：no-op
    - 是 unicode61 或其他：执行 DROP+CREATE+rebuild

    设 PRAGMA user_version = 4（schema 版本标记），便于外部检测。
    """
    # 检查现有 messages_fts 的 tokenize
    cur = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    )
    row = cur.fetchone()
    if row is not None and row[0] and "tokenize" in row[0] and "trigram" in row[0]:
        # 已是 trigram，无需迁移
        conn.execute("PRAGMA user_version = 4")
        return

    # DROP 旧 messages_fts 及附属表 / 触发器
    conn.executescript("""
        DROP TABLE IF EXISTS messages_fts;
        DROP TABLE IF EXISTS messages_fts_data;
        DROP TABLE IF EXISTS messages_fts_idx;
        DROP TABLE IF EXISTS messages_fts_docsize;
        DROP TABLE IF EXISTS messages_fts_config;
        DROP TRIGGER IF EXISTS messages_fts_ai;
        DROP TRIGGER IF EXISTS messages_fts_ad;
        DROP TRIGGER IF EXISTS messages_fts_au;
    """)

    # CREATE 新 messages_fts（tokenize='trigram'）+ 同步触发器
    conn.executescript("""
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content,
            content='messages',
            content_rowid='rowid',
            tokenize='trigram'
        );
        CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages
        BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
        END;
        CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages
        BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content)
            VALUES('delete', old.rowid, old.content);
        END;
        CREATE TRIGGER messages_fts_au AFTER UPDATE ON messages
        BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content)
            VALUES('delete', old.rowid, old.content);
            INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
        END;
    """)

    # 从 messages 表 rebuild 索引（仅当 messages 表存在且非空）
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if table_check is not None:
        row_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if row_count > 0:
            conn.execute(
                "INSERT INTO messages_fts(rowid, content) "
                "SELECT rowid, content FROM messages"
            )

    # 标记 schema 版本
    conn.execute("PRAGMA user_version = 4")



# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------


def init_db(path: Optional[Union[Path, str]] = None) -> sqlite3.Connection:
    """初始化或打开数据库，应用 PRAGMA 并创建 schema。

    参数：
        path: 数据库文件路径；None 时使用 MTCA_DB_PATH。

    返回：
        已开启 row_factory、行级外键、PRAGMA 的 sqlite3.Connection。

    异常：
        RuntimeError：写入失败时抛出。
    """
    db_path = _resolve_path(path)
    try:
        # 文件路径且不是 :memory: 时确保父目录存在
        if str(db_path) != ":memory:" and not db_path.name.startswith(":"):
            db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn)
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        # M2.5.1 schema 迁移：旧库 ALTER TABLE 5 字段（幂等）
        _migrate_to_v2(conn)
        # M3-5 schema 迁移：加 vector_cache 表（Sidecar embedding 缓存,幂等）
        _migrate_to_v3_add_vector_cache(conn)
        # Step 8 B2 修复：messages_fts 改 trigram（中文 token 化）
        _migrate_to_v4_fts5_trigram(conn)
        conn.commit()
    except sqlite3.Error as exc:
        raise RuntimeError(f"初始化数据库失败：{exc}") from exc
    return conn


@contextmanager
def get_connection(
    path: Optional[Union[Path, str]] = None,
) -> Iterator[sqlite3.Connection]:
    """上下文管理器：自动 commit / rollback / close。

    用法：
        with get_connection() as conn:
            conn.execute(...)
    """
    db_path = _resolve_path(path)
    try:
        if str(db_path) != ":memory:" and not db_path.name.startswith(":"):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        raise RuntimeError(f"打开数据库失败：{exc}") from exc

    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    # M2.5.1 schema 迁移：连接即升级（向后兼容旧库，幂等）
    try:
        _migrate_to_v2(conn)
    except sqlite3.Error:
        pass  # 表不存在（全新 DB 尚未 init_db），由后续 init_db 处理
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_stats(path: Optional[Union[Path, str]] = None) -> dict[str, int]:
    """返回核心表的行数统计。"""
    counts: dict[str, int] = {}
    targets: tuple[str, ...] = ("sessions", "messages", "segments", "views")
    with get_connection(path) as conn:
        cur = conn.cursor()
        for table in targets:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = int(cur.fetchone()[0])
    return counts


def execute(
    sql: str,
    params: Any = (),
    path: Optional[Union[Path, str]] = None,
) -> int:
    """执行单条写 SQL，返回 lastrowid（INSERT）或受影响行数。

    异常：
        RuntimeError：IntegrityError / OperationalError 统一转换为中文消息。
    """
    try:
        with get_connection(path) as conn:
            cur = conn.execute(sql, params)
            # INSERT 返回 lastrowid；UPDATE/DELETE 返回受影响行数
            if cur.lastrowid:
                return int(cur.lastrowid)
            return int(cur.rowcount or 0)
    except sqlite3.IntegrityError as exc:
        raise RuntimeError(f"完整性约束失败：{exc}") from exc
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"数据库操作失败：{exc}") from exc


def query(
    sql: str,
    params: Any = (),
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """执行单条读 SQL，返回 list[dict]（每行一个 dict）。

    异常：
        RuntimeError：IntegrityError / OperationalError 统一转换。
    """
    try:
        with get_connection(path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.IntegrityError as exc:
        raise RuntimeError(f"完整性约束失败：{exc}") from exc
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"数据库操作失败：{exc}") from exc


def fog_permit(
    session_id: str,
    called_by: str = "user",
    path: Optional[Union[Path, str]] = None,
) -> None:
    """授予雾化通道权限。

    校验：
        called_by 必须严格等于 'user'；其他值（'ai' / 'system' / ...）直接
        抛出 PermissionError，AI 无权擦除 L0-细节。

    行为：
        通过校验后，向 fog_session 控制表写入一行授权记录；同一会话
        后续的 UPDATE messages 操作将被触发器放行。
    """
    if called_by != "user":
        raise PermissionError(
            f"雾化通道拒绝调用方 '{called_by}'，仅 'user' 允许"
        )
    now_ms = int(time.time() * 1000)
    # 复用 execute 的统一错误转换
    execute(
        "INSERT INTO fog_session (session_id, enabled_at, enabled_by) "
        "VALUES (?, ?, ?)",
        (session_id, now_ms, called_by),
        path=path,
    )


def fog_revoke(session_id: str, path: Optional[Union[Path, str]] = None) -> int:
    """撤销某会话的雾化通道授权（事务结束后调用）。

    返回被删除的行数。
    """
    try:
        with get_connection(path) as conn:
            cur = conn.execute(
                "DELETE FROM fog_session WHERE session_id = ?",
                (session_id,),
            )
            return int(cur.rowcount or 0)
    except sqlite3.Error as exc:
        raise RuntimeError(f"撤销雾化通道失败：{exc}") from exc


__all__ = [
    "MTCA_DB_PATH",
    "init_db",
    "get_connection",
    "get_stats",
    "execute",
    "query",
    "fog_permit",
    "fog_revoke",
    "_get_table_columns",
    "_migrate_to_v2",
]