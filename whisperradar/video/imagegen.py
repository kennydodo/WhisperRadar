"""Port of ImgToVideo.ImageGen (Renderly mode): turns shotlist.json into images\\ through a
running Renderly backend (generation, optional GPU upscale, download).

Run it as `python -m whisperradar.video.imagegen <projectFolder> --renderly <url> --channel <id>
[--image-size 1K|2K|4K] [--upscale 2|4] [--ref-asset 1,2] [--parallel 1-8] [--aspect 16:9]
[--motion-filter PL,PR] [--force] [--dry-run]``. Same output lines and exit codes as the C#
tool: 0 all done, 1 some images failed, 2 usage/other error, 3 stopped early on a Renderly
quota/billing limit (the caller then falls back to a free generator).

The direct Gemini mode of the C# tool is not ported: WhisperRadar never used it.
"""
from __future__ import annotations

import html
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from . import models as m
from . import shotlist

HTTP_TIMEOUT = 15 * 60


class ImageGenerationError(Exception):
    pass


def size_text(n_bytes: int) -> str:
    """ImageFile.SizeText: "123 KB" (.NET rounds half away from zero)."""
    return f"{int(n_bytes / 1024.0 + 0.5)} KB"


def motion_suffix(file: str) -> str:
    stem = os.path.splitext(os.path.basename(file))[0]
    idx = stem.rfind("_")
    return (stem[idx + 1:] if idx >= 0 else stem).upper()


def is_quota_or_billing_error(message: str) -> bool:
    low = message.lower()
    return "quota" in low or "billing" in low


# ---- Renderly client ----------------------------------------------------------------
@dataclass
class Generation:
    id: int
    name: str
    status: str
    image_url: Optional[str]
    error: Optional[str]


class RenderlyClient:
    def __init__(self, base_url: str, timeout: float = HTTP_TIMEOUT):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}/api/{path}", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            raise ImageGenerationError(f"Renderly HTTP {exc.code}: {_truncate(text)}") from None
        except urllib.error.URLError as exc:
            raise ImageGenerationError(f"Renderly unreachable: {exc.reason}") from None

    @staticmethod
    def _parse(doc: dict) -> Generation:
        def s(key):
            v = doc.get(key)
            return v if isinstance(v, str) else None
        return Generation(int(doc["id"]), s("name") or "", s("status") or "", s("image_url"),
                          s("error"))

    def generate(self, channel_id: int, prompt: str, name: str, image_size: str, aspect: str,
                 ref_asset_ids: Sequence[int]) -> Generation:
        return self._parse(self._post(f"channels/{channel_id}/generate", {
            "prompt": prompt, "name": name, "image_size": image_size, "aspect_ratio": aspect,
            "ref_strength": "balanced", "asset_ids": list(ref_asset_ids)}))

    def upscale(self, generation_id: int, scale: int) -> Generation:
        return self._parse(self._post(f"generations/{generation_id}/upscale", {"scale": scale}))

    def download(self, generation: Generation) -> bytes:
        if not generation.image_url:
            raise ImageGenerationError("generation has no image url")
        try:
            with urllib.request.urlopen(f"{self.base}{generation.image_url}",
                                        timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.URLError as exc:
            raise ImageGenerationError(f"download failed: {exc}") from None


def _truncate(text: str) -> str:
    flat = " ".join(text.splitlines())
    return flat if len(flat) <= 300 else flat[:300] + "…"


# ---- review sheet ---------------------------------------------------------------------
GENERATED, EXISTING, FAILED = "generated", "existing", "failed"

_SHEET = """<!doctype html>
<html><head><meta charset="utf-8"/><title>Image review — @TITLE@</title>
<style>
  body { font-family: 'Segoe UI', sans-serif; background: #14161a; color: #d8dade; margin: 0; padding: 16px 20px; }
  h1 { font-size: 18px; font-weight: 600; }
  .summary { color: #8b909a; font-size: 12.5px; margin-bottom: 14px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 12px; }
  .card { background: #1e2127; border: 1px solid #33363d; border-radius: 6px; overflow: hidden; }
  .card img { width: 100%; display: block; background: #000; }
  .missing { height: 120px; display: flex; align-items: center; justify-content: center; color: #f27979; font-size: 13px; background: #241c1e; }
  .info { padding: 8px 10px 10px; }
  .file { font-size: 12.5px; font-weight: 600; word-break: break-all; }
  .prompt { font-size: 11.5px; color: #9aa0aa; margin-top: 4px; line-height: 1.45; }
  .detail { font-size: 11.5px; color: #c9a0a0; margin-top: 4px; }
  .ok { color: #7ecb9c; }
  .skip { color: #8b909a; }
  .fail { color: #f27979; }
</style></head><body>
<h1>Image review — @TITLE@</h1>
<div class="summary">@SUMMARY@</div>
<div class="grid">@CARDS@</div>
</body></html>"""


def write_contact_sheet(path: str, title: str, items: Sequence[dict]) -> None:
    gen = sum(1 for i in items if i["status"] == GENERATED)
    ex = sum(1 for i in items if i["status"] == EXISTING)
    fail = sum(1 for i in items if i["status"] == FAILED)
    cards = []
    for it in items:
        badge, cls = {GENERATED: ("generated", "ok"),
                      EXISTING: ("already on disk", "skip")}.get(it["status"], ("FAILED", "fail"))
        h = html.escape
        card = '<div class="card">'
        if it["status"] != FAILED:
            card += (f'<img src="../images/{h(it["file"])}" loading="lazy" '
                     f'alt="{h(it["file"])}"/>')
        else:
            card += '<div class="missing">generation failed</div>'
        card += (f'<div class="info"><div class="file"><span class="{cls}">[{badge}]</span> '
                 f'{h(it["file"])}</div><div class="prompt">{h(it["prompt"])}</div>')
        if it["detail"]:
            card += f'<div class="detail">{h(it["detail"])}</div>'
        cards.append(card + "</div></div>")
    page = (_SHEET.replace("@TITLE@", html.escape(title))
            .replace("@SUMMARY@", f"{gen} generated · {ex} already on disk · {fail} failed — "
                     "failed files can be regenerated by running the tool again "
                     "(existing files are skipped).")
            .replace("@CARDS@", "".join(cards)))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)


