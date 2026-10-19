#!/usr/bin/env python3
"""本地记忆 MCP 服务器 — SQLite + sqlite-vec 向量检索。

工具：memory_write / memory_search / memory_list / memory_get / memory_update /
      memory_delete / memory_stats

用法：
  作为 MCP stdio 服务运行（默认）
  python3 server.py reindex   强制用当前嵌入模型重建向量索引
"""

import json
import math
import os
import re
import sqlite3
import struct
import sys
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import sqlite_vec

try:
    from sqlite_vec import serialize_float32
except ImportError:  # 旧版 sqlite-vec 兜底
    import struct

    def serialize_float32(vec):
        return struct.pack(f"{len(vec)}f", *vec)

from embed import load_embedder

try:
    from mcp.server.mcpserver import MCPServer as ServerApp  # mcp 2.x
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as ServerApp

DB_PATH = Path(os.environ.get("MEMORY_DB", BASE / "memory.db"))
SEARCH_CANDIDATE_MULTIPLIER = max(4, int(os.environ.get("MEMORY_SEARCH_CANDIDATE_MULTIPLIER", "10")))
SEARCH_CANDIDATE_MAX = max(50, int(os.environ.get("MEMORY_SEARCH_CANDIDATE_MAX", "500")))
CHUNK_SIZE = max(256, int(os.environ.get("MEMORY_CHUNK_SIZE", "1200")))
CHUNK_OVERLAP = max(0, min(CHUNK_SIZE // 2, int(os.environ.get("MEMORY_CHUNK_OVERLAP", "200"))))
CHUNK_THRESHOLD = max(CHUNK_SIZE, int(os.environ.get("MEMORY_CHUNK_THRESHOLD", str(CHUNK_SIZE + 1))))

KIND_DEFAULT_IMPORTANCE = {
    "rule": 5,
    "preference": 5,
    "decision": 5,
    "lesson": 4,
    "status": 3,
    "fact": 3,
    "report": 2,
}
VALID_KINDS = set(KIND_DEFAULT_IMPORTANCE)

EMB = load_embedder()

_initialized = False


def _log(msg: str) -> None:
    print(f"[memory] {msg}", file=sys.stderr, flush=True)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sid() -> str:
    return uuid.uuid4().hex[:12]


def _tags_json(tags) -> str:
    if tags is None:
        return "[]"
    return json.dumps(sorted({str(t).strip() for t in tags if str(t).strip()}), ensure_ascii=False)


def _tagset(tags) -> set[str]:
    if tags is None:
        return set()
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            tags = [tags]
    return {str(t).strip() for t in tags if str(t).strip()}


def _infer_metadata(content: str, tags=None, kind: str | None = None,
                    importance: int | None = None, scope: str | None = None):
    tagset = _tagset(tags)
    if kind is None:
        if tagset & {"rules", "rule", "system", "硬性要求", "记忆规则", "交互规则"} or "硬性要求" in content[:200]:
            kind = "rule"
        elif tagset & {"preference", "用户偏好", "偏好"}:
            kind = "preference"
        elif tagset & {"decision", "项目决策"}:
            kind = "decision"
        elif (
            "report" in tagset
            or "扫描报告" in content[:600]
            or (len(content) >= 4000 and ("热榜" in content[:800] or "报告" in content[:800]))
        ):
            kind = "report"
        elif tagset & {"lesson", "踩坑", "排障", "根因"}:
            kind = "lesson"
        elif tagset & {"status", "progress", "pending", "todo"}:
            kind = "status"
        else:
            kind = "fact"
    kind = str(kind).strip().lower()
    if kind not in VALID_KINDS:
        kind = "fact"

    if scope is None or not str(scope).strip():
        projects = sorted(t for t in tagset if t.startswith("project:"))
        scope = projects[0] if projects else "global"
    scope = str(scope).strip() or "global"

    if importance is None:
        importance = KIND_DEFAULT_IMPORTANCE[kind]
    importance = max(1, min(int(importance), 5))
    return kind, importance, scope


def _decode_vec(blob) -> tuple[float, ...]:
    if blob is None:
        return ()
    return struct.unpack(f"{len(blob) // 4}f", blob)


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def _lexical_overlap(query: str, content: str) -> float:
    """Small exact-token boost to protect technical identifiers from vector drift."""
    ascii_q = set(re.findall(r"[a-z0-9][a-z0-9_.-]{2,}", query.lower()))
    ascii_c = set(re.findall(r"[a-z0-9][a-z0-9_.-]{2,}", content.lower()))
    cjk_q = set(re.findall(r"[\u4e00-\u9fff]{2,}", query))
    cjk_c = set(re.findall(r"[\u4e00-\u9fff]{2,}", content))
    q_tokens = ascii_q | cjk_q
    if not q_tokens:
        return 0.0
    return len(q_tokens & (ascii_c | cjk_c)) / len(q_tokens)


def _tag_overlap(query: str, tags) -> float:
    aliases = {
        "graphics": {"gpu", "显卡", "硬件"},
        "card": {"gpu", "显卡", "硬件"},
        "environment": {"env", "环境", "本机环境"},
        "computer": {"主机配置", "本机环境"},
        "browser": {"browser", "浏览器", "bsk"},
    }
    q_tokens = set(re.findall(r"[a-z0-9][a-z0-9_.-]{2,}", query.lower()))
    q_tokens |= set(re.findall(r"[\u4e00-\u9fff]{2,}", query))
    tagset = _tagset(tags)
    matched = set()
    for token in q_tokens:
        if token in tagset:
            matched.add(token)
        matched |= aliases.get(token, set()) & tagset
    return min(1.0, len(matched) / max(1, len(q_tokens)))


def _chunk_text(content: str) -> list[tuple[int, int, str]]:
    """Split long content into overlapping character windows.

    Character windows are deliberate here: the embedding model already performs
    tokenization, while this keeps chunk boundaries deterministic for Chinese,
    English, code, and mixed technical notes.
    """
    if len(content) <= CHUNK_THRESHOLD:
        return []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    chunks = []
    start = 0
    while start < len(content):
        end = min(len(content), start + CHUNK_SIZE)
        if end < len(content):
            boundary = max(content.rfind("\n\n", start + CHUNK_SIZE // 2, end),
                           content.rfind("\n", start + CHUNK_SIZE // 2, end))
            if boundary > start:
                end = boundary
        text = content[start:end].strip()
        if text:
            actual_start = start + (len(content[start:end]) - len(content[start:end].lstrip()))
            chunks.append((actual_start, end, text))
        if end >= len(content):
            break
        start = max(start + step, end - CHUNK_OVERLAP)
    return chunks


def _delete_chunks(conn: sqlite3.Connection, parent_id: int) -> None:
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM memory_chunks WHERE parent_id=?", (parent_id,)
    ).fetchall()]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM memory_chunks WHERE id IN ({placeholders})", ids)


def _index_chunks(conn: sqlite3.Connection, parent_id: int, content: str) -> int:
    _delete_chunks(conn, parent_id)
    chunks = _chunk_text(content)
    if not chunks:
        return 0
    vectors = EMB.embed([text for _, _, text in chunks])
    for index, ((start, end, text), vector) in enumerate(zip(chunks, vectors)):
        cur = conn.execute(
            "INSERT INTO memory_chunks(parent_id, chunk_index, start_offset, end_offset, content) "
            "VALUES (?,?,?,?,?)",
            (parent_id, index, start, end, text),
        )
        conn.execute(
            "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
            (cur.lastrowid, serialize_float32(vector)),
        )
    return len(chunks)


def _row_text(row, prefix: str = "") -> str:
    tags = json.loads(row["tags"])
    tag_part = " " + " ".join(f"#{t}" for t in tags) if tags else ""
    stamp = row["updated_at"][:19].replace("T", " ")
    chunk = f" chunk={row['chunk_index']}" if "chunk_index" in row.keys() else ""
    return f"[{row['sid']}{chunk}] {stamp}{tag_part}\n{prefix}{row['content']}"


def _resolve(conn: sqlite3.Connection, sid_prefix: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM memories WHERE sid LIKE ? ORDER BY id LIMIT 1",
        (sid_prefix.strip() + "%",),
    ).fetchone()


def init_db() -> None:
    """建表；嵌入提供者/维度变化时自动重建向量索引。"""
    with closing(_db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY,
                sid TEXT UNIQUE NOT NULL,
                content TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                kind TEXT NOT NULL DEFAULT 'fact',
                importance INTEGER NOT NULL DEFAULT 3,
                scope TEXT NOT NULL DEFAULT 'global',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at DESC);
            CREATE TABLE IF NOT EXISTS memory_chunks (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                start_offset INTEGER NOT NULL,
                end_offset INTEGER NOT NULL,
                content TEXT NOT NULL,
                UNIQUE(parent_id, chunk_index)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_chunks_parent ON memory_chunks(parent_id, chunk_index);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
        legacy_metadata = not {"kind", "importance", "scope"}.issubset(columns)
        migrations = {
            "kind": "ALTER TABLE memories ADD COLUMN kind TEXT NOT NULL DEFAULT 'fact'",
            "importance": "ALTER TABLE memories ADD COLUMN importance INTEGER NOT NULL DEFAULT 3",
            "scope": "ALTER TABLE memories ADD COLUMN scope TEXT NOT NULL DEFAULT 'global'",
        }
        for name, sql in migrations.items():
            if name not in columns:
                conn.execute(sql)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_kind_scope "
            "ON memories(kind, scope, updated_at DESC)"
        )
        rows = conn.execute("SELECT id, content, tags, kind, importance, scope FROM memories").fetchall()
        # Newly added columns contain legacy defaults, so infer metadata from the
        # actual content/tags rather than treating the defaults as explicit values.
        for old in rows:
            kind, importance, scope = _infer_metadata(
                old["content"], old["tags"],
                None if legacy_metadata else old["kind"],
                None if legacy_metadata else old["importance"],
                None if legacy_metadata else old["scope"],
            )
            if (old["kind"], old["importance"], old["scope"]) != (kind, importance, scope):
                conn.execute(
                    "UPDATE memories SET kind=?, importance=?, scope=? WHERE id=?",
                    (kind, importance, scope, old["id"]),
                )

        row = conn.execute("SELECT value FROM meta WHERE key='embed_dim'").fetchone()
        if row is None:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(embedding float[{EMB.dim}])"
            )
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(embedding float[{EMB.dim}])"
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('embed_dim', ?), ('embed_provider', ?)",
                (str(EMB.dim), EMB.name),
            )
        else:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(embedding float[{EMB.dim}])"
            )
        if row is not None and int(row["value"]) != EMB.dim:
            _log(f"嵌入模型变化（{row['value']} 维 -> {EMB.dim} 维），重建向量索引")
            conn.execute("DROP TABLE IF EXISTS memories_vec")
            conn.execute("DROP TABLE IF EXISTS chunks_vec")
            conn.execute(
                f"CREATE VIRTUAL TABLE memories_vec USING vec0(embedding float[{EMB.dim}])"
            )
            conn.execute(
                f"CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{EMB.dim}])"
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('embed_dim', ?), ('embed_provider', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(EMB.dim), EMB.name),
            )
            _reindex(conn)
        for parent in conn.execute("SELECT id, content FROM memories").fetchall():
            if not conn.execute("SELECT 1 FROM memory_chunks WHERE parent_id=? LIMIT 1", (parent["id"],)).fetchone():
                _index_chunks(conn, parent["id"], parent["content"])
        conn.commit()


