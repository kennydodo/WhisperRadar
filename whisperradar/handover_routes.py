"""Routes for the judge -> writer hand-over templates (see handover.py)."""
from __future__ import annotations

import json
from urllib.parse import quote

from flask import redirect, render_template, request

from . import db, handover


INSTRUCTION_KEYS = ("plan", "plan_judge", "script", "script_judge")


def _back(pid: int, msg: str) -> str:
    """Back to the page the form was on (the production or its plan page)."""
    where = request.form.get("back") or ""
    base = f"/studio/{pid}"
    if where not in (base, base + "/plan"):
        where = base
    return where + "?msg=" + quote(msg)


def register(app, cfg) -> None:
    @app.get("/handover")
    def handover_page():
        conn = db.connect(cfg.db_path)
        try:
            db.init_db(conn)
            chans = [{"id": c["id"], "name": c["name"]}
                     for c in db.list_own_channels(conn)]
        finally:
            conn.close()
        names = {c["id"]: c["name"] for c in chans}
        for c in chans:
            c["default"] = handover.channel_defaults(cfg, c["id"])
            c["options"] = handover.available_for(cfg, c["id"])
        tpls = list(handover.all_templates(cfg).values())
        for t in tpls:
            t["channel_names"] = [names.get(i, f"#{i} (gone)")
                                  for i in t.get("channels") or []]
        return render_template(
            "handover.html", templates=tpls, own_channels=chans,
            previews={t["id"]: handover.preview(t) for t in tpls},
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
                mask_names=bool(request.form.get("mask_names")),
                channels=request.form.getlist("channels"),
                parts=request.form.getlist("parts"),
                help_text=(request.form.get("help") or "").strip(),
                instructions={k: request.form.get("ins_" + k)
                              for k in handover.INSTRUCTION_KEYS})
        except ValueError as exc:
            return redirect("/handover?error=" + quote(str(exc)))
        return redirect("/handover?msg=" + quote(f"Saved template {name}"))

    @app.post("/handover/channels/<tid>")
    def handover_channels(tid):
        ok = handover.set_template_channels(cfg, tid, request.form.getlist("channels"))
        return redirect("/handover?msg=" + quote("Channels saved" if ok else "Not found"))

    @app.post("/handover/channel-default/<int:cid>")
    def handover_channel_default(cid):
        handover.set_channel_default(cfg, cid, request.form.get("plan") or "",
                                     request.form.get("script") or "")
        return redirect("/handover?msg=" + quote("Channel default saved"))

    @app.post("/handover/rename/<tid>")
    def handover_rename(tid):
        try:
            ok = handover.rename_user_template(cfg, tid, request.form.get("name") or "")
        except ValueError as exc:
            return redirect("/handover?error=" + quote(str(exc)))
        return redirect("/handover?msg=" + quote("Renamed" if ok else "Not found"))

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
        return redirect(_back(pid, "Instructions saved"))

    @app.post("/studio/<int:pid>/handover")
    def handover_choose(pid):
        known = handover.all_templates(cfg)
        conn = db.connect(cfg.db_path)
        try:
            db.init_db(conn)
            prod = db.get_production(conn, pid)
        finally:
            conn.close()
        try:
            cid = prod["own_channel_id"]
        except (KeyError, IndexError, TypeError):
            cid = None
        inherit = handover.channel_defaults(cfg, cid)
        offered = {t["id"] for t in handover.available_for(cfg, cid)}
        try:
            chosen = json.loads(prod["handover"] or "{}")
            chosen = chosen if isinstance(chosen, dict) else {}
        except (KeyError, IndexError, TypeError, ValueError):
            chosen = {}
        for stage in handover.STAGES:
            if stage not in request.form:
                continue
            chosen.pop(stage, None)
            tid = (request.form.get(stage) or "").strip()
            base = inherit.get(stage) if inherit.get(stage) in offered \
                else handover.DEFAULT_ID
            if tid and tid in known and tid != base:
                chosen[stage] = tid
        conn = db.connect(cfg.db_path)
        try:
            db.init_db(conn)
            db.update_production(conn, pid, handover=json.dumps(chosen)
                                 if chosen else None)
        finally:
            conn.close()
        return redirect(_back(pid, "Hand-over template saved"))
