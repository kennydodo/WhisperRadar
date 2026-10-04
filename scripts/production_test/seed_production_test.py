"""Seed a self-contained ~2-minute TEST production that renders with FlowBatch.

This writes DB rows + the production's files (script, subtitles, a locally
generated silent audio, a shotlist). It generates NO image and calls NO paid
API, so it is safe and free to run repeatedly:

  * engine = flowbatch      -> the whole batch goes through FlowBatch
                               (engines\\flowbatch). The Renderly API / Gemini
                               is never contacted, so no GEMINI_API_KEY is
                               needed.
  * motions = ST/ZI/ZO/PU/PD -> every motion EXCEPT the wide trio PL/PR/PV.
                               The channel uses the "flow_safe" planning
                               profile, which allows exactly this set.
  * audio   = ffmpeg silence -> ~120s, generated locally, no TTS/API.

The shotlist covers all 7 scene types (SCN/CU/INF/CMP/PROC/HYB/OVR) and all 5
Flow-safe motions, so one render run exercises the full routing: per-motion
aspect (PU/PD -> 1:1, everything else 16:9), FlowBatch job building, upscale,
adoption and merge.

The images stage CREATES a fresh Flow project for this production and the run
FAILS if FlowBatch would fall back to a previous project (no stored project
URL, FlowBatch --new-project). Re-seeding clears the stored project so the next
render creates another new one.

Usage:

    python scripts\\production_test\\seed_production_test.py --print-only
    python scripts\\production_test\\seed_production_test.py
    python scripts\\production_test\\seed_production_test.py --check

Then render and assemble (the images stage is the only live step):

    python scripts\\run_stage.py <pid> images     # FlowBatch, no Gemini
    python scripts\\run_stage.py <pid> merge

--check validates the FlowBatch job locally (motions + per-motion aspectRatio)
without launching a browser, so the routing can be confirmed for free.

Options:
    --title NAME     production title / channel name (default below); repeat
                     runs reuse the same row and refresh its folder
    --print-only     build everything in memory and report; write nothing
    --check          after writing, validate the FlowBatch job (no browser)
    --tone           use a 440 Hz tone instead of silence for the audio
    --no-audio       write no audio: leave the audio stage pending so it runs
                     the real TTS hook (OpenSpeaker/ai33) when you run it
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from whisperradar import db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402

DEFAULT_TITLE = "PROD TEST FlowBatch 2min"
CHANNEL_NAME = "PROD TEST FlowBatch"

# The channel's planning profile: exactly ST/ZI/ZO/PU/PD, no wide trio.
BRIEF_MOTION = "flow_safe"

# Motions this test may use. PL/PR/PV are deliberately excluded: on the
# Renderly engine those would reach the paid Gemini API.
WIDE_MOTIONS = {"PL", "PR", "PV"}
ALLOWED_MOTIONS = ("ST", "ZI", "ZO", "PU", "PD")

STYLE = ("Flat, evenly lit editorial illustration; centred subject; clean "
         "muted palette; no text, no captions, no watermark.")

# One entry per shot: (scene type, motion, hold seconds, narration sentences).
# Holds sum to 120s; the Flow-safe motion set is covered with no code above
# 40% and ST on two 3s holds only (the brief's own ST rule).
PLAN = [
    ("SCN", "ZI", 6.0, ["Most people assume real change demands one dramatic, life-rewriting decision."]),
    ("HYB", "ZI", 6.0, ["The research tells a quieter story: small actions repeated often outrun rare bursts of effort."]),
    ("INF", "ZI", 7.0, ["Think of a habit as compound interest for behaviour, earning a little with every repetition."]),
    ("SCN", "ZO", 6.0, ["Step back and the same pattern appears in fitness, language learning and saving money."]),
    ("CU", "ZO", 6.0, ["One page read tonight looks trivial; three hundred pages later the reader has changed."]),
    ("CMP", "PU", 6.0, ["Compare two people with the same goal.",
                        "One waits for motivation; the other moves ten minutes daily."]),
    ("PROC", "PD", 6.0, ["The process stays unglamorous: cue, action, reward, repeat, until it runs without negotiation."]),
    ("SCN", "ZI", 7.0, ["Your surroundings quietly decide which of those loops survive and which fade away."]),
    ("OVR", "ZO", 6.0, ["Zoom out and a life looks like a stack of small, ordinary choices."]),
    ("INF", "PD", 7.0, ["Track the streak and the running totals become a scoreboard you can influence."]),
    ("CU", "ST", 3.0, ["One clean frame of a notebook on a desk, nothing moving."]),
    ("HYB", "ZI", 6.0, ["Mark every completed day and the abstract slowly becomes concrete."]),
    ("SCN", "ZI", 6.0, ["Identity follows behaviour: you become the person who does the thing."]),
    ("CMP", "PU", 7.0, ["Missing once is an accident, a single bad night.",
                        "Missing twice begins a new and unwanted habit."]),
    ("PROC", "PD", 6.0, ["So the rule stays simple: never skip twice, and make the next repetition easy."]),
    ("OVR", "ZO", 6.0, ["A year of small moves looks less like luck and more like construction."]),
    ("SCN", "ZI", 7.0, ["The compound effect hides for weeks and gives no feedback.",
                        "Then one ordinary morning the results simply appear."]),
    ("INF", "ZO", 6.0, ["Effort and outcome rarely rise in a straight line; they bend upward late."]),
    ("CU", "ST", 3.0, ["Rest on the quiet moment before the change becomes obvious."]),
    ("OVR", "ZO", 7.0, ["Start smaller than feels impressive, repeat it tomorrow, and let arithmetic do the rest."]),
]

TYPE_SCENE = {
    "SCN": "a wide establishing scene of a person in a calm modern interior",
    "CU": "a tight close-up detail on a desk",
    "INF": "a clean infographic-style diagram of simple shapes and arrows",
    "CMP": "a two-panel side-by-side comparison",
    "PROC": "a vertical stack of three process stages",
    "HYB": "a scene with simple explanatory graphics overlaid",
    "OVR": "a broad conceptual overview of one idea",
}
MOTION_NOTE = {
    "ZI": "compose centred with generous overscan for a slow push in",
    "ZO": "compose centred with breathing room for a slow pull out",
    "PU": "compose for a vertical tilt upward, with room below",
    "PD": "compose for a vertical tilt downward, with room above",
    "ST": "compose a complete static frame, no overscan, nothing moving",
}


def _fmt(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _build_plan(plan):
    """Turn PLAN into (shotlist, srt_text, script_text) with contiguous cues."""
    images, shots, cue_blocks = [], [], []
    cue_no = 1
    clock = 0.0
    for i, (type_, motion, hold, sentences) in enumerate(plan, 1):
        name = f"S01_{i:02d}_{type_}_{motion}.png"
        prompt = (f"TEST shot {i:02d} ({type_}/{motion}): {TYPE_SCENE[type_]}; "
                  f"{MOTION_NOTE[motion]}. {STYLE}")
        images.append({"file": name, "prompt": prompt})

        first = cue_no
        each = hold / len(sentences)
        for text in sentences:
            cue_blocks.append((cue_no, clock, clock + each, text))
            clock += each
            cue_no += 1
        last = cue_no - 1
        shots.append({
            "shot_id": f"SH{i:03d}",
            "cues": f"{first}-{last}" if last > first else f"{first}",
            "asset": name,
            "motion": motion,
        })

    srt = "\n".join(
        f"{n}\n{_fmt(a)} --> {_fmt(b)}\n{text}\n"
        for n, a, b, text in cue_blocks)
    script = " ".join(s for _, _, _, s in cue_blocks) + "\n"
    shotlist = {"style": STYLE, "shots": shots, "images": images}
    return shotlist, srt, script


def _find_production(conn, title):
    for row in db.list_productions(conn):
        if row["title"] == title:
            return row["id"]
    return None


def _ensure_channel(conn, name) -> int:
    existing = db.get_own_channel(conn, name)
    fields = {
        "default_engine": "flowbatch",
        "default_render_mode": "flow",
        "default_upscale": 2,
        "brief_motion": BRIEF_MOTION,
        "generate_references": 0,
    }
    if existing is not None:
        db.update_own_channel(conn, existing["id"], **fields)
        return existing["id"]
    return db.create_own_channel(conn, name, **fields)


def _make_audio(pdir: Path, seconds: float, tone: bool) -> Path | None:
    exe = shutil.which("ffmpeg")
    if not exe:
        print("  audio: ffmpeg not found on PATH - skipping (use --no-audio "
              "to silence this)")
        return None
    source = "sine=frequency=440:sample_rate=22050" if tone \
        else "anullsrc=r=22050:cl=mono"
    out = pdir / "audio.wav"
    cmd = [exe, "-y", "-f", "lavfi", "-i", source, "-t", f"{seconds:.3f}",
           "-c:a", "pcm_s16le", str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not out.exists():
        print(f"  audio: ffmpeg failed - {result.stderr.strip()[-200:]}")
        return None
    return out


def _validate_job(cfg, pdir: Path, pid: int) -> None:
    """Build the FlowBatch job locally and check motions / aspects. No browser."""
    job_path, expected = studio.prepare_flowbatch_job(cfg, pdir, pid)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    default_aspect = job.get("defaults", {}).get("aspectRatio", "16:9")
    print(f"\n--check: FlowBatch job {job_path.name} "
          f"({len(job['images'])} items, default {default_aspect})")
    bad = []
    for item in job["images"]:
        motion = Path(item["file"]).stem.rsplit("_", 1)[-1]
        aspect = item.get("aspectRatio", default_aspect)
        want = {"PU": "1:1", "PD": "1:1"}.get(motion, "16:9")
        ok = (aspect == want) and motion not in WIDE_MOTIONS
        if not ok:
            bad.append((item["file"], motion, aspect, want))
        print(f"   {item['file']:26s} {motion:>2s} -> {aspect:<4s} "
              f"{'ok' if ok else 'MISMATCH'}")
    if bad:
        raise SystemExit(f"--check FAILED: {bad}")
    print(f"--check OK: {len(expected)} items, no PL/PR/PV, aspects as expected")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--title", default=DEFAULT_TITLE)
    ap.add_argument("--print-only", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--tone", action="store_true")
    ap.add_argument("--no-audio", action="store_true")
    args = ap.parse_args()

    motions = [m for _, m, _, _ in PLAN]
    wide = sorted(set(motions) & WIDE_MOTIONS)
    if wide:
        raise SystemExit(f"PL/PR/PV must not appear in this test: {wide}")
    if not set(motions) <= set(ALLOWED_MOTIONS):
        raise SystemExit(f"unexpected motion in PLAN: {sorted(set(motions))}")

    shotlist, srt, script = _build_plan(PLAN)
    total_s = sum(h for _, _, h, _ in PLAN)
    print(f"{args.title}: {len(PLAN)} shots, {len(shotlist['images'])} images, "
          f"{total_s:.0f}s ({total_s / 60:.1f} min)")
    print(f"  motions: {', '.join(sorted(set(motions), key=motions.index))} "
          f"(no PL/PR/PV)")
    print(f"  cues:    {srt.count(' --> ')}")

    if args.print_only:
        print("\n--- shotlist.json (head) ---")
        print(json.dumps(shotlist, indent=2)[:900] + "\n...")
        print("(print-only: nothing written)")
        return 0

    cfg = load_config(ROOT / "config.yaml")
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    try:
        oc = _ensure_channel(conn, CHANNEL_NAME)
        pid = _find_production(conn, args.title)
        if pid is None:
            pid = db.create_production(conn, args.title, "general")
        db.update_production(conn, pid, own_channel_id=oc, autorun=0,
                             stage="images", render_mode="flow",
                             flow_project_url=None, flow_project_id=None,
                             warning=None)

        pdir = studio.prod_dir(cfg, pid)
        for child in list(pdir.iterdir()):
            if child.name == studio.MARKER:
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        for sub in ("images", "refs", "out", "audio"):
            (pdir / sub).mkdir(exist_ok=True)

        (pdir / "style.md").write_text(STYLE + "\n", encoding="utf-8")
        (pdir / "script.md").write_text(script, encoding="utf-8")
        (pdir / "subtitles.srt").write_text(srt, encoding="utf-8")
        (pdir / "shotlist.json").write_text(
            json.dumps(shotlist, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")

        audio = None
        if not args.no_audio:
            audio = _make_audio(pdir, total_s, args.tone)

        # Mark the pre-fabricated stages done so "Run till finish" resumes at
        # images instead of trying to call an LLM. The audio stage is only
        # marked done when a local placeholder was written; with --no-audio it
        # stays pending so it runs the real TTS hook (OpenSpeaker/ai33).
        seeded = [("style", "style.md"), ("script", "script.md"),
                  ("srt", "subtitles.srt"), ("shots", "shotlist.json")]
        if audio:
            seeded.append(("audio", audio.name))
        for stage, artifact in seeded:
            db.add_step(conn, pid, stage, "test", artifact=artifact,
                        detail="seeded by scripts\\production_test")
    finally:
        conn.close()

    print(f"\npid {pid}: {args.title}")
    print(f"  folder: {pdir}")
    print(f"  engine: flowbatch (no Renderly/Gemini API)")
    if audio:
        print(f"  audio:  {audio.name} (silent placeholder)")
    else:
        from whisperradar import ai33 as _ai33
        print("  audio:  none - the audio stage will run the TTS hook"
              + (" (OpenSpeaker/ai33 key found)" if _ai33.api_key(cfg)
                 else " (no OpenSpeaker/ai33 key configured)"))
    print(f"\nRender with: python scripts\\run_stage.py {pid} images")
    if not audio:
        print(f"Audio with:  python scripts\\run_stage.py {pid} audio")
    if args.check:
        _validate_job(cfg, pdir, pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
