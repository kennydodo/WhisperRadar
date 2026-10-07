"""Thumbnails for a finished video: 3-5 concepts (layout, short catchy text,
art prompt) written by a web-chat writer and scored by a judge, art made by
the channel's image engine, text added by Pillow so it is always sharp, then a
mobile-size contact sheet to judge them the way a viewer sees them.

Layouts: the character and the host together, the character alone, the host
alone. Pure parts (parsing, checks, compositing) are tested without a browser
or an image engine. The engine call mirrors how the reference images are made
(FlowBatch job / Renderly ImageGen); it is only exercised live by the user.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

THUMB_DIR = "thumbnails"
THUMBS_FILE = "thumbs.json"
W, H = 1280, 720
MAX_BYTES = 2_000_000          # YouTube's thumbnail limit is 2 MB
MIN_CONCEPTS, MAX_CONCEPTS = 3, 5
TEXT_MAX_WORDS = 4
TEXT_MAX_CHARS = 28
MIN_SCORE = 8.0
LAYOUTS = {
    "character_host": "the character and the host together",
    "character": "the character alone",
    "host": "the host alone",
}
POSITIONS = ("left", "right", "top", "bottom")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

_LAYOUT_ART = {
    "character_host": ("both the character and the human host clearly "
                       "visible side by side, big expressive faces, the "
                       "character on one side and the host on the other"),
    "character": "the character alone, large, close-up, big expressive face",
    "host": "the human host alone, large, close-up, big expressive face",
}


# ---- storage ---------------------------------------------------------------

def thumbs_dir(pdir) -> Path:
    return Path(pdir) / THUMB_DIR


def empty_thumbs() -> dict:
    return {"concepts": [], "chosen": None, "status": "none", "score": None}


def load_thumbs(pdir) -> dict:
    try:
        data = json.loads((thumbs_dir(pdir) / THUMBS_FILE)
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_thumbs()
    base = empty_thumbs()
    if isinstance(data, dict):
        base.update(data)
    if not isinstance(base["concepts"], list):
        base["concepts"] = []
    return base


def save_thumbs(pdir, data: dict) -> None:
    d = thumbs_dir(pdir)
    d.mkdir(parents=True, exist_ok=True)
    (d / THUMBS_FILE).write_text(
        json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


# ---- concepts ----------------------------------------------------------------

def clean_text(text) -> str:
    """The words on the thumbnail: trimmed, at most 4 words / 28 characters."""
    words = re.sub(r"\s+", " ", str(text or "")).strip().split(" ")
    words = [w for w in words if w][:TEXT_MAX_WORDS]
    out = " ".join(words)
    while len(out) > TEXT_MAX_CHARS and len(words) > 1:
        words.pop()
        out = " ".join(words)
    return out[:TEXT_MAX_CHARS].strip()


def _color(value, default: str) -> str:
    value = str(value or "").strip()
    return value if _HEX.match(value) else default


def parse_concepts(raw) -> list[dict]:
    """Normalised concepts from the writer's JSON (a dict with "concepts" or
    a bare list). Unusable entries are dropped; ids are c1, c2, ..."""
    items = raw.get("concepts") if isinstance(raw, dict) else raw
    out: list[dict] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        layout = str(it.get("layout") or "").strip().lower().replace(
            " ", "_").replace("+", "_")
        if layout in ("both", "character_and_host", "host_character"):
            layout = "character_host"
        if layout not in LAYOUTS:
            continue
        prompt = re.sub(r"\s+", " ", str(it.get("art_prompt") or "")).strip()
        if not prompt:
            continue
        pos = str(it.get("text_pos") or "").strip().lower()
        out.append({
            "id": f"c{len(out) + 1}", "layout": layout,
            "text": clean_text(it.get("text")),
            "text_pos": pos if pos in POSITIONS else "left",
            "text_color": _color(it.get("text_color"), "#FFFFFF"),
            "accent": _color(it.get("accent"), "#FFD400"),
            "art_prompt": prompt,
            "idea": str(it.get("idea") or "").strip()[:200],
            "art_file": "", "final": "",
        })
        if len(out) >= MAX_CONCEPTS:
            break
    return out


def local_faults(concepts: list[dict], title: str = "") -> list[str]:
    """Rule checks that need no LLM."""
    faults = []
    if len(concepts) < MIN_CONCEPTS:
        faults.append(f"need {MIN_CONCEPTS}-{MAX_CONCEPTS} concepts, got "
                      f"{len(concepts)}")
    if len({c["layout"] for c in concepts}) < 2 and len(concepts) >= 2:
        faults.append("use at least two different layouts (character+host, "
                      "character alone, host alone)")
    seen = set()
    for c in concepts:
        t = c["text"].strip().lower()
        if not t:
            faults.append(f"{c['id']}: the thumbnail text is empty")
        elif t in seen:
            faults.append(f"{c['id']}: same text as another concept")
        seen.add(t)
        if title and t and t == title.strip().lower():
            faults.append(f"{c['id']}: the text just repeats the title - "
                          "it should add to the title, not copy it")
    return faults


# ---- prompts -------------------------------------------------------------------

def context(cfg, pid: int) -> dict:
    from . import packaging, studio
    ctx = packaging.context(cfg, pid)
    kit = packaging.load_kit(ctx["pdir"])
    ctx["kit_title"] = kit.get("title") or ctx["title"]
    ctx["keyword"] = kit.get("keyword") or ""
    try:
        bible = (Path(ctx["pdir"]) / "bible.md").read_text(encoding="utf-8")
    except OSError:
        bible = ""
    ctx["bible"] = bible.strip()[:3000]
    try:
        refs = studio.shotlist_refs(Path(ctx["pdir"]))
    except Exception:  # noqa: BLE001
        refs = {}
    ctx["ref_text"] = "\n".join(
        f"- {n}: {(r.get('prompt') or '(supplied image)')[:300]}"
        for n, r in refs.items()) or "(none)"
    return ctx


_RULES = f"""Thumbnail rules:
- 1280x720, and it is judged at phone size (about 170 px wide): one clear subject, big faces, strong contrast, nothing small.
- The words add to the title, they never repeat it: at most {TEXT_MAX_WORDS} words, short and catchy, a curiosity gap, a number or a reaction (e.g. "NOBODY KNEW", "WAIT, WHAT?").
- Three layouts exist: "character_host" (the character and the human host together), "character" (the character alone) and "host" (the host alone). Mix them across the concepts.
- Leave clear empty space on the side where the text goes (text_pos). The image itself has NO text, letters or logos - the words are added afterwards.
- art_prompt must describe the whole picture on its own (who, expression, pose, background, lighting, colours) in the channel's art style, in under 500 characters. Describe the character and host from the descriptions given; do not assume the image tool knows them."""


