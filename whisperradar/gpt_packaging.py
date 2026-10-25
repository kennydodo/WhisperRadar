"""Local queue for Codex-generated, finished packaging (no paid API calls).

The dashboard queues work. A scheduled Codex task claims one job and uses its
own image tool, then hands back generated paths and text via the local API.
Only the dashboard writes into production folders. A claim is never silently
repeated: abandoned work expires as failed and can be explicitly retried.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import autorun, db, packaging, prodfiles, studio, thumbnails

PACKAGING_BRIEF = """CONTENT AND EVIDENCE RULES
Read the entire final script before choosing a promise. Titles and thumbnails
must represent its main content, not turn one minor example into the whole video.
Identify the intended viewer, their concrete problem, and the useful outcome.
Lead with a clear, specific benefit or honest curiosity. Use cultural framing
only when the script supports it; do not invent national secrets, universal
customs, guaranteed fixes, or superiority claims.
Make the four options meaningfully different, faithful angles on the same video.
Pair the recommended title and thumbnail around ONE promise. In each title's
why, explain the script support and viewer benefit. In each thumbnail's idea,
explain the visual promise and how it complements the recommended title.
Use a clear focal subject/action, strong readable contrast and a short headline
(aim for 2-4 words where natural). Show a before/after only if the script supports
that outcome. Follow the channel's visual style and reference characters.
Check whether the opening delivers the promise promptly. If it does not, narrow
the packaging to what the finished video actually delivers and mention the
mismatch in notes; do not claim to have edited the video or fixed its opening.
Distinguish creative hypotheses from measured findings. With no supplied channel
analytics, say performance evidence is unavailable. Never invent demand, search
terms, CTR predictions, or a winning topic. A few dozen impressions, one or two
clicks, or one view with zero reported duration cannot establish packaging
failure, retention failure, or a winner. Consider dates, publication age, format,
traffic source and sample size before drawing comparisons. Missing is not zero.
Do not recommend replacing a live title or thumbnail solely because of sparse
early data, or claim a running A/B test has a winner without its results.
In notes, summarize the chosen promise, content fit, evidence limits, and one
testable hypothesis. Suggest recording impressions, CTR, traffic sources and
viewing behaviour at 24h/7d/28d after actual publication; this is a measurement
plan, not a scheduled follow-up or a guarantee of performance.

YOUTUBE SEO REQUIREMENTS
Before drafting, identify the viewer's search intent from the final script and
choose ONE primary search phrase (or two closely related phrases) plus 3-5
relevant secondary phrases in the video's language. Use words a viewer would
naturally search for, not internal production labels. Every phrase must match
content actually covered. Prefer supplied keyword/channel search evidence when
available; record its source and date. Without that evidence, explicitly label
the phrases as script-derived suggestions, not verified search demand. Do not
invent search volumes, competition scores, rankings, or keyword-tool results.

TITLES: All four options must make the subject understandable on their own.
Include the primary phrase naturally in the recommended title, preferably early
when readable; use relevant plain-language phrasing in the other options.
Keep important topic words near the beginning, avoid keyword lists, and retain
the existing 100-character maximum. Combine discoverability with an honest
benefit or curiosity. In each why, explain search intent as well as script fit.

DESCRIPTION: Write unique publish-ready prose. The first two sentences must
identify the topic, naturally include the primary phrase, and explain the
specific value delivered. Work relevant secondary phrases into useful sentences
only where the script supports them. Keep the viewer's language and channel
tone. Do not add keyword dumps, repetitive variants, irrelevant trends, or
unverified claims. Add a small set of relevant hashtags. Use supplied subtitle
or chapter timings for accurate chapters; omit timed chapters when reliable
timings are unavailable rather than inventing timestamps. Keep research notes
and internal SEO reasoning out of the public description.

THUMBNAILS: Communicate the same topic and promise visually. Use short readable
headlines that add to the title rather than repeat it word for word. Do not
stuff search phrases into thumbnail text or claim that text guarantees search
rankings. Preserve the channel's visual style, characters, and readable contrast.
Inspect the finished images for accurate text and readability at a small preview.

FINAL SEO CHECK: Before returning the package, check that the recommended title,
description opening, thumbnail and actual script opening deliver the same promise;
the title/description contain the chosen phrase naturally; each title explains
the subject; secondary phrases add useful context; and the four finished images
remain distinct and on-topic. Revise failures before submitting. Include an SEO
summary in notes: primary phrase, secondary phrases, search intent, evidence
basis (supplied evidence with source/date or script-derived suggestions), and
where the primary phrase appears in the recommended title and description.
Do not claim a measured SEO score, ranking improvement, or guaranteed views.
"""


_LOCK = threading.RLock()
LEASE_SECONDS = 7200


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect(cfg):
    conn = db.connect(cfg.db_path, timeout=30.0)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='gpt_packaging_jobs'").fetchone():
            conn.execute("""CREATE TABLE IF NOT EXISTS gpt_packaging_jobs (
        id TEXT PRIMARY KEY, production_id INTEGER NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        claim_token TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}')""")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS gpt_packaging_active "
                 "ON gpt_packaging_jobs(production_id) "
                 "WHERE status IN ('queued','running')")
            conn.commit()
        with conn:
            yield conn
    finally:
        conn.close()