# ---- the run --------------------------------------------------------------------------
def load_per_file_aspect(project_folder: str, say: Callable[[str], None]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    path = os.path.join(project_folder, "out", "image-batch.json")
    if not os.path.isfile(path):
        return result
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        for entry in doc.get("images") or []:
            f, a = entry.get("file"), entry.get("aspect")
            if isinstance(f, str) and f and isinstance(a, str) and a:
                result[f.upper()] = a
    except (ValueError, AttributeError) as exc:
        say(f"[warn] could not read out\\image-batch.json for per-shot aspect ratios: {exc}")
    return result


def run(project_folder: str, client: RenderlyClient, channel_id: int, image_size: str = "1K",
        upscale: Optional[int] = None, ref_asset_ids: Sequence[int] = (), parallel: int = 2,
        aspect: Optional[str] = None, motion_filter: Optional[set] = None, force: bool = False,
        dry_run: bool = False, say: Callable[[str], None] = print) -> int:
    folder = os.path.abspath(project_folder)
    shotlist_path = os.path.join(folder, shotlist.FILE_NAME)
    if not os.path.isfile(shotlist_path):
        raise FileNotFoundError(
            f"no {shotlist.FILE_NAME} in {folder} — run the LLM planning step first.")
    issues: list = []
    with open(shotlist_path, encoding="utf-8-sig") as fh:
        document = shotlist.parse(fh.read(), issues)
    for i in issues:
        if i.severity != m.INFO:
            say(f"[warn] {i.message}")

    images_dir = os.path.join(folder, "images")
    os.makedirs(images_dir, exist_ok=True)

    planned = sorted(((f, document.prompts[f]) for f in document.prompts),
                     key=lambda fp: fp[0].upper())
    say(f"shotlist: {len(document.shots)} shots, {len(planned)} planned images"
        + (" + master style prompt" if document.style is not None else ""))
    if motion_filter:
        say(f"motion filter: only [{', '.join(sorted(motion_filter))}] this run")

    review: List[dict] = []
    to_generate: List[tuple] = []
    skipped = filtered_out = 0
    for file, prompt in planned:
        if motion_filter and motion_suffix(file) not in motion_filter:
            filtered_out += 1
            continue
        if file != os.path.basename(file) or ":" in file:
            review.append({"file": file, "prompt": prompt, "status": FAILED,
                           "detail": "filename must be a plain file name (no folders)"})
            say(f"[fail] {file} — filename must be a plain file name")
            continue
        if not prompt:
            review.append({"file": file, "prompt": prompt, "status": FAILED,
                           "detail": "no prompt in shotlist.json"})
            say(f"[fail] {file} — no prompt in shotlist.json")
            continue
        if not force and os.path.exists(os.path.join(images_dir, file)):
            skipped += 1
            review.append({"file": file, "prompt": prompt, "status": EXISTING,
                           "detail": "already on disk"})
            say(f"[skip] {file} (exists)")
            continue
        to_generate.append((file, prompt))
        review.append({"file": file, "prompt": prompt, "status": GENERATED, "detail": ""})

    def sheet():
        out_dir = os.path.join(folder, "out")
        os.makedirs(out_dir, exist_ok=True)
        write_contact_sheet(os.path.join(out_dir, "image-review.html"),
                            os.path.basename(folder.rstrip("\\/")), review)

    suffix = f", {filtered_out} filtered out by --motion-filter." if filtered_out else "."
    if dry_run:
        say(f"dry run: {len(to_generate)} would be generated, {skipped} already exist"
            + (suffix if filtered_out else "."))
        for file, prompt in to_generate:
            say(f"[gen ] {file} ({len(prompt)} chars)")
        return 0
    if not to_generate:
        say("nothing to generate — every image matching --motion-filter is already on disk."
            if filtered_out else "nothing to generate — every planned image is already on disk.")
        sheet()
        return 0

    per_file_aspect = load_per_file_aspect(folder, say)
    if per_file_aspect:
        say(f"per-shot aspect ratios: {len(per_file_aspect)} loaded from out\\image-batch.json")

    lock = threading.Lock()
    state = {"generated": 0, "failed": 0, "quota": False, "quota_skipped": 0}
    index_of = {f: n for n, (f, _) in enumerate(to_generate)}
    review_by_file = {r["file"]: r for r in review}
    started = time.time()

    def work(item):
        file, prompt = item
        rec = review_by_file[file]
        if state["quota"]:
            with lock:
                state["quota_skipped"] += 1
                rec.update(status=FAILED, detail="skipped — Renderly quota/billing limit "
                                                 "reached earlier in this batch")
            return
        try:
            full = prompt if document.style is None else document.style + "\n\n" + prompt
            asp = per_file_aspect.get(file.upper(), aspect)
            name = os.path.splitext(file)[0]
            gen = client.generate(channel_id, full, name, image_size, asp or "16:9",
                                  ref_asset_ids)
            if gen.status != "done" or gen.image_url is None:
                raise ImageGenerationError(gen.error or f"generation status {gen.status}")
            if upscale:
                gen = client.upscale(gen.id, upscale)
                if gen.status != "done" or gen.image_url is None:
                    raise ImageGenerationError(gen.error or f"upscale status {gen.status}")
            data = client.download(gen)
            with open(os.path.join(images_dir, file), "wb") as fh:
                fh.write(data)
            with lock:
                state["generated"] += 1
                rec.update(status=GENERATED, detail=size_text(len(data)))
            say(f"[ok]   {file} ({size_text(len(data))})")
        except Exception as exc:  # noqa: BLE001 - one image failing never stops the batch
            msg = str(exc)
            with lock:
                state["failed"] += 1
                rec.update(status=FAILED, detail=msg)
                if is_quota_or_billing_error(msg):
                    state["quota"] = True
            say(f"[fail] {file} — {msg}")

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        list(pool.map(work, to_generate))

    elapsed = (time.time() - started) / 60.0
    say(f"done in {elapsed:.1f} min: {state['generated']} generated, {skipped} skipped, "
        f"{state['failed']} failed"
        + (f", {state['quota_skipped']} skipped (quota/billing)" if state["quota_skipped"] else "")
        + (f", {filtered_out} filtered out by --motion-filter." if filtered_out else "."))
    sheet()
    say(f"review sheet: {os.path.join(folder, 'out', 'image-review.html')}")
    if state["quota"]:
        say("quota/billing limit reached — stopped early so the caller can fall back to a "
            "free generator.")
        return 3
    return 1 if state["failed"] > 0 else 0


# ---- command line -----------------------------------------------------------------------
USAGE = """ImgToVideo batch image generator (Python port, Renderly mode)
usage: python -m whisperradar.video.imagegen <projectFolder> --renderly <url> --channel <id> [options]
  --image-size <1K|2K|4K>  --upscale <2|4>  --ref-asset <ids>  --parallel <1-8>
  --aspect <ratio>  --motion-filter <codes>  --force  --dry-run
Exit codes: 0 ok, 1 some images failed, 2 usage/other error, 3 Renderly quota/billing limit."""


def _parse_args(argv: Sequence[str]) -> dict:
    o = {"folder": None, "force": False, "dry_run": False, "parallel": 2, "aspect": None,
         "renderly": None, "channel": None, "image_size": "1K", "upscale": None,
         "refs": [], "motion_filter": None}
    i = 0
    argv = list(argv)
    while i < len(argv):
        a = argv[i]
        if a == "--force":
            o["force"] = True
        elif a == "--dry-run":
            o["dry_run"] = True
        elif a in ("--parallel", "--aspect", "--renderly", "--channel", "--image-size",
                   "--upscale", "--ref-asset", "--motion-filter"):
            if i + 1 >= len(argv):
                raise ValueError(f"{a} needs a value.")
            v = argv[i + 1]
            i += 1
            if a == "--parallel":
                if not v.isdigit() or not 1 <= int(v) <= 8:
                    raise ValueError("--parallel must be between 1 and 8.")
                o["parallel"] = int(v)
            elif a == "--aspect":
                o["aspect"] = v
            elif a == "--renderly":
                o["renderly"] = v.rstrip("/")
            elif a == "--channel":
                if not v.isdigit() or int(v) < 1:
                    raise ValueError("--channel must be a positive id.")
                o["channel"] = int(v)
            elif a == "--image-size":
                if v.upper() not in ("1K", "2K", "4K"):
                    raise ValueError("--image-size must be 1K, 2K or 4K.")
                o["image_size"] = v.upper()
            elif a == "--upscale":
                if v not in ("2", "4"):
                    raise ValueError("--upscale must be 2 or 4.")
                o["upscale"] = int(v)
            elif a == "--ref-asset":
                for tok in v.split(","):
                    if not tok.strip().isdigit() or int(tok) < 1:
                        raise ValueError(f'--ref-asset must be ids like "12,13" (got "{tok}").')
                    o["refs"].append(int(tok))
            else:
                codes = {t.strip().upper() for t in v.split(",") if t.strip()}
                if not codes:
                    raise ValueError('--motion-filter needs codes like "PL,PR".')
                o["motion_filter"] = codes
        elif a.startswith("-"):
            raise ValueError(f'unknown option "{a}".')
        else:
            if o["folder"] is not None:
                raise ValueError("only one project folder can be given.")
            o["folder"] = a
        i += 1
    if o["folder"] is None:
        raise ValueError("a project folder is required.")
    if o["renderly"] is None:
        raise ValueError("only Renderly mode is ported - give --renderly <url> --channel <id>.")
    if o["channel"] is None:
        raise ValueError("--renderly needs --channel <id> (see RENDERLY).")
    folder = os.path.abspath(o["folder"])
    if not os.path.isdir(folder):
        raise ValueError(f"project folder does not exist: {folder}")
    o["folder"] = folder
    return o


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    def say(line: str) -> None:
        print(line, flush=True)

    try:
        o = _parse_args(argv)
    except ValueError as exc:
        print(USAGE)
        print("error: " + str(exc), file=sys.stderr)
        return 2
    try:
        return run(o["folder"], RenderlyClient(o["renderly"]), o["channel"], o["image_size"],
                   o["upscale"], o["refs"], o["parallel"], o["aspect"], o["motion_filter"],
                   o["force"], o["dry_run"], say)
    except Exception as exc:  # noqa: BLE001
        print("error: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