def _plan_text(ctx: dict) -> str:
    t = (ctx.get("plan") or {}).get("thumbnail") or {}
    if not t.get("text") and not t.get("idea"):
        return ""
    return ("THUMBNAIL IDEA FROM THE PACKAGING PLAN (one of your concepts "
            f"should build on it): layout {t.get('layout')}, words "
            f"\"{t.get('text')}\", {t.get('idea')}\n\n")


def writer_prompt(ctx: dict) -> str:
    return f"""You are a YouTube thumbnail designer. Design {MIN_CONCEPTS}-{MAX_CONCEPTS} thumbnail concepts for a finished video.

CHANNEL: {ctx['channel'] or '(unnamed)'} - genre: {ctx['genre']}. {ctx['channel_about']}
VIDEO TITLE: {ctx['kit_title']}
MAIN KEYWORD: {ctx['keyword'] or '(none)'}

{_plan_text(ctx)}THE SCRIPT (the thumbnail must never promise more than the video delivers):
{ctx['script'][:6000]}

THE CHARACTERS AND HOST (from the channel bible):
{ctx['bible'] or '(no bible text)'}
Reference images used in the video:
{ctx['ref_text']}

THUMBNAILS THAT BEAT THEIR CHANNEL'S NORM IN THIS NICHE (titles only - learn the angle):
{chr(10).join(f'- {t} ({m:.0f}x)' for t, m in ctx['refs']) or '(none available)'}

{_RULES}

Reply with ONE JSON object and nothing else:
{{"concepts": [{{"layout": "character_host|character|host", "text": "2-4 words", "text_pos": "left|right|top|bottom", "text_color": "#FFFFFF", "accent": "#FFD400", "art_prompt": "...", "idea": "one line: why this one gets the click"}}, ...]}}"""


