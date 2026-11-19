"""SQLite 存储层：建表、连接、元数据。

只用一个文件（默认 data/kb.sqlite3）承载三类数据：
    文档/分块 + 向量 + FTS   → RAG 检索
    entities / relations     → 类型化本体（知识图谱）
    memories                 → 长期记忆（带时间有效性）

MVP 阶段选择 SQLite 而不是“向量库 + 图库 + 文档库”三件套：
单机零部署、事务内跨表一致、备份即拷贝一个文件。演进路径见 DESIGN.md。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .util import iso

SCHEMA_VERSION = 1

SCHEMA: list[str] = [
    """CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,
        source_uri TEXT NOT NULL,
        title TEXT NOT NULL,
        doc_type TEXT NOT NULL DEFAULT 'note',
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        meta_json TEXT NOT NULL DEFAULT '{}'
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_uri ON documents(source_uri)",
    "CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash)",
    """CREATE TABLE IF NOT EXISTS chunks (
        id TEXT PRIMARY KEY,
        doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        ord INTEGER NOT NULL,
        heading_path TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL,
        char_start INTEGER NOT NULL DEFAULT 0,
        char_end INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, ord)",
    # trigram 分词器让中文子串检索可用（需 SQLite >= 3.34）
    """CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
        text, chunk_id UNINDEXED, doc_id UNINDEXED, tokenize='trigram'
    )""",
    """CREATE TABLE IF NOT EXISTS vectors (
        owner_kind TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        model TEXT NOT NULL,
        dim INTEGER NOT NULL,
        vec BLOB NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (owner_kind, owner_id)
    )""",
    """CREATE TABLE IF NOT EXISTS entity_types (
        name TEXT PRIMARY KEY,
        parent TEXT,
        description TEXT NOT NULL DEFAULT '',
        is_core INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS relation_types (
        name TEXT PRIMARY KEY,
        domain TEXT NOT NULL DEFAULT '',
        range TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        symmetric INTEGER NOT NULL DEFAULT 0,
        is_core INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS entities (
        id TEXT PRIMARY KEY,
        type_name TEXT NOT NULL,
        name TEXT NOT NULL,
        name_norm TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        props_json TEXT NOT NULL DEFAULT '{}',
        confidence REAL NOT NULL DEFAULT 0.7,
        mention_count INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type_name)",
    "CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(name_norm)",
    """CREATE TABLE IF NOT EXISTS entity_aliases (
        entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        alias TEXT NOT NULL,
        alias_norm TEXT NOT NULL,
        PRIMARY KEY (alias_norm, entity_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_aliases_norm ON entity_aliases(alias_norm)",
    """CREATE TABLE IF NOT EXISTS relations (
        id TEXT PRIMARY KEY,
        type_name TEXT NOT NULL,
        src_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        dst_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        weight REAL NOT NULL DEFAULT 1.0,
        confidence REAL NOT NULL DEFAULT 0.7,
        evidence TEXT NOT NULL DEFAULT '',
        source_chunk_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_rel_src ON relations(src_id)",
    "CREATE INDEX IF NOT EXISTS idx_rel_dst ON relations(dst_id)",
    "CREATE INDEX IF NOT EXISTS idx_rel_type ON relations(type_name)",
    """CREATE TABLE IF NOT EXISTS mentions (
        id TEXT PRIMARY KEY,
        entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
        surface TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.7,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mentions_chunk ON mentions(chunk_id)",
    "CREATE INDEX IF NOT EXISTS idx_mentions_entity ON mentions(entity_id)",
    """CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        subject TEXT NOT NULL DEFAULT '',
        subject_norm TEXT NOT NULL DEFAULT '',
        subject_entity_id TEXT,
        predicate TEXT NOT NULL DEFAULT '',
        object_text TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL,
        importance REAL NOT NULL DEFAULT 0.5,
        confidence REAL NOT NULL DEFAULT 0.7,
        salience REAL NOT NULL DEFAULT 0.5,
        hits INTEGER NOT NULL DEFAULT 0,
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        superseded_by TEXT,
        source_doc_id TEXT,
        source_chunk_id TEXT,
        created_at TEXT NOT NULL,
        last_access_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memories_subject ON memories(subject_norm, predicate)",
    "CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind)",
    """CREATE TABLE IF NOT EXISTS memories_draft (
        id TEXT PRIMARY KEY,
        action TEXT NOT NULL,
        slot_key TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT '',
        keep_id TEXT,
        drop_ids_json TEXT NOT NULL DEFAULT '[]',
        content TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        similarity REAL NOT NULL DEFAULT 0,
        reviewed TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        applied_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_draft_status ON memories_draft(status)",
    "CREATE INDEX IF NOT EXISTS idx_draft_slot ON memories_draft(slot_key)",
    """CREATE TABLE IF NOT EXISTS terms (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        name_norm TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        synonyms_json TEXT NOT NULL DEFAULT '[]',
        entity_id TEXT,
        kind TEXT NOT NULL DEFAULT 'concept',
        certified INTEGER NOT NULL DEFAULT 0,
        usage_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_terms_norm ON terms(name_norm)",
    "CREATE INDEX IF NOT EXISTS idx_terms_certified ON terms(certified)",
    """CREATE TABLE IF NOT EXISTS ingest_log (
        id TEXT PRIMARY KEY,
        doc_id TEXT,
        status TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        stats_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )""",
]

CORE_ENTITY_TYPES: list[tuple[str, str | None, str]] = [
    ("项目", None, "有明确目标与时间跨度的工作集合"),
    ("人物", None, "自然人"),
    ("组织", None, "团队、部门、公司"),
    ("系统", None, "软件系统、服务、平台、组件"),
    ("数据", None, "数据集、数据库、指标"),
    ("流程", None, "规则、流程、制度"),
    ("决策", None, "经过讨论形成的结论"),
    ("文档", None, "文档、规范、报告"),
    ("事件", None, "会议、事故、里程碑"),
    ("概念", None, "术语与抽象概念"),
]

CORE_RELATION_TYPES: list[tuple[str, str, str, str, int]] = [
    # name, domain, range, description, symmetric
    ("依赖", "", "", "A 的运行或推进依赖 B", 0),
    ("使用", "", "", "A 使用 / 基于 B 构建", 0),
    ("负责", "人物", "", "A 负责 B（负责人）", 0),
    ("参与", "人物", "", "A 参与 B", 0),
    ("属于", "", "", "A 是 B 的一部分", 0),
    ("包含", "", "", "A 包含 B", 0),
    ("替代", "", "", "A 取代了 B", 0),
    ("迁移到", "", "", "A 从旧形态迁移到 B", 0),
    ("产出", "", "", "A 产出 B", 0),
    ("针对", "", "", "决策 / 方案 A 针对对象 B", 0),
    ("相关", "", "", "弱关联（共现推断）", 1),
    ("提及", "", "", "文档 A 提及 B", 0),
]


def connect(path: str | Path, *, create: bool = True) -> sqlite3.Connection:
    p = Path(path)
    if create:
        p.parent.mkdir(parents=True, exist_ok=True)
    elif not p.exists():
        raise FileNotFoundError(f"数据库不存在：{p}（先运行 `kb init`）")
    conn = sqlite3.connect(str(p), isolation_level=None)  # 手动管理事务
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """轻量迁移：表已存在时按需补列（SQLite 没有 ADD COLUMN IF NOT EXISTS）。"""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_schema(conn: sqlite3.Connection) -> None:
    with conn:  # 单事务建表
        for stmt in SCHEMA:
            conn.execute(stmt)
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        _ensure_columns(
            conn,
            "memories_draft",
            {
                "similarity": "REAL NOT NULL DEFAULT 0",
                "reviewed": "TEXT NOT NULL DEFAULT ''",
            },
        )
        _seed_core_types(conn)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('created_at', ?)", (iso(),)
        )


def _seed_core_types(conn: sqlite3.Connection) -> None:
    ts = iso()
    for name, parent, desc in CORE_ENTITY_TYPES:
        conn.execute(
            "INSERT OR IGNORE INTO entity_types(name, parent, description, is_core, created_at)"
            " VALUES(?,?,?,1,?)",
            (name, parent, desc, ts),
        )
    for name, domain, rng, desc, sym in CORE_RELATION_TYPES:
        conn.execute(
            "INSERT OR IGNORE INTO relation_types"
            "(name, domain, range, description, symmetric, is_core, created_at)"
            " VALUES(?,?,?,?,?,1,?)",
            (name, domain, rng, desc, sym, ts),
        )


# ---------------------------------------------------------------- meta


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "documents",
        "chunks",
        "vectors",
        "entities",
        "relations",
        "mentions",
        "memories",
        "entity_types",
        "relation_types",
    ]
    out: dict[str, int] = {}
    for t in tables:
        out[t] = int(conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"])
    return out


def analyzer_stats(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute("SELECT COUNT(*) AS c FROM chunk_fts").fetchone()
    return {"fts_rows": int(row["c"])}
