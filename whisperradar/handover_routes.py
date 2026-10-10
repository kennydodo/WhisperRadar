"""Routes for the judge -> writer hand-over templates (see handover.py)."""
from __future__ import annotations

import json
from urllib.parse import quote

from flask import redirect, render_template, request

from . import db, handover


INSTRUCTION_KEYS = ("plan", "plan_judge", "script", "script_judge")


def register(app, cfg) -> None:
    @app.get("/handover")
    def handover_page():
        return render_template(
            "handover.html", templates=list(handover.all_templates(cfg).values()),
            previews={t["id"]: handover.preview(t)
                      for t in handover.all_templates(cfg).values()},
            sample=handover.SAMPLE_NOTES, sample_source=handover.SAMPLE_SOURCE,
            parts=handover.PARTS, msg=request.args.get("msg"),
            error=request.args.get("error"))

    @app.post("/handover/save")
    def handover_save():
        name = (request.form.get("name") or "").strip()
        try:
            handover.save_user_template(
                cfg, name, scrub=bool(request.form.get("scrub")),
                guard=bool(request.form.get("guard")),
                parts=request.form.getlist("parts"),
                help_text=(request.form.get("help") or "").strip(),
                instructions={k: request.form.get("ins_" + k)
                              for k in handover.INSTRUCTION_KEYS})
        except ValueError as exc:
            return redirect("/handover?error=" + quote(str(exc)))
        return redirect("/handover?msg=" + quote(f"Saved template {name}"))

    @app.post("/handover/delete/<tid>")
    def handover_delete(tid):
        ok = handover.delete_user_template(cfg, tid)
        return redirect("/handover?msg=" + quote("Deleted" if ok else "Not found"))

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
        return redirect(f"/studio/{pid}?msg=" + quote("Instructions saved"))

    @app.post("/studio/<int:pid>/handover")
    def handover_choose(pid):
        known = handover.all_templates(cfg)
        chosen = {}
        for stage in handover.STAGES:
            tid = (request.form.get(stage) or "").strip()
            if tid and tid in known and tid != handover.DEFAULT_ID:
                chosen[stage] = tid
        conn = db.connect(cfg.db_path)
        try:
            db.init_db(conn)
            db.update_production(conn, pid, handover=json.dumps(chosen)
                                 if chosen else None)
        finally:
            conn.close()
        return redirect(f"/studio/{pid}?msg=" + quote("Hand-over templates saved"))
