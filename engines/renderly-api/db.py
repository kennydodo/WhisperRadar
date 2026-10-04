from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import DATABASE_URL

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _enable_sqlite_fk(dbapi_conn, _):
    """SQLite ignores foreign keys unless this pragma is enabled."""
    dbapi_conn.execute("PRAGMA foreign_keys=ON")


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


_MIGRATIONS = (
    "ALTER TABLE generations ADD COLUMN name VARCHAR(255) NOT NULL DEFAULT ''",
    "ALTER TABLE generations ADD COLUMN ref_asset_ids TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE generations ADD COLUMN ref_generation_ids TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE generations ADD COLUMN aspect_ratio VARCHAR(20) NOT NULL DEFAULT '16:9'",
    "ALTER TABLE generations ADD COLUMN ref_strength VARCHAR(20) NOT NULL DEFAULT 'balanced'",
    "ALTER TABLE generations ADD COLUMN cost_usd FLOAT NOT NULL DEFAULT 0",
    "ALTER TABLE generations ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE generations ADD COLUMN recent_removed INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE generations ADD COLUMN image_size VARCHAR(20) NOT NULL DEFAULT '1K'",
    "ALTER TABLE generations ADD COLUMN category VARCHAR(20) NOT NULL DEFAULT 'image'",
    "ALTER TABLE generations ADD COLUMN project_id INTEGER",
)

_DATA_MIGRATIONS = (
    # Failed generations are auto-hidden; sweep up any older rows.
    "UPDATE generations SET hidden = 1 WHERE status = 'error'",
    # Remove the auto-created default projects — their generations become
    # unassigned and live in the channel's "Unsorted" view instead.
    "UPDATE generations SET project_id = NULL WHERE project_id NOT IN "
    "(SELECT id FROM projects)",
    "DELETE FROM projects WHERE name = 'Default project'",
)


def ensure_schema():
    """Lightweight migrations for databases created before new columns existed."""
    with engine.begin() as conn:
        for ddl in _MIGRATIONS:
            try:
                conn.execute(text(ddl))
            except Exception:
                pass  # column already exists
        for ddl in _DATA_MIGRATIONS:
            try:
                conn.execute(text(ddl))
            except Exception:
                pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
