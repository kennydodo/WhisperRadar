"""Render every demo scene to its own clip with the real Remotion renderer (no audio, default look).

    python docs/explainer/demo/rag-course-demo/render_demo.py [--scale 0.5] [--only cosine]

Needs Node.js and `npm ci` in tools/remotion once. Clips go to this folder's out/<scene>.mp4 (git-ignored).
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT))
from whisperradar import explainer_scenes as es, studio  # noqa: E402
from whisperradar.video.explainer import recipes, remotion_backend as rb  # noqa: E402

FPS = 30.0


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main():
    scale, only = arg("--scale", "1"), arg("--only")
    cues = es._cue_objs(studio.parse_srt_cues((HERE / "narration.srt").read_text(encoding="utf-8")))
    by_index = {c.index: c for c in cues}
    doc = recipes.build(json.loads((HERE / "plan.json").read_text(encoding="utf-8")), cues)
    out_dir = HERE / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    scenes = doc["scenes"]
    for i, s in enumerate(scenes):
        if only and s["id"] != only:
            continue
        start = by_index[s["cue_range"][0]].start_seconds
        end = by_index[scenes[i + 1]["cue_range"][0]].start_seconds if i + 1 < len(scenes) else cues[-1].end_seconds
        b = s["remotion"]
        props = {"component": b["component"], "params": b["params"], "fade": b.get("fade"),
                 "frames": int(round((end - start) * FPS)), "fps": FPS, "theme": {}}
        pf = out_dir / f"{s['id']}.props.json"
        pf.write_text(json.dumps(props), encoding="utf-8")
        clip = out_dir / f"{s['id']}.mp4"
        print(f"rendering {s['id']} ({props['frames']} frames) -> {clip}", flush=True)
        code = subprocess.call(rb.node_command() + ["--props", str(pf), "--out", str(clip), "--scale", scale])
        if code:
            sys.exit(code)
    print("done")


if __name__ == "__main__":
    main()
