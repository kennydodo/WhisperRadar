"""The channel's own script rules: they override the app's default script rules."""
from urllib.parse import quote

from flask import abort, redirect, render_template, request

from . import db, studio

MAX_CHARS = 12000


def register(app, cfg):
    @app.route("/my-channels/<int:ident>/script-rules", methods=["GET", "POST"])
    def channel_script_rules(ident):
        base = f"/my-channels/{ident}/script-rules"
        conn = db.connect(cfg.db_path)
        try:
            db.init_db(conn)
            row = db.get_own_channel(conn, ident)
            if row is None:
                abort(404)
            if request.method == "POST":
                text = (request.form.get("script_rules") or "").strip()
                if len(text) > MAX_CHARS:
                    return redirect(base + "?error=" + quote(
                        f"Script rules must be at most {MAX_CHARS:,} characters."))
                db.update_own_channel(conn, ident, script_rules=text)
                return redirect(base + "?msg=Script+rules+saved")
            rules = row["script_rules"] or ""
            name = row["name"]
        finally:
            conn.close()
        return render_template("script_rules.html", ident=ident, name=name,
                               rules=rules, defaults=studio.SCRIPT_EDITORIAL_DEFAULTS,
                               max_chars=MAX_CHARS,
                               msg=request.args.get("msg"), error=request.args.get("error"))
