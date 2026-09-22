"""SQLite storage for channels, videos and run history."""

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    channel_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'primary',
    genre TEXT NOT NULL DEFAULT 'general',
    active INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(channel_id) ON DELETE CASCADE,
    video_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT,
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL DEFAULT 'new',
    audio_path TEXT,
    transcript_path TEXT,
    duration REAL,
    language TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    new_videos INTEGER NOT NULL DEFAULT 0,
    downloaded INTEGER NOT NULL DEFAULT 0,
    transcribed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS productions (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    genre TEXT NOT NULL DEFAULT 'general',
    source_video_id TEXT,
    stage TEXT NOT NULL DEFAULT 'script',
    status TEXT NOT NULL DEFAULT 'active',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS production_steps (
    id INTEGER PRIMARY KEY,
    production_id INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'auto',
    status TEXT NOT NULL DEFAULT 'done',
    artifact TEXT,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The channels the USER publishes on (distinct from `channels`, which are the
-- competitor/source channels being monitored for ideas). Mirrored into
-- Renderly lazily: renderly_channel_id is a cache, the name is the identity.
CREATE TABLE IF NOT EXISTS own_channels (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    genre TEXT NOT NULL DEFAULT 'general',
    youtube_handle TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    -- production defaults; NULL/'' means "inherit the global setting"
    default_voice TEXT,
    default_engine TEXT,
    default_render_mode TEXT,
    default_upscale INTEGER,
    bible_dir TEXT,
    refs_dir TEXT,
    -- Google Flow project URL used by the FlowImagesGen engine
    flow_project_url TEXT,
    -- auto-run criteria (NULL = inherit the global setting)
    autorun_enabled INTEGER,
    per_day INTEGER,
    topic_pick TEXT,
    -- Renderly mirror (soft reference: never a FK, always re-resolved)
    renderly_channel_id INTEGER,
    renderly_channel_name TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Global key-value settings (Auto Run criteria and friends). Values are
-- stored as TEXT; settings.py owns their types/defaults.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns of `videos` that set_video() is allowed to update
_VIDEO_FIELDS = {
    "status",
    "audio_path",
    "transcript_path",
    "duration",
    "language",
    "error",
    "auto",
}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(channels)")}
    if "genre" not in cols:
        conn.execute(
            "ALTER TABLE channels ADD COLUMN genre TEXT NOT NULL DEFAULT 'general'"
        )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    if "auto" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN auto INTEGER NOT NULL DEFAULT 1")
        # videos discovered but never downloaded are backlog, not auto-queue
        conn.execute("UPDATE videos SET auto = 0 WHERE status = 'new'")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(productions)")}
    if "llm_provider" not in cols:
        conn.execute("ALTER TABLE productions ADD COLUMN llm_provider TEXT")
    if "extra_prompt" not in cols:
        conn.execute("ALTER TABLE productions ADD COLUMN extra_prompt TEXT")
    if "work_dir" not in cols:
        conn.execute("ALTER TABLE productions ADD COLUMN work_dir TEXT")
    if "stage_extras" not in cols:
        conn.execute("ALTER TABLE productions ADD COLUMN stage_extras TEXT")
        # seed per-stage directions from the old shared field (LLM stages only)
        for pid, old in conn.execute(
                "SELECT id, extra_prompt FROM productions"
                " WHERE extra_prompt IS NOT NULL AND TRIM(extra_prompt) != ''"):
            conn.execute(
                "UPDATE productions SET stage_extras = ? WHERE id = ?",
                (json.dumps({"style": old, "script": old, "shots": old}), pid))
    if "render_mode" not in cols:
        conn.execute("ALTER TABLE productions ADD COLUMN render_mode TEXT")
    if "voice" not in cols:
        conn.execute("ALTER TABLE productions ADD COLUMN voice TEXT")
    if "own_channel_id" not in cols:
        conn.execute("ALTER TABLE productions ADD COLUMN own_channel_id INTEGER")
    _migrate_own_channels(conn)
    _add_column_if_missing(conn, "own_channels", "flow_project_url", "TEXT")


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str,
                           decl: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate_own_channels(conn: sqlite3.Connection) -> None:
    """own_channels shipped with NOT NULL defaults, which made "inherit the
    global setting" impossible. Rebuild it with nullable columns, preserving
    any rows (the table is new, so it is normally empty)."""
    info = {row[1]: row for row in conn.execute("PRAGMA table_info(own_channels)")}
    if not info or info["default_engine"][3] == 0:  # notnull flag clear
        return
    conn.executescript("""
        ALTER TABLE own_channels RENAME TO own_channels_old;
        CREATE TABLE own_channels (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            genre TEXT NOT NULL DEFAULT 'general',
            youtube_handle TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            default_voice TEXT,
            default_engine TEXT,
            default_render_mode TEXT,
            default_upscale INTEGER,
            bible_dir TEXT,
            refs_dir TEXT,
            autorun_enabled INTEGER,
            per_day INTEGER,
            topic_pick TEXT,
            renderly_channel_id INTEGER,
            renderly_channel_name TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO own_channels (id, name, description, genre, youtube_handle,
            active, default_voice, default_engine, default_render_mode,
            default_upscale, bible_dir, refs_dir, autorun_enabled, per_day,
            topic_pick, renderly_channel_id, renderly_channel_name, created_at)
        SELECT id, name, description, genre, youtube_handle, active,
            default_voice, default_engine, default_render_mode,
            default_upscale, bible_dir, refs_dir, autorun_enabled, per_day,
            topic_pick, renderly_channel_id, renderly_channel_name, created_at
        FROM own_channels_old;
        DROP TABLE own_channels_old;
    """)


def add_channel(
    conn, name: str, channel_id: str, kind: str = "primary", genre: str = "general"
) -> None:
    conn.execute(
        "INSERT INTO channels (name, channel_id, kind, genre) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(channel_id) DO UPDATE SET"
        " name = excluded.name, kind = excluded.kind, genre = excluded.genre",
        (name, channel_id, kind, genre),
    )
    conn.commit()


def sync_channels(conn, channels: list[dict]) -> int:
    """Upsert channels declared in config.yaml. Keeps existing videos intact."""
    changed = 0
    for ch in channels:
        if not ch.get("id"):
            continue
        cur = conn.execute(
            "INSERT INTO channels (name, channel_id, kind, genre, active)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(channel_id) DO UPDATE SET"
            " name = excluded.name, kind = excluded.kind, genre = excluded.genre,"
            " active = excluded.active",
            (ch["name"], ch["id"], ch.get("kind", "primary"),
             ch.get("genre") or "general", 1 if ch.get("active", True) else 0),
        )
        changed += cur.rowcount
    conn.commit()
    return changed


def list_channels(conn, active_only: bool = False):
    sql = "SELECT * FROM channels"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY id"
    return conn.execute(sql).fetchall()


def remove_channel(conn, key: str) -> bool:
    cur = conn.execute(
        "DELETE FROM channels WHERE channel_id = ? OR lower(name) = lower(?)",
        (key, key),
    )
    conn.commit()
    return cur.rowcount > 0


_CHANNEL_FIELDS = {"name", "kind", "genre", "active"}


def update_channel(conn, channel_id: str, **fields) -> None:
    cols, vals = [], []
    for key, value in fields.items():
        if key not in _CHANNEL_FIELDS:
            raise ValueError(f"Unknown channel field: {key}")
        cols.append(f"{key} = ?")
        vals.append(value)
    vals.append(channel_id)
    conn.execute(f"UPDATE channels SET {', '.join(cols)} WHERE channel_id = ?", vals)
    conn.commit()


def get_channel(conn, key: str):
    return conn.execute(
        "SELECT * FROM channels WHERE channel_id = ? OR lower(name) = lower(?)",
        (key, key),
    ).fetchone()


def upsert_video(conn, channel_id: str, video: dict, auto: int = 1) -> bool:
    """Insert a video if new. Returns True when a row was created.

    auto=0 marks backlog (pre-existing uploads found at first sync) which are
    only downloaded when explicitly queued.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO videos (channel_id, video_id, title, url,"
        " published_at, auto) VALUES (?, ?, ?, ?, ?, ?)",
        (
            channel_id,
            video["video_id"],
            video.get("title", ""),
            video.get("url", ""),
            video.get("published_at"),
            auto,
        ),
    )
    conn.commit()
    return cur.rowcount == 1


def set_video(conn, video_id: str, **fields) -> None:
    cols, vals = [], []
    for key, value in fields.items():
        if key not in _VIDEO_FIELDS:
            raise ValueError(f"Unknown video field: {key}")
        cols.append(f"{key} = ?")
        vals.append(value)
    vals.append(video_id)
    conn.execute(f"UPDATE videos SET {', '.join(cols)} WHERE video_id = ?", vals)
    conn.commit()


def delete_video(conn, video_id: str) -> None:
    conn.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
    conn.commit()


def get_video(conn, video_id: str):
    return conn.execute(
        "SELECT v.*, c.name AS channel_name, c.genre AS channel_genre FROM videos v"
        " JOIN channels c ON c.channel_id = v.channel_id WHERE v.video_id = ?",
        (video_id,),
    ).fetchone()


def _video_filters(status: str | None, genre: str | None, backlog: bool,
                   channel: str | None):
    clauses, params = [], []
    if backlog:
        clauses.append("v.auto = 0")
        clauses.append("v.status = 'new'")
    if status:
        clauses.append("v.status = ?")
        params.append(status)
    if genre:
        clauses.append("c.genre = ?")
        params.append(genre)
    if channel:
        clauses.append("(c.channel_id = ? OR lower(c.name) = lower(?))")
        params.extend([channel, channel])
    return clauses, params


def count_videos(conn, status: str | None = None, genre: str | None = None,
                 backlog: bool = False, channel: str | None = None) -> int:
    sql = ("SELECT COUNT(*) FROM videos v"
           " JOIN channels c ON c.channel_id = v.channel_id")
    clauses, params = _video_filters(status, genre, backlog, channel)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return conn.execute(sql, params).fetchone()[0]


def get_videos(conn, status: str | None = None, genre: str | None = None,
               backlog: bool = False, channel: str | None = None,
               limit: int | None = None, offset: int = 0):
    sql = ("SELECT v.*, c.name AS channel_name, c.genre AS channel_genre FROM videos v"
           " JOIN channels c ON c.channel_id = v.channel_id")
    clauses, params = _video_filters(status, genre, backlog, channel)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY v.published_at DESC"
    if limit:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    return conn.execute(sql, params).fetchall()


def get_pending_downloads(conn):
    return conn.execute(
        "SELECT v.*, c.name AS channel_name, c.genre AS channel_genre FROM videos v"
        " JOIN channels c ON c.channel_id = v.channel_id"
        " WHERE v.status = 'new' AND v.auto = 1 AND c.active = 1"
        " ORDER BY v.published_at"
    ).fetchall()


def get_pending_transcripts(conn):
    return conn.execute(
        "SELECT v.*, c.name AS channel_name, c.genre AS channel_genre FROM videos v"
        " JOIN channels c ON c.channel_id = v.channel_id"
        " WHERE v.status = 'downloaded' AND c.active = 1 ORDER BY v.published_at"
    ).fetchall()


def reset_errors(conn) -> int:
    """Move errored videos back to their retry state. Returns count."""
    total = 0
    cur = conn.execute(
        "UPDATE videos SET status = 'downloaded', error = NULL"
        " WHERE status = 'error' AND audio_path IS NOT NULL"
    )
    total += cur.rowcount
    cur = conn.execute(
        "UPDATE videos SET status = 'new', error = NULL WHERE status = 'error'"
    )
    total += cur.rowcount
    conn.commit()
    return total


def start_run(conn) -> int:
    cur = conn.execute("INSERT INTO runs DEFAULT VALUES")
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------- studio ---

STAGES = ["style", "script", "audio", "srt", "shots", "images", "merge", "review"]

_PROD_FIELDS = {"title", "genre", "stage", "status", "notes",
                "source_video_id", "llm_provider", "extra_prompt", "work_dir",
                "stage_extras", "render_mode", "voice", "own_channel_id"}


def stage_extra(prod, stage: str) -> str:
    """Per-stage additional direction for the LLM (JSON map in stage_extras)."""
    try:
        data = json.loads(prod["stage_extras"] or "{}")
    except (ValueError, TypeError):
        data = {}
    value = data.get(stage) if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


def create_production(conn, title: str, genre: str = "general",
                      source_video_id: str | None = None,
                      work_dir: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO productions (title, genre, source_video_id, work_dir, stage)"
        " VALUES (?, ?, ?, ?, 'style')",
        (title, genre or "general", source_video_id, work_dir),
    )
    conn.commit()
    return cur.lastrowid


def get_production(conn, pid: int):
    return conn.execute("SELECT * FROM productions WHERE id = ?", (pid,)).fetchone()


def list_productions(conn):
    return conn.execute(
        "SELECT * FROM productions ORDER BY updated_at DESC, id DESC"
    ).fetchall()


def update_production(conn, pid: int, **fields) -> None:
    cols, vals = [], []
    for key, value in fields.items():
        if key not in _PROD_FIELDS:
            raise ValueError(f"Unknown production field: {key}")
        cols.append(f"{key} = ?")
        vals.append(value)
    cols.append("updated_at = datetime('now')")
    vals.append(pid)
    conn.execute(f"UPDATE productions SET {', '.join(cols)} WHERE id = ?", vals)
    conn.commit()


def delete_production(conn, pid: int) -> None:
    conn.execute("DELETE FROM productions WHERE id = ?", (pid,))
    conn.commit()


def add_step(conn, pid: int, stage: str, method: str = "auto",
             artifact: str | None = None, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO production_steps (production_id, stage, method, status,"
        " artifact, detail) VALUES (?, ?, ?, 'done', ?, ?)",
        (pid, stage, method, artifact, detail),
    )
    conn.commit()


def latest_steps(conn, pid: int) -> dict:
    """Map of stage -> most recent done step for that stage."""
    rows = conn.execute(
        "SELECT s.* FROM production_steps s"
        " JOIN (SELECT stage, MAX(id) AS mid FROM production_steps"
        "       WHERE production_id = ? AND status = 'done' GROUP BY stage) m"
        " ON s.id = m.mid",
        (pid,),
    ).fetchall()
    return {row["stage"]: row for row in rows}


def step_history(conn, pid: int):
    return conn.execute(
        "SELECT * FROM production_steps WHERE production_id = ? ORDER BY id DESC",
        (pid,),
    ).fetchall()


def delete_steps(conn, pid: int, stages: list[str] | None = None) -> int:
    """Remove a production's step history - all of it, or per stage.
    Used by the studio start-over reset. An explicit empty list deletes
    nothing; None deletes everything."""
    if stages is not None:
        marks = ",".join("?" for _ in stages)
        cur = conn.execute(
            f"DELETE FROM production_steps"
            f" WHERE production_id = ? AND stage IN ({marks})",
            (pid, *stages),
        )
    else:
        cur = conn.execute(
            "DELETE FROM production_steps WHERE production_id = ?", (pid,)
        )
    conn.commit()
    return cur.rowcount


def stage_done(conn, pid: int, stage: str) -> bool:
    return stage in latest_steps(conn, pid)


def finish_run(conn, run_id: int, **counts) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = datetime('now'), new_videos = ?,"
        " downloaded = ?, transcribed = ?, failed = ? WHERE id = ?",
        (
            counts.get("new_videos", 0),
            counts.get("downloaded", 0),
            counts.get("transcribed", 0),
            counts.get("failed", 0),
            run_id,
        ),
    )
    conn.commit()


# ------------------------------------------------------- own channels ---
# The channels the user publishes on. Kept separate from `channels` (the
# monitored competitor/source channels) so the two never mix.

_OWN_CHANNEL_FIELDS = {
    "name", "description", "genre", "youtube_handle", "active",
    "default_voice", "default_engine", "default_render_mode",
    "default_upscale", "bible_dir", "refs_dir", "flow_project_url",
    "autorun_enabled", "per_day", "topic_pick",
    "renderly_channel_id", "renderly_channel_name",
}


def list_own_channels(conn, active_only: bool = False):
    sql = "SELECT * FROM own_channels"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY name COLLATE NOCASE"
    return conn.execute(sql).fetchall()


def get_own_channel(conn, key):
    """Look up an own channel by numeric id or by name (case-insensitive)."""
    if key is None or key == "":
        return None
    try:
        return conn.execute("SELECT * FROM own_channels WHERE id = ?",
                            (int(key),)).fetchone()
    except (TypeError, ValueError):
        return conn.execute(
            "SELECT * FROM own_channels WHERE name = ? COLLATE NOCASE",
            (str(key),)).fetchone()


def create_own_channel(conn, name: str, **fields) -> int:
    values = {k: v for k, v in fields.items() if k in _OWN_CHANNEL_FIELDS}
    cols = ["name", *values]
    marks = ",".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT INTO own_channels ({', '.join(cols)}) VALUES ({marks})",
        [name, *values.values()],
    )
    conn.commit()
    return cur.lastrowid


def update_own_channel(conn, oc_id: int, **fields) -> None:
    cols, vals = [], []
    for key, value in fields.items():
        if key not in _OWN_CHANNEL_FIELDS:
            raise ValueError(f"Unknown own_channel field: {key}")
        cols.append(f"{key} = ?")
        vals.append(value)
    if not cols:
        return
    vals.append(oc_id)
    conn.execute(f"UPDATE own_channels SET {', '.join(cols)} WHERE id = ?", vals)
    conn.commit()


def remove_own_channel(conn, oc_id: int) -> bool:
    """Delete an own channel from WhisperRadar only - the Renderly mirror is
    deliberately left alone (deleting there cascades assets/generations)."""
    cur = conn.execute("DELETE FROM own_channels WHERE id = ?", (oc_id,))
    conn.execute("UPDATE productions SET own_channel_id = NULL"
                 " WHERE own_channel_id = ?", (oc_id,))
    conn.commit()
    return cur.rowcount > 0


# ----------------------------------------------------------- settings ---

def all_settings(conn) -> dict:
    return {row["key"]: row["value"] for row in
            conn.execute("SELECT key, value FROM settings")}


def get_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?",
                       (key,)).fetchone()
    return default if row is None else row["value"]


def set_setting(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, None if value is None else str(value)),
    )
    conn.commit()