def _ensure() -> None:
    global _initialized
    if not _initialized:
        init_db()
        _initialized = True


def _reindex(conn: sqlite3.Connection) -> int:
    rows = conn.execute("SELECT id, content FROM memories ORDER BY id").fetchall()
    conn.execute("DELETE FROM memories_vec")
    conn.execute("DELETE FROM chunks_vec")
    conn.execute("DELETE FROM memory_chunks")
    for row in rows:
        vec = EMB.embed([row["content"]])[0]
        conn.execute(
            "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
            (row["id"], serialize_float32(vec)),
        )
        _index_chunks(conn, row["id"], row["content"])
    conn.commit()
    return len(rows)


# ---------- 核心操作（供 MCP 工具与测试直接调用） ----------

def db_write(content: str, tags=None, kind: str | None = None,
             importance: int | None = None, scope: str | None = None) -> str:
    _ensure()
    content = content.strip()
    if not content:
        raise ValueError("content 不能为空")
    tags_json = _tags_json(tags)
    kind, importance, scope = _infer_metadata(content, tags_json, kind, importance, scope)
    sid = _sid()
    vec = EMB.embed([content])[0]
    now = _now()
    with closing(_db()) as conn:
        cur = conn.execute(
            "INSERT INTO memories(sid, content, tags, kind, importance, scope, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sid, content, tags_json, kind, importance, scope, now, now),
        )
        conn.execute(
            "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
            (cur.lastrowid, serialize_float32(vec)),
        )
        chunk_count = _index_chunks(conn, cur.lastrowid, content)
        conn.commit()
    suffix = ' ' + ' '.join('#' + t for t in json.loads(tags_json)) if tags_json != '[]' else ''
    chunk_note = f"（已切分 {chunk_count} 个重叠窗口）" if chunk_count else ""
    return f"已写入记忆 [{sid}]{suffix} [{kind}/{scope}] {chunk_note}\n{content}"


