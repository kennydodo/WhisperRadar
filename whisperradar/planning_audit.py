"""Local evidence for beats runs; no model, browser or paid service calls."""
from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher

from . import studio
from .video import auto_values


def words(text):
    return re.findall(r"[a-z0-9]+", text.lower().replace("’", "'"))


def fingerprint(text):
    return hashlib.sha256(" ".join(words(text)).encode()).hexdigest()


def source_report(pdir, narration):
    script_path = pdir / "script.md"
    script = script_path.read_text(encoding="utf-8-sig") if script_path.exists() else ""
    ratio = SequenceMatcher(None, words(script), words(narration), autojunk=False).ratio() if script else None
    warnings = []
    if ratio is not None and ratio < .9:
        warnings.append(f"Script/subtitles content mismatch ({ratio:.0%} similarity). "
                        "Planning uses the recorded subtitle narration. Align the audio "
                        "again if the newer script is the intended version.")
    elif script:
        # A changed amount or percent can matter even when almost every word agrees.
        def amounts(text):
            return sorted(re.sub(r"\s+", "", v).lower() for v in auto_values.find_values(text)
                          if re.search(r"[$€£%]|percent", v, re.I))
        if amounts(script) != amounts(narration):
            warnings.append("Script/subtitles numerical mismatch: amounts or percentages differ. "
                            "Review the recorded narration before trusting on-screen values.")
    provenance = pdir / "elements_source.json"
    if (pdir / "elements.md").exists():
        try:
            old = json.loads(provenance.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        if not old:
            warnings.append("Elements source is unverified; regenerate elements to tie them to this narration.")
        elif old.get("fingerprint") != fingerprint(narration):
            warnings.append("Elements were made from different narration; regenerate them before planning images.")
    return {"similarity": ratio, "narration_fingerprint": fingerprint(narration), "warnings": warnings}


def pacing_report(data, cues, profile):
    by = {int(c["index"]): c for c in cues}
    holds = []
    for shot in data.get("shots", []):
        rng = studio.cue_range(shot.get("cues"))
        if rng and rng[0] in by and rng[1] in by:
            holds.append(studio._srt_seconds(by[rng[1]]["end"]) - studio._srt_seconds(by[rng[0]]["start"]))
    short = sum(h < 3 for h in holds)
    warnings = []
    if holds and short / len(holds) > .25:
        warnings.append(f"Pacing: {short}/{len(holds)} shots are under 3s. "
                        + (f"The channel minimum is {profile.min_hold:g}s; its existing share check applies."
                           if profile.min_hold else "The channel has no minimum hold configured; review grouping before rendering."))
    return {"shots": len(holds), "under_3s": short, "minimum_hold": profile.min_hold,
            "average_hold": sum(holds) / len(holds) if holds else 0, "warnings": warnings}


def number_mode(cfg, ctx):
    enabled = bool(ctx.get("eff", {}).get("brief_text_overlays"))
    auto = enabled
    if cfg is not None:
        from . import db
        from .video import settings_spec
        conn = db.connect(cfg.db_path)
        try:
            values = settings_spec.effective(conn, ctx["prod"]["own_channel_id"])
            auto = values.get("text.auto_values", auto)
        finally:
            conn.close()
        # Existing project options can be manual overrides; never turn one on here.
        try:
            project = json.loads((ctx["pdir"] / "imgtovideo.json").read_text(encoding="utf-8-sig"))
            auto = project.get("text", {}).get("auto_values", auto)
        except (OSError, ValueError):
            pass
        if getattr(cfg, "studio_video_engine", "python") != "python":
            auto = False
    return bool(auto), enabled


def numerical_report(data, cues, *, auto=False, overlays_enabled=False):
    """Verify render-time auto values as well as explicit shot text. Do not demand duplicates."""
    by = {int(c["index"]): c for c in cues}
    images = {i["file"]: i for i in data.get("images", [])}
    prepared = auto_values.prepare_cues([
        (studio._srt_seconds(c["start"]), studio._srt_seconds(c["end"]), c["text"]) for c in cues])
    normalized = {int(c["index"]): dict(c, text=t[2]) for c, t in zip(cues, prepared)}
    entries, faults = [], []
    def key(value):
        return re.sub(r"\s+", "", value).lower()
    for n, shot in enumerate(data.get("shots", []), 1):
        rng = studio.cue_range(shot.get("cues"))
        if not rng or rng[0] not in by or rng[1] not in by:
            continue
        local = [normalized[i] for i in range(rng[0], rng[1] + 1) if i in normalized]
        expected = list(dict.fromkeys(v for c in local for v in auto_values.find_values(c["text"])))
        explicit = "\n".join(str(o.get("text", "")) for o in shot.get("overlays", []) if isinstance(o, dict))
        generated = []
        if auto:
            generated = auto_values.build(
                [(studio._srt_seconds(c["start"]), studio._srt_seconds(c["end"]), c["text"]) for c in local],
                studio._srt_seconds(local[0]["start"]), studio._srt_seconds(local[-1]["end"]))
        actual = explicit + "\n" + "\n".join(o["text"] for o in generated)
        if not overlays_enabled:
            actual += "\n" + str(images.get(shot.get("asset"), {}).get("prompt", ""))
        missing = [v for v in expected if key(v) not in key(actual)]
        entries.append({"beat": n, "asset": shot.get("asset"), "expected": expected,
                        "automatic": [o["text"] for o in generated], "missing": missing})
        # Renderer text is an explicit channel choice; other policies are prose,
        # so keep image-only omissions advisory rather than guessing permission.
        if missing and overlays_enabled:
            faults.append(f"beat {n}: numerical coverage missing {', '.join(missing)}. "
                          "Add these exact narration values to overlays with their meaning; "
                          "keep numerals out of the generated image.")
    return {"automatic_values": auto, "overlays_enabled": overlays_enabled,
            "entries": entries, "faults": faults}
