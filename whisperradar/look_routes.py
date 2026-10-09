"""Routes for "Get style & bible" on a watched channel (see channel_look)."""
from __future__ import annotations

from urllib.parse import quote

from flask import abort, jsonify, redirect, render_template, request, send_from_directory

from . import channel_look, db, webchat, webstages


def register(app, cfg, sjob) -> None:
    def _channel(key):
        conn = db.connect(cfg.db_path)
        try:
            db.init_db(conn)
            return db.get_channel(conn, key)
        finally:
            conn.close()

    @app.get("/watched/<key>/look")
    def look_page(key):
        ch = _channel(key)
        if ch is None:
            return redirect("/watched?error=Unknown+channel")
        sites = list(webchat.custom_sites_ui())
        return render_template(
            "look.html", ch=ch, sites=sites,
            look=channel_look.load(cfg, ch["channel_id"]),
            running=bool(sjob.running),
            default_frames=channel_look.DEFAULT_FRAMES,
            min_frames=channel_look.MIN_FRAMES,
            max_frames=channel_look.MAX_FRAMES,
            msg=request.args.get("msg"), error=request.args.get("error"))

    @app.get("/watched/<key>/look/status")
    def look_status(key):
        """Polled by the page: reloads itself when the job has finished."""
        try:
            last = str(list(sjob.log)[-1])[:300]
        except Exception:  # noqa: BLE001
            last = ""
        return jsonify(running=bool(sjob.running), last=last)

    @app.get("/watched/<key>/look/frame/<name>")
    def look_frame(key, name):
        ch = _channel(key)
        if ch is None or not name.endswith(".jpg"):
            abort(404)
        return send_from_directory(
            channel_look.look_dir(cfg, ch["channel_id"]) / "frames", name)

    @app.post("/watched/<key>/look/run")
    def look_run(key):
        ch = _channel(key)
        base = f"/watched/{quote(key)}/look"
        if ch is None:
            return redirect("/watched?error=Unknown+channel")
        site = (request.form.get("site") or "").strip()
        if site not in webchat.SITES:
            return redirect(base + "?error=" + quote(
                "Choose a web chat that can read images (add one in "
                "Settings > Web chat LLMs)"))
        if sjob.running:
            return redirect(base + "?error=" + quote("A job is already running"))
        try:
            frames = int(request.form.get("frames") or
                         channel_look.DEFAULT_FRAMES)
        except ValueError:
            frames = channel_look.DEFAULT_FRAMES
        hint = (request.form.get("hint") or "").strip()[:600]
        options = {}
        for s in webchat.custom_sites_ui():
            k = s["key"]
            options[k] = {
                "model": (request.form.get(f"wc_{k}_model") or "").strip()
                or None,
                "level": (request.form.get(f"wc_{k}_level") or "").strip()
                or None,
                "toggles": {t["id"]: request.form.get(f"wc_{k}_{t['id']}")
                            == "on" for t in s.get("toggles") or []}}
        _job = sjob._real()
        cid = ch["channel_id"]

        def worker():
            with webstages.web_transport(cfg, sjob.log.append, options) as transport:
                transport.set_stop(lambda: _job.cancel)
                channel_look.run(cfg, cid, site, transport, sjob.log.append,
                                 n_frames=frames, hint=hint)

        sjob.start(worker, f"style & bible ({ch['name']})")
        return redirect(base + "?msg=" + quote(
            "Started - it downloads a few videos at low resolution, takes "
            "frames and asks the chat. This page reloads itself when the job is done."))
