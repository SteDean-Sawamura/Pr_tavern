"""SQLite database connection pool manager."""

import asyncio
import aiosqlite
from contextlib import asynccontextmanager
from config import DB_PATH

# Singleton connection pool (single-writer, multiple-reader for SQLite)
_pool: asyncio.Queue | None = None
_pool_size = 4
_pool_created = 0
_pool_lock: asyncio.Lock | None = None


def _ensure_pool():
    """Lazily initialize pool and lock inside a running event loop."""
    global _pool, _pool_lock
    if _pool is None:
        _pool = asyncio.Queue(maxsize=_pool_size)
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()


async def _create_connection() -> aiosqlite.Connection:
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


@asynccontextmanager
async def get_db():
    """Get a database connection from the pool.

    Usage:
        async with get_db() as db:
            cursor = await db.execute(...)
    """
    global _pool_created
    _ensure_pool()
    db = None

    # Try to get a connection from the pool without blocking
    try:
        db = _pool.get_nowait()
    except asyncio.QueueEmpty:
        # No pooled connections available — create a new one if under limit
        async with _pool_lock:
            if _pool_created < _pool_size:
                db = await _create_connection()
                _pool_created += 1
            else:
                # At limit — wait for a connection to be returned
                db = None

    if db is None:
        db = await _pool.get()

    # Verify connection is still alive
    try:
        await db.execute("SELECT 1")
    except Exception:
        # Connection is broken — discard and create a fresh one
        try:
            await db.close()
        except Exception:
            pass
        # Replace in one lock acquisition to avoid counter race
        try:
            db = await _create_connection()
        except Exception:
            # Failed to create replacement — decrement counter and re-raise
            async with _pool_lock:
                _pool_created -= 1
            raise

    try:
        yield db
    finally:
        # P0-6: 在归还连接前回滚未提交的事务，防止下一个消费者拿到"脏"连接。
        try:
            await db.rollback()
        except Exception:
            pass
        # Always return healthy connection to the pool
        try:
            _pool.put_nowait(db)
        except asyncio.QueueFull:
            await db.close()
            async with _pool_lock:
                _pool_created -= 1


async def init_db():
    """Create all tables if they don't exist.

    Uses a dedicated connection (not from pool) to avoid schema changes
    racing with pooled connections that may cache old schema.
    """
    db = await _create_connection()
    try:
        await db.executescript(CREATE_TABLES_SQL)
        # 迁移: 如果 saves 表缺少 authors_note 列则添加
        try:
            cursor = await db.execute("PRAGMA table_info(saves)")
            columns = [row[1] for row in await cursor.fetchall()]
            if "authors_note" not in columns:
                await db.execute("ALTER TABLE saves ADD COLUMN authors_note TEXT DEFAULT ''")
            if "authors_note_position" not in columns:
                await db.execute("ALTER TABLE saves ADD COLUMN authors_note_position TEXT DEFAULT 'end'")
            if "authors_note_depth" not in columns:
                await db.execute("ALTER TABLE saves ADD COLUMN authors_note_depth INTEGER DEFAULT 4")
            if "negative_prompt" not in columns:
                await db.execute("ALTER TABLE saves ADD COLUMN negative_prompt TEXT DEFAULT ''")
            if "logit_bias" not in columns:
                await db.execute("ALTER TABLE saves ADD COLUMN logit_bias TEXT DEFAULT '[]'")
            if "lorebook_max_recursion" not in columns:
                await db.execute("ALTER TABLE saves ADD COLUMN lorebook_max_recursion INTEGER DEFAULT NULL")
        except Exception:
            pass
        # 迁移: ai_profiles 加 stage_models 列
        try:
            cursor = await db.execute("PRAGMA table_info(ai_profiles)")
            columns = [row[1] for row in await cursor.fetchall()]
            if "stage_models" not in columns:
                await db.execute("ALTER TABLE ai_profiles ADD COLUMN stage_models TEXT DEFAULT NULL")
            for field, default in (("review_max_tokens", 500), ("review_retry_max_tokens", 1500)):
                if field not in columns:
                    await db.execute(f"ALTER TABLE ai_profiles ADD COLUMN {field} INTEGER NOT NULL DEFAULT {default}")
        except Exception:
            pass
        await db.commit()
    finally:
        await db.close()


