"""The Video options page: every ImgToVideo option, global or per own channel."""
from __future__ import annotations

from urllib.parse import quote

from flask import redirect, render_template, request

from . import db
from .video import settings_spec as ss


def _groups():
    out = []
    for s in ss.SPEC:
        if not out or out[-1]["group"] != s["group"]:
            out.append({"group": s["group"], "title": s["title"], "items": []})
        out[-1]["items"].append(s)
    return out


def register(app, cfg) -> None:
    @app.get("/video-options")
    def video_options_page():
        scope = (request.args.get("channel") or "").strip()
        conn = db.connect(cfg.db_path)
        try:
            db.init_db(conn)
            chans = [{"id": c["id"], "name": c["name"]}
                     for c in db.list_own_channels(conn)]
            glob = ss.load_global(conn)
            if scope and not any(str(c["id"]) == scope for c in chans):
                scope = ""
            mine = ss.load_channel(conn, scope) if scope else glob
        finally:
            conn.close()
        # what a blank field falls back to
        inherited = {s["path"]: glob.get(s["path"], s["default"]) for s in ss.SPEC}
        return render_template(
            "video_options.html", groups=_groups(), values=mine,
            inherited=inherited, own_channels=chans, scope=scope,
            msg=request.args.get("msg"), error=request.args.get("error"))

    @app.post("/video-options/save")
    def video_options_save():
        scope = (request.form.get("channel") or "").strip()
        values, errors = ss.parse_form(request.form)
        if not errors:
            errors = ss.validate(values)
        back = "/video-options" + (f"?channel={quote(scope)}" if scope else "")
        sep = "&" if "?" in back else "?"
        if errors:
            return redirect(back + sep + "error=" + quote("; ".join(errors)))
        conn = db.connect(cfg.db_path)
        try:
            db.init_db(conn)
            if scope:
                ss.save_channel(conn, scope, values)
            else:
                ss.save_global(conn, values)
        finally:
            conn.close()
        return redirect(back + sep + "msg=" + quote(
            f"Saved {len(values)} option(s). New productions and the next "
            "merge pick them up."))