def _rerank_hits(rows, query_vec, k: int, query: str):
    """MMR-style reranking: relevance + importance, while avoiding near duplicates."""
    remaining = list(rows)
    selected = []
    selected_vecs = []
    selected_kinds = {}
    selected_scopes = {}
    selected_parents = {}
    while remaining and len(selected) < k:
        best = None
        best_score = -float("inf")
        for row in remaining:
            row_vec = _decode_vec(row["embedding"])
            relevance = max(-1.0, min(1.0, _dot(query_vec, row_vec)))
            lexical_bonus = 0.22 * _lexical_overlap(query, row["content"])
            tag_bonus = 0.18 * _tag_overlap(query, row["tags"])
            # Priority helps only when the memory is already semantically relevant;
            # a global rule must not displace an unrelated project fact.
            priority_bonus = 0.012 * (int(row["importance"]) - 3) if relevance >= 0.45 else 0.0
            type_bonus = 0.008 if relevance >= 0.45 and row["kind"] in {"rule", "preference", "decision"} else 0.0
            redundancy = max((_dot(row_vec, old) for old in selected_vecs), default=0.0)
            # Similar reports should not consume the complete result set.
            diversity_penalty = 0.32 * max(0.0, redundancy - 0.72)
            kind_count = selected_kinds.get(row["kind"], 0)
            scope_count = selected_scopes.get(row["scope"], 0)
            parent_id = row["parent_id"]
            crowding_penalty = 0.10 * max(0, kind_count - 0)
            if row["kind"] == "report":
                crowding_penalty += 0.08 * max(0, scope_count - 0)
            crowding_penalty += 0.18 * selected_parents.get(parent_id, 0)
            score = 0.76 * relevance + lexical_bonus + tag_bonus + priority_bonus + type_bonus - diversity_penalty - crowding_penalty
            if best is None or score > best_score:
                best, best_score = row, score
        remaining.remove(best)
        selected.append((best, best_score))
        selected_vecs.append(_decode_vec(best["embedding"]))
        selected_kinds[best["kind"]] = selected_kinds.get(best["kind"], 0) + 1
        selected_scopes[best["scope"]] = selected_scopes.get(best["scope"], 0) + 1
        selected_parents[best["parent_id"]] = selected_parents.get(best["parent_id"], 0) + 1
    return selected


