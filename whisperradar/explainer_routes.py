"""The /explainer page: import, validate, storyboard and preview explainer.json.

Declarative JSON only (no LLM planner, no paid calls). A production picks
explainer mode by having explainer.json in its folder; this panel is the
safe editor for that file.
"""
from __future__ import annotations

import json
import os
from urllib.parse import quote

from flask import Response, redirect, render_template, request

from . import db, studio
from .video.explainer_planner import has_explainer
from .video.explainer import validate as ev
from .video.explainer.validate import ExplainerError


def _pid_dir(cfg, pid_raw: str):
    try:
        pid = int(pid_raw)
    except (TypeError, ValueError):
        return None
    conn = db.connect(cfg.db_path)
    try:
        db.init_db(conn)
        row = conn.execute("SELECT id FROM productions WHERE id = ?",
                           (pid,)).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    if not row:
        return None
    try:
        return studio.prod_dir(cfg, pid), pid
    except (OSError, ValueError, RuntimeError):
        return None


def _load_doc(pdir):
    path = os.path.join(pdir, "explainer.json")
    if not os.path.isfile(path):
        return None, None
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read()
    try:
        doc = ev.load(path, project_folder=pdir)
        return text, doc
    except ExplainerError as e:
        return text, e


def register(app, cfg) -> None:
    @app.get("/explainer")
    def explainer_page():
        pinfo = _pid_dir(cfg, request.args.get("pid") or "")
        if not pinfo:
            return Response("Unknown production", status=404)
        pdir, pid = pinfo
        text, doc = _load_doc(str(pdir))
        errors = []
        summary = None
        if isinstance(doc, ExplainerError):
            errors = doc.errors
        elif doc is not None:
            summary = (f"{len(doc.scenes)} scene(s), canvas "
                       f"{doc.canvas[0]}x{doc.canvas[1]}, theme {doc.theme.name}")
        return render_template(
            "explainer.html", pid=pid, has_file=has_explainer(str(pdir)),
            text=text or "", errors=errors, summary=summary,
            storyboards=_storyboards(str(pdir)),
            preview=_preview_name(str(pdir)),
            msg=request.args.get("msg"), error=request.args.get("error"))

    @app.post("/explainer/<pid>/import")
    def explainer_import(pid):
        pinfo = _pid_dir(cfg, pid)
        if not pinfo:
            return Response("Unknown production", status=404)
        pdir, _ = pinfo
        text = request.form.get("text") or ""
        back = f"/explainer?pid={quote(str(pid), safe='')}"
        if not text.strip():
            return redirect(back + "&error=" + quote("Paste explainer.json first."))
        path = os.path.join(str(pdir), "explainer.json")
        # validate BEFORE writing: a broken import never touches a good file
        probe = os.path.join(str(pdir), ".explainer-probe.json")
        try:
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write(text)
            doc = ev.load(probe, project_folder=str(pdir))
        except ExplainerError as e:
            try:
                os.remove(probe)
            except OSError:
                pass
            return redirect(back + "&error=" + quote(
                "explainer.json is invalid:\n" + "\n".join(e.errors[:8])))
        except OSError as e:
            return redirect(back + "&error=" + quote(f"Could not write: {e}"))
        os.replace(probe, path)
        return redirect(back + "&msg=" + quote(
            f"Imported explainer.json ({len(doc.scenes)} scene(s)). "
            "The next merge/render picks it up."))

    @app.post("/explainer/<pid>/validate")
    def explainer_validate(pid):
        pinfo = _pid_dir(cfg, pid)
        if not pinfo:
            return Response("Unknown production", status=404)
        pdir, _ = pinfo
        back = f"/explainer?pid={quote(str(pid), safe='')}"
        if not has_explainer(str(pdir)):
            return redirect(back + "&error=" + quote("No explainer.json yet."))
        text, doc = _load_doc(str(pdir))
        if isinstance(doc, ExplainerError):
            return redirect(back + "&error=" + quote(
                "explainer.json is invalid:\n" + "\n".join(doc.errors[:8])))
        if doc is None:
            return redirect(back + "&error=" + quote("No explainer.json yet."))
        # also check the SRT anchors resolve
        from .video.explainer import timing as tg
        from .video import parsing
        srt = _find_srt(str(pdir))
        if not srt:
            return redirect(back + "&error=" + quote(
                "No .srt file in the production folder - scenes cannot be timed."))
        with open(srt, encoding="utf-8-sig") as fh:
            cues = parsing.parse_srt(fh.read())
        try:
            compiled = tg.compile_doc(doc, cues, 30.0)
        except ExplainerError as e:
            return redirect(back + "&error=" + quote(
                "Timing check failed:\n" + "\n".join(e.errors[:8])))
        lines = [f"scene {cs.spec.id}: frames [{cs.start_frame}, {cs.end_frame}), "
                 f"{len(cs.events)} event(s)" for cs in compiled]
        return redirect(back + "&msg=" + quote(
            f"Valid. {len(cues)} cues, {len(compiled)} scene(s). " + " | ".join(lines)))

    @app.post("/explainer/<pid>/storyboard")
    def explainer_storyboard(pid):
        pinfo = _pid_dir(cfg, pid)
        if not pinfo:
            return Response("Unknown production", status=404)
        pdir, _ = pinfo
        back = f"/explainer?pid={quote(str(pid), safe='')}"
        code, out = _run_cli(["storyboard", str(pdir)])
        if code != 0:
            return redirect(back + "&error=" + quote(out[-600:] or "storyboard failed"))
        return redirect(back + "&msg=" + quote("Storyboard written to out/explainer/storyboard."))

    @app.post("/explainer/<pid>/preview")
    def explainer_preview(pid):
        pinfo = _pid_dir(cfg, pid)
        if not pinfo:
            return Response("Unknown production", status=404)
        pdir, _ = pinfo
        back = f"/explainer?pid={quote(str(pid), safe='')}"
        code, out = _run_cli(["render-demo", str(pdir)])
        if code != 0:
            return redirect(back + "&error=" + quote(out[-600:] or "preview failed"))
        return redirect(back + "&msg=" + quote("Preview written to out/preview.mp4."))

    @app.get("/explainer/<pid>/storyboard/<path:name>")
    def explainer_storyboard_file(pid, name):
        pinfo = _pid_dir(cfg, pid)
        if not pinfo:
            return Response("Unknown production", status=404)
        pdir, _ = pinfo
        base = os.path.abspath(os.path.join(str(pdir), "out", "explainer", "storyboard"))
        target = os.path.abspath(os.path.join(base, name))
        if not target.startswith(base) or not os.path.isfile(target):
            return Response("Not found", status=404)
        with open(target, "rb") as fh:
            return Response(fh.read(), mimetype="image/png",
                            headers={"Cache-Control": "no-store"})

    @app.get("/explainer/<pid>/preview.mp4")
    def explainer_preview_file(pid):
        pinfo = _pid_dir(cfg, pid)
        if not pinfo:
            return Response("Unknown production", status=404)
        pdir, _ = pinfo
        target = os.path.join(str(pdir), "out", "preview.mp4")
        if not os.path.isfile(target):
            return Response("Not found", status=404)
        with open(target, "rb") as fh:
            return Response(fh.read(), mimetype="video/mp4",
                            headers={"Cache-Control": "no-store"})


def _find_srt(pdir: str):
    for n in sorted(os.listdir(pdir)):
        if n.lower().endswith(".srt"):
            return os.path.join(pdir, n)
    return None


def _storyboards(pdir: str) -> list:
    sb_dir = os.path.join(pdir, "out", "explainer", "storyboard")
    if not os.path.isdir(sb_dir):
        return []
    return sorted("storyboard/" + n for n in os.listdir(sb_dir)
                  if n.lower().endswith(".png"))


def _preview_name(pdir: str) -> bool:
    return os.path.isfile(os.path.join(pdir, "out", "preview.mp4"))


def _run_cli(args: list) -> tuple:
    """Run the explainer CLI in-process (subprocess of this interpreter)."""
    import subprocess
    import sys
    cmd = [sys.executable, "-m", "whisperradar.video.explainer.cli", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        return 1, str(e)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out