def _concepts_json(concepts: list[dict]) -> str:
    keep = ("layout", "text", "text_pos", "text_color", "accent",
            "art_prompt", "idea")
    return json.dumps({"concepts": [{k: c[k] for k in keep}
                                    for c in concepts]},
                      ensure_ascii=False, indent=1)


def judge_prompt(ctx: dict, concepts: list[dict], faults: list[str]) -> str:
    return f"""You are a strict YouTube thumbnail reviewer. Judge these thumbnail concepts for a video.

CHANNEL: {ctx['channel'] or '(unnamed)'} - genre: {ctx['genre']}
VIDEO TITLE: {ctx['kit_title']}
WHAT THE VIDEO SAYS: {ctx['script'][:1500]}

THE CONCEPTS:
{_concepts_json(concepts)}

RULE CHECKS ALREADY FAILING (code-checked): {faults or 'none'}

{_RULES}

Score 1-10 how likely the BEST of these is to win the click at phone size next to the title, and that the set offers real variety. Name the exact concept (by position, 1-based) and text that is weak.

Reply with ONE JSON object and nothing else:
{{"score": 7.5, "pass": false, "faults": ["specific problem"], "fixes": ["specific rewrite"]}}
"pass" is true only when the score is {MIN_SCORE:g} or higher and nothing is left to fix."""


def judge_followup(concepts: list[dict], faults: list[str]) -> str:
    return ("The designer revised the concepts after your review. Judge "
            "again under the SAME rules and reply in exactly the SAME JSON "
            "format. First check each point you raised, then check nothing "
            f"else got worse.\n\nRULE CHECKS STILL FAILING: {faults or 'none'}"
            "\n\n" + _concepts_json(concepts))


def writer_feedback(verdict: dict, faults: list[str]) -> str:
    return ("The reviewer found problems with your concepts.\n"
            + (f"Rule checks run by code (fix these too): "
               f"{'; '.join(faults)}\n" if faults else "")
            + "The reviewer's verdict, verbatim (JSON):\n"
            + json.dumps(verdict, ensure_ascii=False, indent=1)
            + "\n\nChange ONLY what is named; keep the rest as it was. "
              "Return ALL the concepts again as one JSON object in the same "
              "format, and nothing else.")


