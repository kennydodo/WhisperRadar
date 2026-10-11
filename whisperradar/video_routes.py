"""The Video options page: every ImgToVideo option, global or per own channel."""
from __future__ import annotations

from urllib.parse import quote

from flask import Response, redirect, render_template, request

from . import db
from .video import options as vopt
from .video import settings_spec as ss
from .video import sound_effects as sfx

_SFX_NOTES = {
    "pop": "the channel's default pop (chosen under Sound)",
    "ding": "bell-like ding, about 1 s", "click": "short dry click",
    "tick": "tiny high tick", "whoosh": "airy sweep, 0.7 s",
    "swipe": "fast swipe, 0.45 s",
}


def sfx_list() -> list[dict]:
    pops = dict(vopt.POP_VARIANTS)
    out = []
    for n in sfx.built_in_names():
        out.append({"name": n, "note": _SFX_NOTES.get(n) or pops.get(n, ""),
                    "group": "pop" if n.startswith("pop") else "other"})
    return out


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
            sounds=sfx_list(),
            msg=request.args.get("msg"), error=request.args.get("error"))

    @app.get("/video-options/sfx/<name>.wav")
    def video_options_sfx(name):
        """A built-in sound rendered with ffmpeg (cached), so it can be played."""
        low = name.lower()
        if low == sfx.POP_ALIAS:
            conn = db.connect(cfg.db_path)
            try:
                db.init_db(conn)
                low = (ss.load_global(conn).get("sound.default_pop")
                       or vopt.DEFAULT_POP_ID)
            finally:
                conn.close()
        if not sfx.is_built_in(low) or low == sfx.POP_ALIAS:
            return Response("unknown sound", status=404)
        folder = cfg.db_path.parent / "sfx_preview"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (low + ".wav")
        if not (path.is_file() and path.stat().st_size > 0):
            conn = db.connect(cfg.db_path)
            try:
                db.init_db(conn)
                exe = ss.load_global(conn).get("render.ffmpeg_path") or "ffmpeg"
            finally:
                conn.close()
            try:
                from .video.ffmpeg_runner import FfmpegRunner
                res = FfmpegRunner(exe).run(sfx.build_arguments(low, str(path)))
            except RuntimeError as exc:
                return Response(str(exc), status=503)
            if not res.success or not path.is_file():
                return Response("ffmpeg could not make this sound", status=500)
        return Response(path.read_bytes(), mimetype="audio/wav",
                        headers={"Cache-Control": "no-store"})

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