def db_search(query: str, k: int = 8, tag: str | None = None) -> str:
    _ensure()
    query = query.strip()
    if not query:
        raise ValueError("query 不能为空")
    k = max(1, min(int(k), 50))
    query_vec = EMB.embed_query(query)
    vec = serialize_float32(query_vec)
    candidates = min(max(k * SEARCH_CANDIDATE_MULTIPLIER, 50), SEARCH_CANDIDATE_MAX)
    with closing(_db()) as conn:
        parent_rows = conn.execute(
            """
            SELECT m.*, m.id AS parent_id, NULL AS chunk_index,
                   v.distance, v.embedding
            FROM memories_vec v
            JOIN memories m ON m.id = v.rowid
            WHERE v.embedding MATCH ? AND k = ?
              AND NOT EXISTS (
                  SELECT 1 FROM memory_chunks mc WHERE mc.parent_id = m.id
              )
            ORDER BY v.distance
            """,
            (vec, candidates),
        ).fetchall()
        chunk_rows = conn.execute(
            """
            SELECT m.sid, m.tags, m.kind, m.importance, m.scope,
                   m.created_at, m.updated_at, m.id AS parent_id,
                   c.chunk_index, c.content, v.distance, v.embedding
            FROM chunks_vec v
            JOIN memory_chunks c ON c.id = v.rowid
            JOIN memories m ON m.id = c.parent_id
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
            """,
            (vec, candidates),
        ).fetchall()
    rows = list(parent_rows) + list(chunk_rows)
    if tag:
        tag = tag.lstrip("#")
        rows = [r for r in rows if tag in _tagset(r["tags"])]
    ranked = _rerank_hits(rows, query_vec, k, query)
    total = conn_total()
    if not ranked:
        return f"（共 {total} 条记忆）没有找到与「{query}」相关的记忆"
    lines = [f"（共 {total} 条记忆，相关性+多样性排序，候选 {len(rows)} 条）"]
    for r, _score in ranked:
        lines.append(
            f"{_row_text(r)}  dist={r['distance']:.3f} "
            f"kind={r['kind']} importance={r['importance']}"
        )
    return "\n\n".join(lines)


