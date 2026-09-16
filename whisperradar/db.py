"""SQLite storage for channels, videos and run history."""

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