def run_concepts(cfg, pid: int, transport, writer: str = "zai",
                 judge: str = "deepseek", log: Callable[[str], None] = print,
                 should_stop: Callable[[], bool] = lambda: False,
                 min_score: float = MIN_SCORE) -> dict:
    """The writer/judge loop for the concepts. Keeps art and final files of
    concepts whose art prompt did not change; saves the best set."""
    from . import studio, webstages as ws
    ctx = context(cfg, pid)
    if not ctx["script"]:
        raise ws.StageFailed("This production has no script yet.")
    log(f"thumbnails: designing concepts for \"{ctx['kit_title'][:70]}\"")
    reply = ws._send(transport, writer, lambda f: writer_prompt(ctx), log,
                     ready=ws._has_json)
    best, judge_open, rnd, empty = None, False, 0, 0
    while True:
        rnd += 1
        concepts = parse_concepts(studio._parse_json_object(reply))
        if not concepts:
            empty += 1
            if empty >= 3:
                raise ws.StageFailed(f"{writer} did not return concepts")
            log(f"{writer}: no usable concepts - asking again")
            reply = transport.ask(
                writer, "Your reply had no usable concepts. Reply with ONE "
                "JSON object {\"concepts\": [...]} in the format given, and "
                "nothing else.", (), new_chat=False, ready=ws._has_json)
            continue
        faults = local_faults(concepts, ctx["kit_title"])
        log(f"round {rnd}: {len(concepts)} concept(s), {len(faults)} rule "
            f"fault(s); asking {judge} to review")
        verdict, score = {}, None
        for attempt in (1, 2):
            if judge_open:
                raw = ws._send(transport, judge,
                               lambda f: judge_followup(concepts, faults),
                               log, new_chat=False, ready=ws._is_json_verdict)
            else:
                raw = ws._send(transport, judge,
                               lambda f: judge_prompt(ctx, concepts, faults),
                               log, ready=ws._is_json_verdict)
            verdict = studio._parse_json_object(raw)
            if verdict:
                judge_open = True
            try:
                score = round(float(verdict.get("score")), 1)
                break
            except (TypeError, ValueError):
                score = None
                log(f"{judge} gave no usable score"
                    + (" - asking once more" if attempt == 1 else ""))
        passed = (score is not None and score >= min_score and not faults
                  and verdict.get("pass") is not False)
        log(f"round {rnd}: score {score if score is not None else 'n/a'} "
            f"(min {min_score:g}) - "
            + ("PASSED" if passed else "not accepted"))
        rank = (not faults, score or 0.0)
        if best is None or rank > best[0]:
            best = (rank, concepts, score, passed)
        if passed or should_stop():
            if not passed:
                log("stopped by you")
            break
        reply = transport.ask(writer, writer_feedback(
            verdict or {"faults": ["no verdict"]}, faults), (),
            new_chat=False, ready=ws._has_json)
    _rank, concepts, score, passed = best
    data = load_thumbs(ctx["pdir"])
    old = {c["art_prompt"]: c for c in data["concepts"] if c.get("art_file")}
    for c in concepts:               # same art prompt = keep the made picture
        prev = old.get(c["art_prompt"])
        if prev:
            c["art_file"], c["final"] = prev["art_file"], ""
    data.update({"concepts": concepts, "score": score, "chosen": None,
                 "status": "ready" if passed else "draft"})
    save_thumbs(ctx["pdir"], data)
    log("thumbnail concepts saved" + ("" if passed else " as a draft"))
    return data


def concepts_job(cfg, pid: int, writer: str, judge: str, log,
                 should_stop: Callable[[], bool] = lambda: False,
                 options: dict | None = None) -> None:
    from . import webstages as ws
    with ws.web_transport(cfg, log, options) as t:
        log(f"settings: {t.options}")
        t.set_stop(should_stop)
        run_concepts(cfg, pid, t, writer, judge, log=log,
                     should_stop=should_stop)


# ---- art -----------------------------------------------------------------------

def art_prompt_for(c: dict) -> str:
    side = {"left": "the right", "right": "the left", "top": "the bottom",
            "bottom": "the top"}[c["text_pos"]]
    # the subject goes where the words are not
    room = {"left": "the left", "right": "the right", "top": "the top",
            "bottom": "the bottom"}[c["text_pos"]]
    return (f"{c['art_prompt']} Composition: {_LAYOUT_ART[c['layout']]}, "
            f"placed on {side} of the frame, leaving clear simple empty "
            f"space on {room} for text. 16:9. No text, no letters, no "
            "words, no logos, no watermark.")