def _public(row):
    if not row:
        return None
    return {k: row[k] for k in ("id", "production_id", "status", "created_at",
                                "updated_at", "error")}


def _expire(conn):
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(seconds=LEASE_SECONDS)).isoformat(timespec="seconds")
    conn.execute("UPDATE gpt_packaging_jobs SET status='failed',error=?,updated_at=? "
                 "WHERE status='running' AND updated_at<?",
                 ("The Codex worker did not finish. Retry generation to start again.",
                  _now(), cutoff))


def latest(cfg, pid):
    with _LOCK, _connect(cfg) as conn:
        _expire(conn)
        return _public(conn.execute("SELECT * FROM gpt_packaging_jobs "
                       "WHERE production_id=? ORDER BY rowid DESC LIMIT 1",
                       (pid,)).fetchone())


def latest_for(cfg, pids):
    """One pass for the Finished page: {production_id: latest job}."""
    pids = list(pids)
    if not pids:
        return {}
    with _LOCK, _connect(cfg) as conn:
        _expire(conn)
        marks = ",".join("?" * len(pids))
        rows = conn.execute(f"SELECT * FROM gpt_packaging_jobs WHERE "
                            f"production_id IN ({marks}) ORDER BY rowid",
                            pids).fetchall()
    return {row["production_id"]: _public(row) for row in rows}


def clear_error(cfg, pid):
    """Forget a production's FAILED jobs (the error line on the Finished page).
    Queued, running and completed jobs are left alone. Returns how many went."""
    with _LOCK, _connect(cfg) as conn:
        _expire(conn)
        cur = conn.execute("DELETE FROM gpt_packaging_jobs "
                           "WHERE production_id=? AND status='failed'", (pid,))
        return cur.rowcount


def queue_status(cfg):
    """Job history for the user queue page; never expose worker claim tokens."""
    with _LOCK, _connect(cfg) as conn:
        _expire(conn)
        rows = conn.execute("SELECT j.*,p.title AS production_title,p.status AS production_status "
                            "FROM gpt_packaging_jobs j LEFT JOIN productions p ON p.id=j.production_id "
                            "ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
                            "CASE WHEN j.status='queued' THEN j.rowid END ASC,j.rowid DESC LIMIT 200").fetchall()
        return [dict(_public(row), production_title=row["production_title"],
                     production_status=row["production_status"]) for row in rows]


def queue(cfg, pid):
    with _LOCK, _connect(cfg) as conn:
        conn.execute("BEGIN IMMEDIATE")
        prod = db.get_production(conn, pid)
        if not prod:
            raise ValueError("Unknown production")
        if prod["status"] != "ready":
            raise ValueError("Finish and approve this production first; published productions cannot generate GPT packaging")
        pdir = studio.prod_dir(cfg, pid)
        if not (pdir / "script.md").is_file():
            raise ValueError("The final script is missing")
        _expire(conn)
        active = conn.execute("SELECT * FROM gpt_packaging_jobs WHERE "
                              "production_id=? AND status IN ('queued','running')",
                              (pid,)).fetchone()
        if active:
            return _public(active)
        jid, now = uuid.uuid4().hex, _now()
        conn.execute("INSERT INTO gpt_packaging_jobs "
                     "(id,production_id,status,created_at,updated_at) "
                     "VALUES (?,?,'queued',?,?)", (jid, pid, now, now))
        return _public(conn.execute("SELECT * FROM gpt_packaging_jobs WHERE id=?",
                                   (jid,)).fetchone())


def _read(pdir, name):
    path = pdir / name
    return path.read_text(encoding="utf-8-sig") if path.is_file() else ""


