"""Copy the WATCHED channels (competitor list) from one machine to another.

  on the machine that has them:   python scripts/watched_transfer.py export watched.json
  on the machine that needs them: python scripts/watched_transfer.py import watched.json

Only the channel list moves (name, channel id, kind, genre, active). Channels
already present (same channel id) are left alone. Videos and view history are
NOT copied - the next fetch fills them in. Run it from the repo folder.
"""
import json
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "whisperradar.db"
COLS = ("name", "channel_id", "kind", "genre", "active")


def main(argv):
    if len(argv) != 3 or argv[1] not in ("export", "import"):
        print(__doc__)
        return 2
    mode, path = argv[1], Path(argv[2])
    con = sqlite3.connect(str(DB))
    if mode == "export":
        rows = [dict(zip(COLS, r)) for r in con.execute(
            "SELECT name, channel_id, kind, genre, active FROM channels "
            "ORDER BY id")]
        path.write_text(json.dumps(rows, indent=1, ensure_ascii=False),
                        encoding="utf-8")
        print(f"exported {len(rows)} watched channel(s) to {path}")
        return 0
    rows = json.loads(path.read_text(encoding="utf-8"))
    added = 0
    for r in rows:
        cur = con.execute(
            "INSERT OR IGNORE INTO channels (name, channel_id, kind, genre, "
            "active) VALUES (?,?,?,?,?)",
            (r["name"], r["channel_id"], r.get("kind") or "primary",
             r.get("genre") or "general", int(r.get("active", 1))))
        added += cur.rowcount
    con.commit()
    print(f"imported {added} new watched channel(s); "
          f"{len(rows) - added} were already here")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
