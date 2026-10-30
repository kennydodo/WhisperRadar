"""Opening library, channel choices and production overrides."""
import json
from urllib.parse import quote

from flask import abort, redirect, render_template, request

from . import db, openings


def register(app, cfg):
    def connection():
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        return conn

    @app.get("/opening-styles")
    def opening_styles_page():
        conn = connection()
        try:
            styles = openings.catalog(conn)
        finally:
            conn.close()
        return render_template("opening_styles.html", styles=styles,
                               msg=request.args.get("msg"), error=request.args.get("error"))

    @app.post("/opening-styles/save")
    def opening_styles_save():
        conn = connection()
        try:
            openings.save_custom(conn, request.form.get("name", ""),
                                 request.form.get("instructions", ""),
                                 request.form.get("example", ""), request.form.get("id", ""))
        except ValueError as exc:
            return redirect("/opening-styles?error=" + quote(str(exc)))
        finally:
            conn.close()
        return redirect("/opening-styles?msg=Opening+style+saved")

    def choices(kind, ident):
        conn = connection()
        base = f"/my-channels/{ident}/opening" if kind == "channel" else f"/studio/{ident}/opening"
        try:
            row = db.get_own_channel(conn, ident) if kind == "channel" else db.get_production(conn, ident)
            if row is None:
                abort(404)
            if request.method == "POST":
                try:
                    if kind == "channel":
                        ids = request.form.getlist("styles")
                        openings.snapshot(conn, ids)
                        db.update_own_channel(conn, ident, opening_styles=json.dumps(list(dict.fromkeys(ids))))
                    elif request.form.get("action") == "refresh":
                        channel = db.get_own_channel(conn, row["own_channel_id"]) if row["own_channel_id"] else None
                        value = openings.snapshot(conn, openings.channel_ids(channel))
                        db.update_production(conn, ident, opening_config=json.dumps(value, ensure_ascii=False))
                    else:
                        value = openings.snapshot(conn, request.form.getlist("styles"),
                                                  request.form.get("custom", ""), request.form.get("chosen", ""),
                                                  openings.resolve(conn, row)["styles"])
                        db.update_production(conn, ident, opening_config=json.dumps(value, ensure_ascii=False))
                except ValueError as exc:
                    return redirect(base + "?error=" + quote(str(exc)))
                return redirect(base + "?msg=Opening+choices+saved")
            value = openings.resolve(conn, row) if kind == "production" else dict(styles=[], chosen="", custom="")
            selected = [x["id"] for x in value["styles"]] if kind == "production" else openings.channel_ids(row)
            styles = openings.catalog(conn)
            # Frozen custom styles remain selectable even after importing a production.
            frozen = {x["id"]: x for x in value["styles"]}
            styles = [frozen.pop(x["id"], x) for x in styles]
            styles += list(frozen.values())
            title = row["name"] if kind == "channel" else row["title"]
        finally:
            conn.close()
        return render_template("opening_choices.html", kind=kind, ident=ident, title=title,
                               styles=styles, selected=selected, value=value, base=base,
                               msg=request.args.get("msg"), error=request.args.get("error"))

    @app.route("/my-channels/<int:ident>/opening", methods=["GET", "POST"])
    def channel_opening(ident):
        return choices("channel", ident)

    @app.route("/studio/<int:ident>/opening", methods=["GET", "POST"])
    def production_opening(ident):
        return choices("production", ident)