def _style_for(pdir: Path) -> str:
    from . import studio
    style = ""
    try:
        style = str(json.loads((pdir / "shotlist.json")
                               .read_text(encoding="utf-8")).get("style")
                    or "")
    except (OSError, ValueError):
        style = ""
    if not style.strip():
        p = studio.find_style(pdir)
        style = p.read_text(encoding="utf-8") if p else ""
    return style.strip()


def _engine_flowbatch(cfg, pdir: Path, pid: int, prompts: dict, log,
                      cancel) -> None:
    """FlowBatch generate into thumbnails/art/<id>.png (mirrors
    studio.run_flowbatch_refs)."""
    from . import chrome_profile, studio
    if not studio.flowbatch_ready(cfg):
        raise RuntimeError("thumbnail art needs FlowBatch - set "
                           "studio.flowbatch_repo in config.yaml")
    out = thumbs_dir(pdir) / "art"
    out.mkdir(parents=True, exist_ok=True)
    style = _style_for(pdir)
    job: dict = {
        "name": f"wr-{pid}-thumbs", "outputsDir": str(out),
        "refMode": "reuse",
        "defaults": {"mode": "image", "agent": False, "aspectRatio": "16:9",
                     "outputs": 1, "refMode": "reuse"},
        "images": [{"file": f"{cid}.png", "prompt": p}
                   for cid, p in prompts.items()],
    }
    url, _ = studio.flow_project_url_for(cfg, pid)
    if url:
        job["projectUrl"] = url
    longest = max(len(p) for p in prompts.values())
    if style and longest + len(style) + 1 <= studio.FLOWBATCH_MAX_PROMPT_CHARS:
        job["style"] = style
    job_path = thumbs_dir(pdir) / "flowbatch_thumbs.json"
    job_path.write_text(json.dumps(job, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    studio.set_flowbatch_tier(cfg, 0)
    chrome_profile.apply(cfg, "flowbatch", log)
    cmd = studio._flowbatch_cmd(["generate", "--job", str(job_path),
                                 "--output", str(out), "--no-color"])
    if url:
        cmd += ["--project-url", url]
    log(f"thumbnails: generating {len(prompts)} picture(s) with FlowBatch")
    code, tail = studio._flowbatch_stream(cmd, studio.flowbatch_dir(cfg),
                                          log, cancel)
    if code != 0:
        raise RuntimeError(f"thumbnail art failed (exit {code}): "
                           + " | ".join(tail[-4:])[:300])


def _engine_renderly(cfg, pdir: Path, pid: int, prompts: dict, channel,
                     log) -> None:
    """Renderly ImageGen through a throwaway project (mirrors
    studio.run_renderly_refs)."""
    from . import studio
    out = thumbs_dir(pdir) / "art"
    out.mkdir(parents=True, exist_ok=True)
    style = _style_for(pdir)
    tmp = Path(tempfile.mkdtemp(prefix=f"wr-{pid}-thumbs-"))
    try:
        job: dict = {
            "shots": [{"asset": cid, "cues": "1-1"} for cid in prompts],
            "images": [{"file": f"{cid}.png", "prompt": p}
                       for cid, p in prompts.items()]}
        if style:
            job["style"] = style
        (tmp / "shotlist.json").write_text(
            json.dumps(job, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        log(f"thumbnails: generating {len(prompts)} picture(s) with Renderly")
        studio.run_imagegen(cfg, tmp, channel=channel, upscale=None)
        for cid in prompts:
            src = next((p for p in ((tmp / "images") / f"{cid}{e}"
                                    for e in (".png", ".jpg", ".jpeg"))
                        if p.is_file()), None)
            if src is not None:
                shutil.copy(src, out / f"{cid}.png")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def generate_art(cfg, pid: int, ids: list[str] | None = None, log=print,
                 cancel=None) -> dict:
    """Make the picture for each concept (or just `ids`) with the channel's
    image engine, then compose the final thumbnails. {made, missing}."""
    from . import autorun, studio
    pdir = studio.prod_dir(cfg, pid)
    data = load_thumbs(pdir)
    todo = [c for c in data["concepts"] if not ids or c["id"] in ids]
    if not todo:
        raise RuntimeError("No thumbnail concepts yet - design them first.")
    prompts = {c["id"]: art_prompt_for(c) for c in todo}
    eff = autorun._effective(cfg, pid)
    engine = studio.effective_engine(
        eff["engine"], autorun._default_render_mode(
            cfg, autorun._get_prod(cfg, pid)))
    if engine == "flowbatch":
        _engine_flowbatch(cfg, pdir, pid, prompts, log, cancel)
    else:
        channel = studio.resolve_renderly_channel(
            cfg, eff["own_channel"], create=True)
        _engine_renderly(cfg, pdir, pid, prompts, channel, log)
    made, missing = [], []
    art = thumbs_dir(pdir) / "art"
    for c in todo:
        f = next((p for p in (art / f"{c['id']}{e}"
                              for e in (".png", ".jpg", ".jpeg"))
                  if p.is_file()), None)
        if f is None:
            missing.append(c["id"])
            continue
        c["art_file"] = f"{THUMB_DIR}/art/{f.name}"
        made.append(c["id"])
    save_thumbs(pdir, data)
    compose_all(pdir)
    if missing:
        log(f"thumbnails: no picture came back for {', '.join(missing)}")
    log(f"thumbnails: {len(made)} picture(s) ready")
    return {"made": made, "missing": missing}


# ---- compositing -----------------------------------------------------------------

_FONTS = ("impact.ttf", "Impact.ttf", "arialbd.ttf", "segoeuib.ttf",
          "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf")
_FONT_DIRS = ("C:/Windows/Fonts", "/usr/share/fonts/truetype/dejavu",
              "/usr/share/fonts/truetype/liberation",
              "/usr/share/fonts/TTF", "/Library/Fonts",
              "/System/Library/Fonts/Supplemental")


def _font(size: int):
    from PIL import ImageFont
    for d in _FONT_DIRS:
        for name in _FONTS:
            p = Path(d) / name
            if p.is_file():
                try:
                    return ImageFont.truetype(str(p), size)
                except OSError:
                    continue
    try:
        return ImageFont.load_default(size)
    except TypeError:                     # Pillow < 10.1
        return ImageFont.load_default()


def _cover(img, w: int, h: int):
    from PIL import Image
    img = img.convert("RGB")
    scale = max(w / img.width, h / img.height)
    nw, nh = max(w, round(img.width * scale)), max(h, round(img.height * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    x, y = (nw - w) // 2, (nh - h) // 2
    return img.crop((x, y, x + w, y + h))


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = _color(value, "#FFFFFF")
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    """Words onto at most two lines, as balanced as the width allows."""
    words = text.split()
    if len(words) <= 1:
        return [text]
    one = draw.textlength(text, font=font)
    if one <= max_w:
        return [text]
    best = None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        w = max(draw.textlength(a, font=font), draw.textlength(b, font=font))
        if best is None or w < best[0]:
            best = (w, [a, b])
    return best[1]


def compose(art_path, concept: dict, out_path) -> Path:
    """The final 1280x720 JPEG: the art covered to size, a soft dark
    gradient behind the words, the words big with a black outline (the
    second line in the accent colour). Under 2 MB."""
    from PIL import Image, ImageDraw
    img = _cover(Image.open(art_path), W, H)
    pos = concept.get("text_pos") or "left"
    text = (concept.get("text") or "").upper()
    if text:
        # gradient behind the text side
        horiz0 = pos in ("left", "right")
        n = W if horiz0 else H
        span = 0.55
        line = []
        for i in range(n):
            t = i / n
            t = t if pos in ("left", "top") else 1 - t
            line.append(int(150 * (1 - t / span)) if t < span else 0)
        strip = Image.new("L", (n, 1) if horiz0 else (1, n))
        strip.putdata(line)
        shade = strip.resize((W, H))
        img = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), img, shade)
        draw = ImageDraw.Draw(img)
        horiz = pos in ("left", "right")
        box_w = int(W * (0.46 if horiz else 0.88))
        box_h = int(H * (0.78 if horiz else 0.40))
        size = 230
        while size > 36:
            font = _font(size)
            lines = _wrap(draw, text, font, box_w)
            lw = max(draw.textlength(s, font=font) for s in lines)
            lh = size * 1.05 * len(lines)
            if lw <= box_w and lh <= box_h:
                break
            size -= 6
        stroke = max(4, size // 14)
        total_h = size * 1.05 * len(lines)
        if horiz:
            x0 = int(W * 0.04) if pos == "left" else int(W * 0.96 - box_w)
            y0 = (H - total_h) / 2
        else:
            x0 = (W - box_w) / 2
            y0 = H * 0.05 if pos == "top" else H * 0.95 - total_h
        main, accent = (_hex_rgb(concept.get("text_color")),
                        _hex_rgb(concept.get("accent")))
        for i, line in enumerate(lines):
            lw = draw.textlength(line, font=font)
            x = x0 + ((box_w - lw) / 2 if not horiz else
                      (0 if pos == "left" else box_w - lw))
            draw.text((x, y0 + i * size * 1.05), line, font=font,
                      fill=accent if (i == 1 and len(lines) > 1) else main,
                      stroke_width=stroke, stroke_fill=(0, 0, 0))
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    for quality in (92, 85, 78, 70, 60):
        img.save(out, "JPEG", quality=quality, optimize=True)
        if out.stat().st_size < MAX_BYTES:
            break
    return out


def compose_all(pdir) -> list[str]:
    """Re-compose every concept that has art. Returns the ids composed."""
    pdir = Path(pdir)
    data = load_thumbs(pdir)
    done = []
    for c in data["concepts"]:
        art = pdir / c["art_file"] if c.get("art_file") else None
        if not art or not art.is_file():
            c["final"] = ""
            continue
        out = thumbs_dir(pdir) / f"{c['id']}.jpg"
        compose(art, c, out)
        c["final"] = f"{THUMB_DIR}/{out.name}"
        done.append(c["id"])
    save_thumbs(pdir, data)
    contact_sheet(pdir)
    return done


def contact_sheet(pdir, title: str = "") -> Path | None:
    """How the thumbnails look in a phone feed: each at ~170 px wide, on a
    dark and a light background, side by side."""
    from PIL import Image, ImageDraw
    pdir = Path(pdir)
    data = load_thumbs(pdir)
    finals = [pdir / c["final"] for c in data["concepts"]
              if c.get("final") and (pdir / c["final"]).is_file()]
    if not finals:
        return None
    tw, th, gap = 170, 96, 12
    cols = min(len(finals), 5)
    sheet_w = cols * (tw + gap) + gap
    sheet = Image.new("RGB", (sheet_w, 2 * (th + 2 * gap)), (15, 15, 15))
    draw = ImageDraw.Draw(sheet)
    draw.rectangle((0, th + 2 * gap, sheet_w, 2 * (th + 2 * gap)),
                   fill=(250, 250, 250))
    for row in (0, 1):
        for i, f in enumerate(finals):
            im = Image.open(f).convert("RGB").resize((tw, th), Image.LANCZOS)
            sheet.paste(im, (gap + i * (tw + gap),
                             row * (th + 2 * gap) + gap))
    out = thumbs_dir(pdir) / "sheet.jpg"
    sheet.save(out, "JPEG", quality=90)
    return out


# ---- the user's own pictures --------------------------------------------------------

def set_art(pdir, concept_id: str, source) -> bool:
    """Use an existing image (an upload, or one of the production's own
    images) as the picture for a concept, and compose it."""
    pdir = Path(pdir)
    data = load_thumbs(pdir)
    c = next((c for c in data["concepts"] if c["id"] == concept_id), None)
    src = Path(source)
    if c is None or not src.is_file():
        return False
    dst = thumbs_dir(pdir) / "art" / f"{concept_id}{src.suffix.lower() or '.png'}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dst.resolve():
        shutil.copy(src, dst)
    c["art_file"] = f"{THUMB_DIR}/art/{dst.name}"
    save_thumbs(pdir, data)
    compose_all(pdir)
    return True


def production_images(pdir) -> list[str]:
    d = Path(pdir) / "images"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir()
                  if p.suffix.lower() in (".png", ".jpg", ".jpeg"))[:200]


# ---- inspiration: the thumbnails of the videos this one is based on -----------

INSPIRATION_DIR = "inspiration"


def inspiration_items(cfg, pid: int, limit: int = 6) -> list[dict]:
    """The source video first, then the niche's best outliers: id, title,
    multiplier, whether it is the source."""
    from . import db, outliers, packaging
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    out: list[dict] = []
    try:
        prod = db.get_production(conn, pid)
        src = prod["source_video_id"] if prod else None
        rows = conn.execute(
            "SELECT v.video_id, v.channel_id, c.name AS channel_name,"
            " c.genre AS genre, v.title, v.url, v.published_at,"
            " v.view_count, v.duration, v.status FROM videos v"
            " JOIN channels c ON c.channel_id = v.channel_id"
            " WHERE v.view_count IS NOT NULL").fetchall()
        items = outliers.build(rows)
        by_id = {i["video_id"]: i for i in items}
        if src and src in by_id:
            i = by_id[src]
            out.append({"id": src, "title": i["title"],
                        "multiplier": i["multiplier"], "source": True})
        elif src:
            row = db.get_video(conn, src)
            if row:
                out.append({"id": src, "title": row["title"],
                            "multiplier": None, "source": True})
        genre = (packaging.context(cfg, pid)["genre"]
                 if prod else "")
        shown, _n = outliers.filter_sort(items, min_multiplier=5.0,
                                         genre=genre, limit=limit + 1)
        if len(shown) < 3:              # a thin genre: look across the niche
            shown, _n = outliers.filter_sort(items, min_multiplier=5.0,
                                             limit=limit + 1)
        for i in shown:
            if i["video_id"] != src and len(out) < limit:
                out.append({"id": i["video_id"], "title": i["title"],
                            "multiplier": i["multiplier"], "source": False})
    finally:
        conn.close()
    return out


def fetch_inspiration(cfg, pid: int, log=print, opener=None) -> list[dict]:
    """Download the YouTube thumbnails of inspiration_items into
    thumbnails/inspiration/. `opener(url) -> bytes` is replaceable for tests.
    Items whose thumbnail could not be fetched are left out."""
    import urllib.request
    from . import studio

    def get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()

    opener = opener or get
    d = thumbs_dir(studio.prod_dir(cfg, pid)) / INSPIRATION_DIR
    d.mkdir(parents=True, exist_ok=True)
    got = []
    for it in inspiration_items(cfg, pid):
        target = d / f"{it['id']}.jpg"
        if not target.is_file():
            for name in ("maxresdefault", "hqdefault"):
                try:
                    data = opener(f"https://i.ytimg.com/vi/{it['id']}/{name}.jpg")
                except Exception:  # noqa: BLE001
                    continue
                if data and len(data) > 2000:
                    target.write_bytes(data)
                    break
        if target.is_file():
            got.append(it)
        else:
            log(f"thumbnails: could not fetch the thumbnail of {it['id']}")
    log(f"thumbnails: {len(got)} inspiration thumbnail(s) ready")
    return got


def inspiration_files(pdir) -> list[str]:
    d = thumbs_dir(pdir) / INSPIRATION_DIR
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("*.jpg"))
