# FFmpeg capabilities on this machine (probed 2026-10-17)

Build: `ffmpeg version 9.0.1-full_build-www.gyan.dev` (gcc 16.1.0, MSYS2).

Relevant configuration flags: `--enable-libfreetype --enable-libharfbuzz --enable-libfribidi
--enable-fontconfig --enable-libass --enable-libx264 --enable-nvenc --enable-amf
--enable-libvpl (qsv) --enable-libplacebo`.

## Verdict: every filter the explainer needs is available

| Filter | Present | Notes for the explainer |
|---|---|---|
| `drawtext` | yes (libfreetype) | `x`/`y` are expressions, `fontsize`/`fontcolor`/`box*` are per-frame (T) timeline-enabled, `expansion=normal` supports `%{eif:...}` counters |
| `drawbox` | yes | `x`/`y`/`width`/`height` expressions, `eval=frame`, timeline (`enable=`) |
| `overlay` | yes | `eval=frame`, `enable=between(t,..)`, `alpha` option |
| `fade` | yes | `alpha=1` fades RGBA plates in/out |
| `geq` | yes | computed masks (frame-pipe fallback only needs ffmpeg stdin) |
| `alphamerge` | yes | static mask reveals |
| `drawgrid` | yes | static grids |
| `gradients` | yes | animated background washes if ever needed |
| `color` (lavfi) | yes | backgrounds |
| `subtitles` | yes (libass) | burned-in captions already used by `shorts.py` |
| `xfade`, `zoompan`, `concat`, `amix`, `tpad`, `format` | yes | already used by the engine |

## Encoders

`libx264` (reference), `h264_nvenc` (RTX 3050), `h264_amf`, `h264_qsv` all compiled in.
Encoder probing already works via `video/encoder_probe.py`; renderer defaults stay
`libx264` for determinism, hardware is acceleration only.

## Decision applied to the architecture

- Filtergraph backend can express: fades, slides (overlay x/y expressions),
  counters (`drawtext` `%{eif:trunc(...)}`), animated bars (drawbox width expr),
  grids (drawgrid + drawbox), plates composited via overlay.
- Pillow plates: text (typography/wrap), path geometry, diagram art.
- Frame-pipe backend: only for true arc-length path tracing (ffmpeg cannot do it).
- Both backends emit the same artifact: silent frame-exact clip in `out/explainer/`.
