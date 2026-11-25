"""KB storage: SQLite default + PostgreSQL shared tables (kb_id + RLS).

只用一个文件（默认 data/kb.sqlite3）承载三类数据：
    文档/分块 + 向量 + FTS   → RAG 检索
    entities / relations     → 类型化本体（知识图谱）
    memories                 → 长期记忆（带时间有效性）

MVP 阶段选择 SQLite 而不是“向量库 + 图库 + 文档库”三件套：
单机零部署、事务内跨表一致、备份即拷贝一个文件。演进路径见 DESIGN.md。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

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
        "symmetric" INTEGER NOT NULL DEFAULT 0,
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



# PostgreSQL schema. One database, one shared table set, scoped by kb_id.
# RLS makes all existing queries kb-isolated without rewriting every SQL.
PG_SCHEMA: list[str] = [
    """CREATE TABLE IF NOT EXISTS meta (
        kb_id TEXT NOT NULL DEFAULT current_setting('app.kb_id', true),
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY(kb_id, key)
    )""",
    """CREATE TABLE IF NOT EXISTS documents (
        kb_id TEXT NOT NULL DEFAULT current_setting('app.kb_id', true),
        id TEXT NOT NULL,
        source_uri TEXT NOT NULL,
        title TEXT NOT NULL,
        doc_type TEXT NOT NULL DEFAULT 'note',
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        meta_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY(kb_id, id)
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_kb_uri ON documents(kb_id, source_uri)",
    "CREATE INDEX IF NOT EXISTS idx_documents_kb_hash ON documents(kb_id, content_hash)",
    """CREATE TABLE IF NOT EXISTS chunks (
        kb_id TEXT NOT NULL DEFAULT current_setting('app.kb_id', true),
        id TEXT NOT NULL,
        doc_id TEXT NOT NULL,
        ord INTEGER NOT NULL,
        heading_path TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL,
        char_start INTEGER NOT NULL DEFAULT 0,
        char_end INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        PRIMARY KEY(kb_id, id),
        FOREIGN KEY(kb_id, doc_id) REFERENCES documents(kb_id, id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chunks_kb_doc ON chunks(kb_id, doc_id, ord)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_text_trgm ON chunks USING gin (text gin_trgm_ops)",
    """CREATE TABLE IF NOT EXISTS chunk_fts (
        kb_id TEXT NOT NULL DEFAULT current_setting('app.kb_id', true),
        text TEXT NOT NULL,
        chunk_id TEXT NOT NULL,
        doc_id TEXT NOT NULL,
        PRIMARY KEY(kb_id, chunk_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chunk_fts_kb_text ON chunk_fts USING gin (text gin_trgm_ops)",
    """CREATE TABLE IF NOT EXISTS vectors (
        kb_id TEXT NOT NULL DEFAULT current_setting('app.kb_id', true),
        owner_kind TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        model TEXT NOT NULL,
        dim INTEGER NOT NULL,
        vec vector(256) NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(kb_id, owner_kind, owner_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_vectors_vec_hnsw ON vectors USING hnsw (vec vector_cosine_ops)",
    """CREATE TABLE IF NOT EXISTS entity_types (
        kb_id TEXT NOT NULL DEFAULT current_setting('app.kb_id', true),
        name TEXT NOT NULL,
        parent TEXT,
        description TEXT NOT NULL DEFAULT '',
        is_core INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        PRIMARY KEY(kb_id, name)
    )""",
    """CREATE TABLE IF NOT EXISTS relation_types (
        kb_id TEXT NOT NULL DEFAULT current_setting('app.kb_id', true),
        name TEXT NOT NULL,
        domain TEXT NOT NULL DEFAULT '',
        range TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        "symmetric" INTEGER NOT NULL DEFAULT 0,
        is_core INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        PRIMARY KEY(kb_id, name)
    )""",
    """CREATE TABLE IF NOT EXISTS entities (
        kb_id TEXT NOT NULL DEFAULT current_setting('app.kb_id', true),
        id TEXT NOT NULL,
        type_name TEXT NOT NULL,
        name TEXT NOT NULL,
        name_norm TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        props_json TEXT NOT NULL DEFAULT '{}',
        confidence REAL NOT NULL DEFAULT 0.7,
        mention_count INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(kb_id, id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_entities_kb_type ON entities(kb_id, type_name)",
    "CREATE INDEX IF NOT EXISTS idx_entities_kb_norm ON entities(kb_id, name_norm)",
    """CREATE TABLE IF NOT EXISTS entity_aliases (
        kb_id TEXT NOT NULL DEFAULT current_setting('app.kb_id', true),
        entity_id TEXT NOT NULL,
        alias TEXT NOT NULL,
        alias_norm TEXT NOT NULL,
        PRIMARY KEY(kb_id, alias_norm, entity_id),
        FOREIGN KEY(kb_id, entity_id) REFERENCES entities(kb_id, id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_aliases_kb_norm ON entity_aliases(kb_id, alias_norm)",
    """CREATE TABLE IF NOT EXISTS relations (
        kb_id TEXT NOT NULL DEFAULT current_setting('app.kb_id', true),
        id TEXT NOT NULL,
        type_name TEXT NOT NULL,
        src_id TEXT NOT NULL,
        dst_id TEXT NOT NULL,
        weight REAL NOT NULL DEFAULT 1.0,
        confidence REAL NOT NULL DEFAULT 0.7,
        evidence TEXT NOT NULL DEFAULT '',
        source_chunk_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(kb_id, id),
        FOREIGN KEY(kb_id, src_id) REFERENCES entities(kb_id, id) ON DELETE CASCADE,
        FOREIGN KEY(kb_id, dst_id) REFERENCES entities(kb_id, id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_rel_kb_src ON relations(kb_id, src_id)",
    "CREATE INDEX IF NOT EXISTS idx_rel_kb_dst ON relations(kb_id, dst_id)",
    "CREATE INDEX IF NOT EXISTS idx_rel_kb_type ON relations(kb_id, type_name)",
    """CREATE TABLE IF NOT EXISTS mentions (
        kb_id TEXT NOT NULL DEFAULT current_setting('app.kb_id', true),
        id TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        chunk_id TEXT NOT NULL,
        surface TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.7,
        created_at TEXT NOT NULL,
        PRIMARY KEY(kb_id, id),
        FOREIGN KEY(kb_id, entity_id) REFERENCES entities(kb_id, id) ON DELETE CASCADE,
        FOREIGN KEY(kb_id, chunk_id) REFERENCES chunks(kb_id, id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mentions_kb_chunk ON mentions(kb_id, chunk_id)",
    "CREATE INDEX IF NOT EXISTS idx_mentions_kb_entity ON mentions(kb_id, entity_id)",
    """CREATE TABLE IF NOT EXISTS memories (
        kb_id TEXT NOT NULL DEFAULT current_setting('app.kb_id', true),
        id TEXT NOT NULL,
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
        last_access_at TEXT,
        PRIMARY KEY(kb_id, id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memories_kb_subject ON memories(kb_id, subject_norm, predicate)",
    "CREATE INDEX IF NOT EXISTS idx_memories_kb_kind ON memories(kb_id, kind)",
    """CREATE TABLE IF NOT EXISTS memories_draft (
        kb_id TEXT NOT NULL DEFAULT current_setting('app.kb_id', true),
        id TEXT NOT NULL,
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
        applied_at TEXT,
        PRIMARY KEY(kb_id, id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_draft_kb_status ON memories_draft(kb_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_draft_kb_slot ON memories_draft(kb_id, slot_key)",
    """CREATE TABLE IF NOT EXISTS terms (
        kb_id TEXT NOT NULL DEFAULT current_setting('app.kb_id', true),
        id TEXT NOT NULL,
        name TEXT NOT NULL,
        name_norm TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        synonyms_json TEXT NOT NULL DEFAULT '[]',
        entity_id TEXT,
        kind TEXT NOT NULL DEFAULT 'concept',
        certified INTEGER NOT NULL DEFAULT 0,
        usage_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(kb_id, id)
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_terms_kb_norm ON terms(kb_id, name_norm)",
    "CREATE INDEX IF NOT EXISTS idx_terms_kb_certified ON terms(kb_id, certified)",
    """CREATE TABLE IF NOT EXISTS ingest_log (
        kb_id TEXT NOT NULL DEFAULT current_setting('app.kb_id', true),
        id TEXT NOT NULL,
        doc_id TEXT,
        status TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        stats_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        PRIMARY KEY(kb_id, id)
    )""",
]

for _table in (
    "meta", "documents", "chunks", "chunk_fts", "vectors", "entity_types",
    "relation_types", "entities", "entity_aliases", "relations", "mentions",
    "memories", "memories_draft", "terms", "ingest_log",
):
    PG_SCHEMA.extend([
        f"ALTER TABLE {_table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {_table} FORCE ROW LEVEL SECURITY",
        f"""DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = current_schema()
              AND tablename = '{_table}' AND policyname = 'kb_isolation'
          ) THEN
            CREATE POLICY kb_isolation ON {_table} USING
              (kb_id = current_setting('app.kb_id', true)) WITH CHECK
              (kb_id = current_setting('app.kb_id', true));
          END IF;
        END $$""",
    ])


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


class _PgCursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)

    @property
    def rowcount(self) -> int:
        return int(getattr(self._cursor, "rowcount", -1))


class PgConnection:
    """Small DB-API facade so the KB SQL can keep using qmark placeholders."""

    is_postgres = True

    def __init__(self, dsn: str, *, kb_id: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - optional infra
            raise RuntimeError(
                "知识库使用 PostgreSQL 需要安装 psycopg[binary]"
            ) from exc
        self._raw = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        self.kb_id = kb_id or "default"
        self._raw.execute(
            "SELECT set_config('app.kb_id', %s, false)", (self.kb_id,)
        )

    def execute(self, sql: str, params: Any = ()):
        return _PgCursor(self._raw.execute(sql.replace("?", "%s"), params))

    def executemany(self, sql: str, seq: Any):
        # psycopg3 exposes executemany on cursors, not on Connection. Keep the
        # facade simple and return the last statement's rowcount.
        last = None
        for params in seq:
            last = self._raw.execute(sql.replace("?", "%s"), params)
        return _PgCursor(last)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._raw.commit()
        else:
            self._raw.rollback()
        return False

    def close(self) -> None:
        self._raw.close()


def _kb_id_from_path(path: Path) -> str:
    name = path.name
    if name.startswith("kb_") and name.endswith(".sqlite3"):
        return name[3:-8]
    return "default"


def connect(
    path: str | Path,
    *,
    create: bool = True,
    kb_id: str | None = None,
    dsn: str | None = None,
) -> Any:
    if dsn is None:
        dsn = (
            os.getenv("WEBCHAT_KB_DB_URL")
            or os.getenv("WEBCHAT_DB_URL")
            or ""
        ).strip()
    dsn = (dsn or "").strip()
    if dsn.startswith(("postgres://", "postgresql://")):
        return PgConnection(dsn, kb_id=kb_id or os.getenv("WEBCHAT_KB_ID", "") or _kb_id_from_path(Path(path)))
    p = Path(path)
    if create:
        p.parent.mkdir(parents=True, exist_ok=True)
    elif not p.exists():
        raise FileNotFoundError(f"数据库不存在：{p}（先运行 `kb init`）")
    conn = sqlite3.connect(str(p), isolation_level=None)
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
    is_postgres = bool(getattr(conn, "is_postgres", False))
    # Multiple KB services bootstrap in parallel at startup. Serialize DDL and
    # policy creation so two processes cannot race on CREATE POLICY.
    if is_postgres:
        conn.execute("SELECT pg_advisory_lock(?)", (817263451,))
    try:
        with conn:  # 单事务建表
            for stmt in (PG_SCHEMA if is_postgres else SCHEMA):
                conn.execute(stmt)
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            if not is_postgres:
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
                "INSERT INTO meta(key, value) VALUES('created_at', ?) ON CONFLICT DO NOTHING", (iso(),)
            )
    finally:
        if is_postgres:
            conn.execute("SELECT pg_advisory_unlock(?)", (817263451,))


def _seed_core_types(conn: sqlite3.Connection) -> None:
    ts = iso()
    for name, parent, desc in CORE_ENTITY_TYPES:
        conn.execute(
            "INSERT INTO entity_types(name, parent, description, is_core, created_at)"
            " VALUES(?,?,?,1,?) ON CONFLICT DO NOTHING",
            (name, parent, desc, ts),
        )
    for name, domain, rng, desc, sym in CORE_RELATION_TYPES:
        conn.execute(
            "INSERT INTO relation_types"
            "(name, domain, range, description, \"symmetric\", is_core, created_at)"
            " VALUES(?,?,?,?,?,1,?) ON CONFLICT DO NOTHING",
            (name, domain, rng, desc, sym, ts),
        )


# ---------------------------------------------------------------- meta


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    if getattr(conn, "is_postgres", False):
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?)"
            " ON CONFLICT(kb_id, key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    else:
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
