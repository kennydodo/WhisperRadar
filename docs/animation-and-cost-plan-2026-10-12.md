# Animation plan: moving the images, on a budget (written 2026-10-12)

Constraint: GTX 1060 (6 GB, Pascal) until a new PC. Goal: more than camera moves, as cheaply as possible.
Today ImgToVideo only moves the CAMERA over a still (ZI/ZO/PL/PR/PU/PD/PV). Nothing inside the picture moves.

## What the 1060 can and cannot do
- CAN: ffmpeg (NVENC H.264 encode works on Pascal), small depth models (MiDaS small / Depth Anything small), background removal/segmentation (rembg/u2net), per-frame 2D transforms in Python/numpy/OpenCV, particle/overlay effects.
- CANNOT (practically): modern image-to-video models (Wan, LTX, Hunyuan). Community 6 GB workflows exist, but Pascal has no fast FP8/BF16, so expect very slow runs (unverified: assume long per clip) and unstable results. Not worth it. A recent community guide also shows even still-image models take 2-4 min/image on this card.
- So: do deterministic/2D animation locally, rent the AI motion for a few clips.

## Recommended plan (cheapest first)
1. **Ambient motion layers (local, ~free).** Drifting clouds/dust/rain/steam/fireflies/light flicker/leaf sway as overlays rendered with ffmpeg/OpenCV. Deterministic, same philosophy as ImgToVideo. Shotlist gets an optional `ambient` field per shot (e.g. "dust", "steam", "clouds"). Works for all four channel styles. Build first: lowest effort, lowest risk.
2. **Layered cutout animation (local, free per clip).** Split a shot into background + character/animal layers (segment with rembg/SAM-small, or have Flow generate a clean background plate + transparent character). Animate with transforms: blink, mouth flap, bob/sway, simple hop/walk, parallax between layers. Best fit for flat vector (Nature Made Us) and anime-pastel (To Live and More). New stage + shotlist field `animate: {layer, motion}`.
3. **Depth parallax (local, optional).** Depth model -> shift foreground more than background during the camera move. Makes camera moves richer; poorer on flat vector art, so lower priority than #2.
4. **Paid AI image-to-video for hero shots only.** About 10-15% of shots (hook, key reveal). Example: 10-min video, ~120 shots, ~18 clips x 5 s = 90 s of AI video. At $0.04-0.10 per second that is roughly $3.50-$9 per video. Shotlist field `ai_motion: "<motion prompt>"` written in Runway's pattern (motion only, positive wording, "locked camera" when still, one scene per clip, clean source image with no motion-blur/speed lines).
5. **Loop trick for ambient scenes** (To Live and More): prompt "camera starts and ends on the exact same frame" for seamless loops, reuse the clip.

## Cheaper than Runway: paid image-to-video options
Prices are list API prices per second of output, from public comparison pages dated Aug-Sep 2026; they move often, so re-check before committing.
| Model | ~$/s | Note |
|---|---|---|
| Hailuo 2.3 (via PiAPI) | 0.038 (720p) | cheapest listed |
| LTX-2 (WaveSpeedAI) | 0.04 (1080p fast) | open weights |
| HunyuanVideo 1.5 (WaveSpeedAI) | 0.04 (720p) | |
| Veo 3.1 Lite | 0.05 (720p) | budget draft tier |
| PixVerse V6 (fal.ai) | 0.045 (720p) | |
| Luma Ray 3.2 | 0.06 (720p) | |
| Wan 3.0 | ~0.083 | first/last frame |
| Kling 3.0 | ~0.084 silent | strong motion; test that it keeps the first frame intact |
| Veo 3.1 Fast | 0.10 | |
| Runway Gen-4.5 | 0.12 | the official guide we read; not the cheapest |
Other ways to spend less: test on the cheapest tier then re-run only winners; 5 s clips unless the move needs 10 s; 720p is enough because ImgToVideo upscale/crop happens after; reuse clips (loops); cap AI clips per video in settings; check Flow's own image-to-video (we already automate Flow) and any free/trial credits before paying elsewhere (unverified: not checked yet).

## Beyond animation: other low-cost ways to stand out
- Original-synthesis scripts from the transcript corpus (see ideas-standing-out-2026-10-12.md) cost only LLM tokens.
- Sound design (ambience + a few SFX; ImgToVideo already has a SoundEffectLibrary) is cheap and makes the same images feel richer.
- Animated charts/numbers for finance and health: render with Python (matplotlib/ffmpeg), free, and more convincing than AI images.
- Kinetic captions and chapter cards generated locally.
- Keep images good: fix source images before animating (blurry hands/faces get worse).

## Suggested build order
1. Ambient layers + `ambient` shotlist field (spike on one Nature Made Us shot).
2. Layered cutout animation spike on the same shot (decide by how it looks).
3. `ai_motion` field + one provider adapter (cheapest acceptable model), capped per video.
4. Only then: depth parallax, if wanted.

## Sources
- Runway guides: help.runwayml.com Image to Video Prompting Guide; Gen-4 Video Prompting Guide.
- Pricing: genrates.com (as of Aug 1 2026); teamday.ai "Best AI video models" (checked Sep 23 2026).
- 1060 limits: discuss.huggingface.co "Running Modern AI Image Models on a GTX 1060 6GB".

## Update: laptop RTX 3050 with 6 GB VRAM
Verdict: local AI image-to-video is still NOT worth it on 6 GB.
- Comparison pages say video models want 8-16 GB for small/quantized builds and 24 GB+ to avoid offloading to system RAM. A LTX-2.5 GGUF listing recommends 10-12 GB even for its poorest (Q2) quant, which it calls "poor quality, testing only"; weights are only part of the need (+4-8 GB for context/VAE/text encoders).
- One article says people "pushed LTX-2 onto 6GB with large amounts of system RAM, though that route is slow" - no times or settings given. Unverified in practice on this laptop.
- What the 3050 DOES give over the 1060: tensor cores + FP16/BF16, so depth maps, background removal/segmentation (layered animation) and ffmpeg/OpenCV effects run several times faster. Small still-image models (SDXL-class) are also practical, if useful for reference images.
Plan stays: ambient layers + layered cutout animation locally (run the heavy prep on the laptop), paid cloud image-to-video for ~10-15% hero shots at the cheapest acceptable model. Optional low-priority experiment: try one LTX/Wan quantized workflow on the laptop for a single clip to measure real time/quality before ruling it out for good.