def conn_total() -> int:
    with closing(_db()) as conn:
        return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]


def db_list(limit: int = 20, tag: str | None = None, q: str | None = None) -> str:
    _ensure()
    limit = max(1, min(int(limit), 200))
    sql, args = "SELECT * FROM memories", []
    if q:
        sql += " WHERE content LIKE ?"
        args.append(f"%{q}%")
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(min(max(limit * SEARCH_CANDIDATE_MULTIPLIER, 50), SEARCH_CANDIDATE_MAX) if tag else limit)
    with closing(_db()) as conn:
        rows = conn.execute(sql, args).fetchall()
    if tag:
        tag = tag.lstrip("#")
        rows = [r for r in rows if tag in json.loads(r["tags"])]
    rows = rows[:limit]
    if not rows:
        return "没有记忆。"
    lines = [f"共 {conn_total()} 条记忆，最近 {len(rows)} 条："]
    lines += [_row_text(r) for r in rows]
    return "\n\n".join(lines)


def db_get(sid_prefix: str) -> str:
    _ensure()
    with closing(_db()) as conn:
        row = _resolve(conn, sid_prefix)
    if not row:
        return f"未找到 id 前缀为 {sid_prefix} 的记忆"
    return _row_text(row)


def db_update(sid_prefix: str, content: str | None = None, tags=None,
              kind: str | None = None, importance: int | None = None,
              scope: str | None = None) -> str:
    _ensure()
    if all(value is None for value in (content, tags, kind, importance, scope)):
        raise ValueError("content、tags、kind、importance、scope 至少提供一个")
    with closing(_db()) as conn:
        row = _resolve(conn, sid_prefix)
        if not row:
            return f"未找到 id 前缀为 {sid_prefix} 的记忆"
        new_content = content.strip() if content is not None else row["content"]
        if not new_content:
            raise ValueError("content 不能为空")
        new_tags = _tags_json(tags) if tags is not None else row["tags"]
        new_kind, new_importance, new_scope = _infer_metadata(
            new_content,
            new_tags,
            kind if kind is not None else row["kind"],
            importance if importance is not None else row["importance"],
            scope if scope is not None else row["scope"],
        )
        conn.execute(
            "UPDATE memories SET content=?, tags=?, kind=?, importance=?, scope=?, updated_at=? WHERE id=?",
            (new_content, new_tags, new_kind, new_importance, new_scope, _now(), row["id"]),
        )
        if content is not None:
            vec = EMB.embed([new_content])[0]
            conn.execute("DELETE FROM memories_vec WHERE rowid=?", (row["id"],))
            conn.execute(
                "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                (row["id"], serialize_float32(vec)),
            )
        chunk_count = _index_chunks(conn, row["id"], new_content)
        conn.commit()
        updated = conn.execute("SELECT * FROM memories WHERE id=?", (row["id"],)).fetchone()
    note = f"（已切分 {chunk_count} 个重叠窗口）" if chunk_count else ""
    return f"已更新 [{updated['sid']}] {note}\n" + _row_text(updated)