def context(cfg, pid):
    with _connect(cfg) as conn:
        prod = db.get_production(conn, pid)
        if not prod or prod["status"] != "ready":
            raise ValueError("Production is no longer ready; published productions cannot generate GPT packaging")
        pdir = studio.prod_dir(cfg, pid)
        own = db.get_own_channel(conn, prod["own_channel_id"]) if prod["own_channel_id"] else None
        style, style_src, bible, bible_src = autorun.style_bible(cfg, conn, prod, pdir)
        ref_dirs = [pdir / "refs"]
        if own and own["refs_dir"]:
            ref_dirs.append(Path(own["refs_dir"]))
        refs = sorted({str(p.resolve()) for folder in ref_dirs if folder.is_dir()
                       for p in folder.iterdir()
                       if p.is_file() and p.suffix.lower() in studio.IMAGE_EXTS})
        images = sorted((pdir / "images").glob("*.png"))
        sample = [str(images[i].resolve()) for i in sorted(set(
            [0, len(images) // 2, len(images) - 1]))] if images else []
        _cues, chapters = packaging.load_chapters(pdir)
        payload = {"production_id": pid, "production_title": prod["title"],
                   "channel": own["name"] if own else "", "source_folder": str(pdir),
                   "script": _read(pdir, "script.md"),
                   "subtitles": _read(pdir, "subtitles.srt"),
                   "visual_style": style, "style_source": style_src,
                   "bible": bible, "bible_source": bible_src,
                   "references": refs, "sample_images": sample,
                   "chapters": chapters,
                   "existing_kit": packaging.load_kit(pdir)}
        if not payload["script"].strip():
            raise ValueError("The final script is missing or empty")
        digest = {k: payload[k] for k in ("production_title", "source_folder",
                                         "script", "subtitles", "visual_style", "bible")}
        payload["fingerprint"] = hashlib.sha256(json.dumps(
            digest, sort_keys=True).encode()).hexdigest()
        return payload


def claim(cfg):
    with _LOCK, _connect(cfg) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _expire(conn)
        # Serialize image-tool jobs even if two scheduled runs overlap.
        if conn.execute("SELECT 1 FROM gpt_packaging_jobs WHERE status='running'").fetchone():
            return None
        while True:
            row = conn.execute("SELECT * FROM gpt_packaging_jobs WHERE status='queued' "
                               "ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return None
            try:
                # Second connection reads while this transaction holds RESERVED:
                # writers block, readers do not.
                ctx = context(cfg, row["production_id"])
            except (ValueError, OSError) as exc:
                conn.execute("UPDATE gpt_packaging_jobs SET status='failed',error=?,updated_at=? "
                             "WHERE id=?", (str(exc), _now(), row["id"]))
                continue
            token = secrets.token_urlsafe(32)
            conn.execute("UPDATE gpt_packaging_jobs SET status='running',claim_token=?,"
                         "fingerprint=?,updated_at=? WHERE id=?",
                         (token, ctx["fingerprint"], _now(), row["id"]))
            return {"id": row["id"], "claim_token": token, "context": ctx}


def _authorized(conn, jid, token):
    row = conn.execute("SELECT * FROM gpt_packaging_jobs WHERE id=?", (jid,)).fetchone()
    if not row or not row["claim_token"] or not hmac.compare_digest(
            row["claim_token"], str(token or "")):
        raise ValueError("Invalid job claim")
    return row


def fail(cfg, jid, token, error):
    with _LOCK, _connect(cfg) as conn:
        row = _authorized(conn, jid, token)
        if row["status"] == "running":
            conn.execute("UPDATE gpt_packaging_jobs SET status='failed',error=?,updated_at=? "
                         "WHERE id=?", (str(error)[:1000], _now(), jid))
        return _public(conn.execute("SELECT * FROM gpt_packaging_jobs WHERE id=?", (jid,)).fetchone())


def worker_job(cfg, jid, token):
    """Resume the claim held by the local browser session, without requeueing."""
    with _LOCK, _connect(cfg) as conn:
        _expire(conn)
        row = _authorized(conn, jid, token)
        if row["status"] != "running":
            return None
        # Keep the lease alive: a worker polling this page is not abandoned.
        conn.execute("UPDATE gpt_packaging_jobs SET updated_at=? WHERE id=?",
                     (_now(), jid))
        return {"id": jid, "claim_token": token,
                "context": context(cfg, row["production_id"])}


def _generated_image(path):
    """A local worker may hand back only files from Codex's generated images."""
    root = (Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
            / "generated_images").resolve()
    src = Path(path).resolve()
    if not src.is_relative_to(root) or not src.is_file():
        raise ValueError("Thumbnail path must be a Codex-generated image")
    from PIL import Image, ImageOps
    with Image.open(src) as image:
        if not 1.72 <= image.width / image.height <= 1.83:
            raise ValueError("Each thumbnail must be landscape 16:9")
        image = ImageOps.fit(image.convert("RGB"), (1280, 720),
                             method=Image.Resampling.LANCZOS)
        for quality in (92, 85, 78, 70):
            stream = io.BytesIO()
            image.save(stream, "JPEG", quality=quality, optimize=True)
            if len(stream.getvalue()) < thumbnails.MAX_BYTES:
                return stream.getvalue()
    raise ValueError("Thumbnail is too large")


def complete(cfg, jid, token, result):
    with _LOCK, _connect(cfg) as conn:
        row = _authorized(conn, jid, token)
        if row["status"] == "complete":
            return json.loads(row["result"])
        if row["status"] != "running":
            raise ValueError("This job is no longer running")
        ctx = context(cfg, row["production_id"])
        if row["fingerprint"] != ctx["fingerprint"]:
            raise ValueError("Script, style or Bible changed. Retry with the current production.")
        if not isinstance(result, dict):
            raise ValueError("A packaging result is required")
        kit = packaging.parse_kit(result)
        if not kit["title"] or len(kit["title"]) > 100:
            raise ValueError("A title of 1-100 characters is required")
        if not 200 <= len(kit["description"]) <= 4500:
            raise ValueError("The description must contain 200-4500 characters")
        title_options = kit["titles"]
        if (len(title_options) != 4
                or any(not t["text"] or len(t["text"]) > 100 for t in title_options)
                or len({t["text"].casefold() for t in title_options}) != 4):
            raise ValueError("Exactly four distinct titles of 1-100 characters are required")
        if kit["title"] not in {t["text"] for t in title_options}:
            raise ValueError("The recommended title must be one of the four titles")
        items = result.get("thumbnails")
        if not isinstance(items, list) or len(items) != 4:
            raise ValueError("Exactly four finished thumbnails are required")
        prepared = []
        for it in items:
            if not isinstance(it, dict) or not str(it.get("text") or "").strip():
                raise ValueError("Each thumbnail needs its headline")
            prepared.append(_generated_image(it.get("path") or ""))
        pdir = Path(ctx["source_folder"])
        # Unique output paths and backups preserve older images/user edits.
        folder = thumbnails.thumbs_dir(pdir) / "jobs" / jid
        folder.mkdir(parents=True, exist_ok=True)
        for previous in (packaging.kit_path(pdir),
                         thumbnails.thumbs_dir(pdir) / thumbnails.THUMBS_FILE,
                         prodfiles.path(pdir, "description.txt"), prodfiles.path(pdir, "title.txt"), prodfiles.path(pdir, "titles.txt")):
            backup = folder / ("previous-" + previous.name)
            if previous.exists() and not backup.exists():
                shutil.copy2(previous, backup)
        concepts = []
        for i, (it, data) in enumerate(zip(items, prepared), 1):
            name = f"gpt-{jid[:8]}-{i}.jpg"
            dst = thumbnails.thumbs_dir(pdir) / name
            dst.write_bytes(data)
            concepts.append({"id": f"gpt_{jid[:8]}_{i}", "layout": "character",
                "text": str(it["text"]).strip(), "text_pos": "top",
                "text_color": "#FFD400", "accent": "#FFD400",
                "emphasis": "none", "focal": "auto", "art_prompt": str(it.get("prompt") or ""),
                "idea": str(it.get("idea") or ""), "art_file": "",
                "final": f"{thumbnails.thumbs_dir(pdir).relative_to(pdir).as_posix()}/{name}", "precomposed": True})
        kit.update(status="ready", provider="codex", description_complete=True,
                   notes=str(result.get("notes") or ""))
        # Description is complete text from the worker; keep hashtags/chapters
        # in the body so the kit does not add them a second time.
        kit["hashtags"], kit["chapter_titles"] = [], []
        try:
            recommended = int(result.get("recommended_thumbnail", 1))
        except (TypeError, ValueError):
            recommended = 1
        recommended = min(max(recommended, 1), len(concepts))
        packaging.save_kit(pdir, kit)
        prodfiles.wpath(pdir, "title.txt").write_text(kit["title"] + "\n", encoding="utf-8")
        prodfiles.wpath(pdir, "titles.txt").write_text("\n".join(t["text"] for t in title_options) + "\n", encoding="utf-8")
        prodfiles.wpath(pdir, "description.txt").write_text(kit["description"] + "\n", encoding="utf-8")
        thumbnails.save_thumbs(pdir, {"concepts": concepts, "chosen": None,
            "status": "ready", "score": None, "provider": "codex",
            "recommended": recommended})
        thumbnails.contact_sheet(pdir)
        (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
        saved = {"production_id": row["production_id"], "title": kit["title"],
                 "description_file": str(prodfiles.path(pdir, "description.txt")),
                 "title_file": str(prodfiles.path(pdir, "title.txt")),
                 "titles_file": str(prodfiles.path(pdir, "titles.txt")), "titles": title_options,
                 "thumbnails": [str(pdir / c["final"]) for c in concepts]}
        conn.execute("UPDATE gpt_packaging_jobs SET status='complete',result=?,"
                     "updated_at=?,error='' WHERE id=?", (json.dumps(saved), _now(), jid))
        return saved