async def close_pool():
    """Close all pooled connections. Call on shutdown."""
    global _pool_created
    if _pool is None:
        return
    async with _pool_lock:
        while not _pool.empty():
            try:
                db = _pool.get_nowait()
                await db.close()
            except asyncio.QueueEmpty:
                break
        _pool_created = 0


async def fetch_one(query: str, params: tuple = ()) -> dict | None:
    """Execute query and return first row as dict, or None."""
    async with get_db() as db:
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)


async def fetch_all(query: str, params: tuple = ()) -> list[dict]:
    """Execute query and return all rows as list of dicts."""
    async with get_db() as db:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS scripts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT DEFAULT '1.0',
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS saves (
    id TEXT PRIMARY KEY,
    script_id TEXT REFERENCES scripts(id),
    name TEXT,
    active_node_id TEXT,
    total_nodes INTEGER DEFAULT 0,
    play_time_seconds INTEGER DEFAULT 0,
    summary TEXT,
    authors_note TEXT DEFAULT '',
    authors_note_position TEXT DEFAULT 'end',
    authors_note_depth INTEGER DEFAULT 4,
    negative_prompt TEXT DEFAULT '',
    logit_bias TEXT DEFAULT '[]',
    lorebook_max_recursion INTEGER DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS save_scripts (
    save_id TEXT PRIMARY KEY REFERENCES saves(id) ON DELETE CASCADE,
    content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rpg_saves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    script_id TEXT NOT NULL,
    name TEXT NOT NULL,
    turn INTEGER DEFAULT 0,
    game_time TEXT,
    data TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tree_nodes (
    id TEXT PRIMARY KEY,
    save_id TEXT REFERENCES saves(id) ON DELETE CASCADE,
    parent_id TEXT,
    game_time TEXT,
    turn_number INTEGER,
    player_action TEXT,
    ai_response TEXT,
    choices_presented TEXT,
    dice_rolls TEXT,
    state_changes TEXT,
    triggered_events TEXT,
    state_snapshot TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS action_requests (
    save_id TEXT NOT NULL REFERENCES saves(id) ON DELETE CASCADE,
    action_id TEXT NOT NULL,
    PRIMARY KEY (save_id, action_id)
);

CREATE TABLE IF NOT EXISTS pending_action_plans (
    save_id TEXT PRIMARY KEY REFERENCES saves(id) ON DELETE CASCADE,
    action_id TEXT NOT NULL,
    request TEXT NOT NULL,
    base_node_id TEXT,
    plan_id TEXT NOT NULL,
    plan_signature TEXT NOT NULL,
    reply TEXT
);

CREATE TABLE IF NOT EXISTS pending_action_injections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    save_id TEXT NOT NULL REFERENCES saves(id) ON DELETE CASCADE,
    action_id TEXT NOT NULL,
    request TEXT NOT NULL,
    base_node_id TEXT,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_injections_action
    ON pending_action_injections(save_id, action_id, id);

CREATE INDEX IF NOT EXISTS idx_nodes_save ON tree_nodes(save_id);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON tree_nodes(parent_id);
-- P10: 高频查询索引
CREATE INDEX IF NOT EXISTS idx_saves_script ON saves(script_id);
CREATE INDEX IF NOT EXISTS idx_saves_updated ON saves(updated_at DESC);

CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    source_url TEXT,
    source_type TEXT,
    search_query TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_materials_created ON materials(created_at DESC);

CREATE TABLE IF NOT EXISTS material_tags (
    material_id INTEGER REFERENCES materials(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (material_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_tags ON material_tags(tag);

CREATE TABLE IF NOT EXISTS material_script_links (
    material_id INTEGER REFERENCES materials(id),
    script_id TEXT REFERENCES scripts(id),
    inject_target TEXT,
    inject_data TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (material_id, script_id, inject_target)
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    api_key TEXT DEFAULT '',
    model TEXT NOT NULL,
    max_tokens INTEGER DEFAULT 8192,
    base_url TEXT,
    is_active INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    stage_models TEXT DEFAULT NULL,
    review_max_tokens INTEGER NOT NULL DEFAULT 500,
    review_retry_max_tokens INTEGER NOT NULL DEFAULT 1500
);
"""
