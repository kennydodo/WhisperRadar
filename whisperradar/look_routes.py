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
            send_count=channel_look.SEND_COUNT,
            max_send=channel_look.MAX_ATTACH,
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

    def _options():
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
        return options

    def _int(name, default):
        try:
            return int(request.form.get(name) or default)
        except ValueError:
            return default

    def _start(ch, label, call, note):
        """Shared by run/regen: web chat + transport + background job."""
        base = f"/watched/{quote(ch['channel_id'])}/look"
        site = (request.form.get("site") or "").strip()
        if site not in webchat.SITES:
            return redirect(base + "?error=" + quote(
                "Choose a web chat that can read images (add one in "
                "Settings > Web chat LLMs)"))
        if sjob.running:
            return redirect(base + "?error=" + quote("A job is already running"))
        hint = (request.form.get("hint") or "").strip()[:600]
        options = _options()
        _job = sjob._real()

        def worker():
            with webstages.web_transport(cfg, sjob.log.append, options) as transport:
                transport.set_stop(lambda: _job.cancel)
                call(site, transport, hint)

        sjob.start(worker, f"{label} ({ch['name']})")
        return redirect(base + "?msg=" + quote(note))

    @app.post("/watched/<key>/look/run")
    def look_run(key):
        ch = _channel(key)
        if ch is None:
            return redirect("/watched?error=Unknown+channel")
        frames = _int("frames", channel_look.DEFAULT_FRAMES)
        send = _int("send", channel_look.SEND_COUNT)
        cid = ch["channel_id"]

        def call(site, transport, hint):
            channel_look.run(cfg, cid, site, transport, sjob.log.append,
                             n_frames=frames, hint=hint, send_count=send)

        return _start(ch, "style & bible", call,
                      "Started - it downloads some videos at low resolution, "
                      "keeps up to 50 frames, sends a random selection to the "
                      "chat. This page reloads itself when the job is done.")

    @app.post("/watched/<key>/look/writing")
    def look_writing(key):
        """Ask for the writing style again from the stored transcripts (no
        download, no frames)."""
        ch = _channel(key)
        if ch is None:
            return redirect("/watched?error=Unknown+channel")
        cid = ch["channel_id"]

        def call(site, transport, hint):
            channel_look.regenerate_writing(cfg, cid, site, transport,
                                            sjob.log.append, hint=hint)

        return _start(ch, "writing style", call,
                      "Started - asking the chat for the writing style from "
                      "the transcripts of several videos. This page reloads "
                      "itself when done.")

    @app.post("/watched/<key>/look/regen")
    def look_regen(key):
        """Ask again with the frames already collected (no download). The
        ticked frames are sent; none ticked = a new random selection."""
        ch = _channel(key)
        if ch is None:
            return redirect("/watched?error=Unknown+channel")
        picked = request.form.getlist("pick")
        send = _int("send", channel_look.SEND_COUNT)
        cid = ch["channel_id"]
        if len(picked) > channel_look.MAX_ATTACH:
            return redirect(f"/watched/{quote(key)}/look?error=" + quote(
                f"Select at most {channel_look.MAX_ATTACH} frames "
                f"(you selected {len(picked)})"))

        def call(site, transport, hint):
            channel_look.regenerate(cfg, cid, site, transport,
                                    sjob.log.append, selected=picked,
                                    send_count=send, hint=hint)

        return _start(ch, "style & bible (regenerate)", call,
                      "Started - asking the chat again with "
                      + (f"your {len(picked)} selected frames."
                         if picked else f"{send} random frames.")
                      + " This page reloads itself when done.")