def db_delete(sid_prefix: str) -> str:
    _ensure()
    with closing(_db()) as conn:
        row = _resolve(conn, sid_prefix)
        if not row:
            return f"未找到 id 前缀为 {sid_prefix} 的记忆"
        _delete_chunks(conn, row["id"])
        conn.execute("DELETE FROM memories_vec WHERE rowid=?", (row["id"],))
        conn.execute("DELETE FROM memories WHERE id=?", (row["id"],))
        conn.commit()
    return f"已删除 [{row['sid']}]：{row['content'][:60]}"


def db_stats() -> str:
    _ensure()
    with closing(_db()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS oldest, MAX(created_at) AS newest FROM memories"
        ).fetchone()
    size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    with closing(_db()) as conn:
        chunks = conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
    return (
        f"记忆条数：{row['n']}\n"
        f"滑动窗口块：{chunks}\n"
        f"最早写入：{row['oldest'] or '-'}\n"
        f"最近写入：{row['newest'] or '-'}\n"
        f"数据库：{DB_PATH}（{size / 1024:.0f} KB）\n"
        f"嵌入模型：{EMB.name}（{EMB.dim} 维）"
    )


# ---------- MCP 工具 ----------

mcp = ServerApp("memory")


@mcp.tool()
def memory_write(content: str, tags: list[str] | None = None,
                 kind: str | None = None, importance: int | None = None,
                 scope: str | None = None) -> str:
    """存入长期记忆。kind 可选 rule/preference/decision/lesson/status/fact/report；report 按 scope 保留最近 MEMORY_REPORT_WINDOW 条。"""
    return db_write(content, tags, kind, importance, scope)


@mcp.tool()
def memory_search(query: str, k: int = 8, tag: str | None = None) -> str:
    """语义检索记忆：用自然语言描述要找的内容，返回最相关的 k 条（支持中英文）。可用 tag 过滤标签。"""
    return db_search(query, k, tag)


@mcp.tool()
def memory_list(limit: int = 20, tag: str | None = None, q: str | None = None) -> str:
    """列出记忆，按时间倒序。可按 tag 精确过滤、按 q 子串过滤 content。"""
    return db_list(limit, tag, q)


@mcp.tool()
def memory_get(id: str) -> str:
    """按短 id（或唯一前缀）获取一条记忆的完整内容。"""
    return db_get(id)


@mcp.tool()
def memory_update(id: str, content: str | None = None, tags: list[str] | None = None,
                  kind: str | None = None, importance: int | None = None,
                  scope: str | None = None) -> str:
    """更新记忆内容、标签或元数据；content 变化会自动重新计算向量。"""
    return db_update(id, content, tags, kind, importance, scope)


@mcp.tool()
def memory_delete(id: str) -> str:
    """按短 id（或唯一前缀）删除一条记忆。"""
    return db_delete(id)


@mcp.tool()
def memory_stats() -> str:
    """查看记忆库状态：条数、时间范围、数据库大小、嵌入模型。"""
    return db_stats()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "reindex":
        _ensure()
        with closing(_db()) as conn:
            n = _reindex(conn)
        _log(f"已用 {EMB.name}（{EMB.dim} 维）重建 {n} 条记忆的向量索引")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        _ensure()
        host = os.environ.get("MEMORY_HTTP_HOST", "127.0.0.1")
        port = int(os.environ.get("MEMORY_HTTP_PORT", "8766"))
        _log(f"memory MCP 就绪（streamable-http）：http://{host}:{port}/mcp db={DB_PATH} embedder={EMB.name}({EMB.dim}d)")
        mcp.run(transport="streamable-http", host=host, port=port)
        return
    _ensure()
    _log(f"memory MCP 就绪（stdio）：db={DB_PATH} embedder={EMB.name}({EMB.dim}d)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
