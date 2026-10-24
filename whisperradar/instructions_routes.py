"""Route for the per-production writer / judge instructions (see handover.py)."""
from __future__ import annotations

import json
from urllib.parse import quote

from flask import redirect, request

from . import db, handover

INSTRUCTION_KEYS = handover.INSTRUCTION_KEYS


def _back(pid: int, msg: str) -> str:
    """Back to the page the form was on (the production or its plan page)."""
    where = request.form.get("back") or ""
    base = f"/studio/{pid}"
    if where not in (base, base + "/plan"):
        where = base
    return where + "?msg=" + quote(msg)


def register(app, cfg) -> None:
    @app.post("/studio/<int:pid>/instructions")
    def instructions_save(pid):
        """Your own instructions: the plan / script WRITER's extra direction
        and what the plan / script JUDGE should look for or ignore. Stored in
        the production's stage_extras (keys plan, plan_judge, script,
        script_judge); a field that is not sent is left alone."""
        conn = db.connect(cfg.db_path)
        try:
            db.init_db(conn)
            prod = db.get_production(conn, pid)
            try:
                data = json.loads(prod["stage_extras"] or "{}")
            except (ValueError, TypeError):
                data = {}
            data = data if isinstance(data, dict) else {}
            for key in INSTRUCTION_KEYS:
                if key in request.form:
                    data[key] = (request.form.get(key) or "").strip()
            db.update_production(conn, pid, stage_extras=json.dumps(data))
        finally:
            conn.close()
        return redirect(_back(pid, "Instructions saved"))
