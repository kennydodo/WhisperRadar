# WhisperRadar

Watch a list of YouTube channels (plus "similar" channels you pick), detect
new uploads daily, download the audio, transcribe it with Whisper, and keep
everything in a local database so you can export transcripts and feed them to
an LLM.

## Layout

```
whisperradar/
├── config.yaml              # channels, paths, whisper settings
├── wr.py                    # CLI entrypoint (or: python -m whisperradar)
├── whisperradar/
│   ├── cli.py               # commands
│   ├── config.py            # config loading
│   ├── db.py                # SQLite schema + helpers
│   ├── watch.py             # RSS feed checks, channel resolution
│   ├── download.py          # yt-dlp audio download
│   ├── transcribe.py        # faster-whisper (GPU -> CPU fallback)
│   └── pipeline.py          # orchestration
├── scripts/
│   ├── daily_run.cmd        # what the scheduled task executes
│   └── schedule_daily.ps1   # registers the Windows scheduled task
└── data/                    # created at runtime (db, audio, transcripts)
```

## Setup

1. Install [ffmpeg](https://ffmpeg.org) (needed by yt-dlp and Whisper):

   ```powershell
   winget install Gyan.FFmpeg
   ```

2. Create a virtual environment and install dependencies:

   ```powershell
   cd C:\Users\Kehinde\source\whisperradar
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```

3. Add your channels:

```powershell
python wr.py add https://www.youtube.com/@SomeChannel
python wr.py add https://www.youtube.com/@RelatedChannel --similar
python wr.py channels
```

**Primary vs similar channels:** `primary` channels auto-download new uploads
as they appear. `similar` channels are watch-only: everything they publish
lands in **backlog** and is downloaded only when you explicitly queue it
(`wr.py backlog <id|all>` or the dashboard's queue button) - so exploratory
channels never eat bandwidth uninvited.

## Web dashboard

Easiest: double-click `start_dashboard.cmd` (starts the server and opens your
browser; if it is already running, it just opens the page).

Or from a terminal:

```powershell
python wr.py serve          # http://127.0.0.1:8540
python wr.py serve --port 9000
```

The dashboard lets you add/remove channels (it resolves any YouTube URL or
@handle), trigger Watch / Download / Transcribe / Run-all with live log
output, browse videos by status/genre/channel with pagination, read
transcripts in the browser, download individual `.txt` files, and export
everything as a zip.

It runs on [waitress](https://docs.pylonsproject.org/projects/waitress/) (a
production WSGI server) and falls back to the Flask dev server if waitress is
missing.

### Auto-start at login

```powershell
powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1          # install
powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 -Remove  # remove
```

Installs a shortcut in your Startup folder: at every login the dashboard
starts minimized (server + browser). Needs no admin rights.

## Studio (video production pipeline)

The **Studio** tab turns competitor research into original videos. Create a
production from any transcribed video in your library, then walk its stages:
**style → script → audio → srt → shots → images → merge → review**.

- **Every stage is flexible**: run the automation (if its hook is configured)
  or do it by hand - paste a script, upload 11labs audio, your own .srt,
  images, or the final video. Approve & advance, or send back for rework.
  Script and additional-direction can be saved under names and reloaded as
  versions to try different takes.
- **Shots stage**: the LLM plans `shotlist.json` + per-image prompts using
  ImgToVideo's `manifest-authoring-brief.md` (read fresh from the repo on
  every run, so edits to the brief apply immediately); the batch sheet is kept
  as `batch_sheet.txt`, and prompts can also be extracted from the shotlist.
  A character/reference bible can be provided for recurring characters.
- **Images stage**: render the missing shotlist images via the **Renderly API**
  (Gemini backend) or the **Flow Driver** (Renderly's extension-v2 drives
  Google Flow in a real Chrome window; import + upscale via Renderly). The
  choice is remembered per production; upload your own images anytime.
- **Style guide**: the LLM analyzes the source transcript's writing style
  (tone, pacing, hooks, structure) into an editable `style.md`, and every
  script/image generation must match it while using only the facts - original
  wording, familiar feel. A 5-gram overlap check warns if a script drifts
  too close to the source.
- **LLM providers** (`studio.llm_providers` in config.yaml): named
  OpenAI-compatible providers (GLM, DeepSeek, ...) with separate API keys
  (or `WR_<NAME>_API_KEY` env variables); pick one per generation.
- **Tool hooks** (`tts_command`, `imagegen_command`, `merge_command`) plug in
  chatterbox, Renderly and ImgToVideo when you're ready.

## Daily usage

Everything at once (what the scheduler runs):

```powershell
python wr.py run
```

Or step by step for debugging:

```powershell
python wr.py watch        # refresh feeds, log new uploads
python wr.py download     # download pending audio (new uploads only)
python wr.py transcribe   # transcribe downloaded audio
python wr.py videos --status transcribed
python wr.py export all --out C:\transcripts-for-llm
python wr.py export <video_id> --out C:\some\folder
```

### Backlog (old videos)

When you add a channel, its existing uploads are logged as **backlog** and are
*not* auto-downloaded - only videos published after you subscribed are. To
process older videos on purpose:

```powershell
python wr.py backlog                 # list backlog
python wr.py backlog <video_id>      # queue one
python wr.py backlog all             # queue everything
python wr.py download <video_id>     # download one now
python wr.py transcribe <video_id>   # (re-)transcribe one now
```

The dashboard does the same: `backlog` filter chip, per-video **queue**,
**transcribe** and **retry** buttons. It also groups by **genre** and
**channel**: use the channel dropdown and genre chips to filter, and click a
channel name to see only its videos.

### Editing channels

Change a channel's genre, name, kind or state later — from the dashboard
(**edit** button on the channel row) or the CLI:

```powershell
python wr.py edit "Japan Living Inside" --genre lifestyle
python wr.py edit <id> --name "New Name" --kind similar --pause
```

Changing the genre moves the channel's existing audio/transcript files into
the new genre folder and updates the database, so nothing gets mixed up.

### Deleting videos

```powershell
python wr.py delete <video_id>               # remove from library + delete files
python wr.py delete <video_id> --keep-files  # remove from library, keep files
python wr.py reset <video_id>                # delete files, keep + re-queue video
```

The dashboard's **del** button per video does the same as `delete`. On disk,
audio and transcripts are stored per genre (`data\audio\<genre>\`,
`data\transcripts\<genre>\`) so channels never mix. `python wr.py clean`
scans recursively for files no longer tracked in the database.

If a download or transcription fails, the video is marked `error` - fix the
cause (or just try again) with `wr.py run --retry`, `wr.py transcribe <id>`,
or the dashboard's retry button.

### Cleaning orphan files

Audio/transcripts left over from removed videos or a deleted database can be
listed and removed:

```powershell
python wr.py clean          # dry run: lists orphans
python wr.py clean --yes    # delete them
```

Re-queue videos that errored:

```powershell
python wr.py run --retry
```

## Scheduling (daily check)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\schedule_daily.ps1 -Time 09:00
```

Output goes to `data\run.log`. Remove the task later with
`schtasks /Delete /TN "WhisperRadar Daily" /F`.

## GPU vs CPU

`transcribe.py` auto-detects CUDA via ctranslate2:

- **GPU available** -> `device=cuda`, `compute_type=float16`
- **No GPU / CUDA load fails** (missing cuDNN DLLs, old driver) -> silently
  falls back to `device=cpu`, `compute_type=int8`

For GPU support install CUDA/cuDNN-enabled ctranslate2 dependencies per the
[faster-whisper docs](https://github.com/SYSTRAN/faster-whisper); otherwise it
just runs on CPU. The device used for each video is shown in the logs and
stored in the DB.

Model size is set in `config.yaml` (`tiny | base | small | medium | large-v3`).
`small` is a good default; `large-v3` is much better but slow on CPU.

## Configuration

All options live in `config.yaml`:

| Key | Meaning |
| --- | --- |
| `channels` | list of `{name, id, kind, genre, active}`; `kind` = `primary` or `similar`, `genre` = category tag |
| `whisper.model` | model size |
| `whisper.language` | `null` = auto-detect, or e.g. `en` |
| `cookies_from_browser` | `chrome` / `firefox` / `edge` if YouTube bot-checks you |
| `max_video_seconds` | skip videos longer than this |

**Genres**: tag each channel with a genre (`python wr.py add <url> --genre tech`
or `genre: tech` in config). Filter videos by genre with
`python wr.py videos --genre tech` or the dashboard's genre chips, and export
per genre — `python wr.py export all` organizes transcripts into
`<out>/<genre>/` folders, while `--genre tech` exports just that genre.
The dashboard's "Export all transcripts (.zip)" also accepts `?genre=tech`.

## Troubleshooting

- **`ffmpeg not found`** -> install it (see setup) and reopen the terminal.
- **Bot-check / sign-in errors** -> set `cookies_from_browser` in config.
- **RSS only shows the last 15 videos** -> run `python wr.py run` at least
  daily, or use `--retry` after a gap.
- **CUDA errors in logs** -> it already fell back to CPU; install cuDNN 9 DLLs
  to re-enable GPU.
