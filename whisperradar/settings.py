"""Global Auto Run / producer settings, stored in the `settings` table.

One spec drives everything: the settings page renders the fields from it, the
producer loop and the CLI read typed values through load(), and save()
validates and coerces the posted form. Per-channel overrides live on
own_channels and are NULL when the channel inherits these globals.
"""

import re

from . import briefs

# Flow native renders download at Flow's own size (1376x768); one local
# Real-ESRGAN pass then upscales to one of FlowBatch's tiers.
# Render resolution and the image upscale tier are ONE decision: the images must
# be generated at the size the video is rendered at. The resolution wins.
RESOLUTION_UPSCALE = {"1080p": 1, "2k": 2, "4k": 4, "flow-native": 0}
UPSCALE_RESOLUTION = {v: k for k, v in RESOLUTION_UPSCALE.items()}
UPSCALE_LABELS = {0: "0 - native 1376x768 (Flow native)",
                  1: "1 - HD 1920x1080",
                  2: "2 - 2K 2560x1440",
                  4: "4 - 4K 3840x2160"}


# applying one of these to all channels applies its partner too
PAIRED_FIELDS = {"render_resolution": "default_upscale",
                 "default_upscale": "render_resolution"}


def with_partners(keys):
    """keys + their paired fields, order kept, no duplicates."""
    out = []
    for key in keys:
        for k in (key, PAIRED_FIELDS.get(key)):
            if k and k not in out:
                out.append(k)
    return out


def normalize_upscale(tier):
    """Tier 3 was a duplicate of 2 (both 2K) and is gone from the UI; old
    stored 3s read as 2."""
    return 2 if tier == 3 else tier


def upscale_for_resolution(resolution):
    """The upscale tier that belongs to a render resolution (None if unknown)."""
    return RESOLUTION_UPSCALE.get(str(resolution or "").strip().lower())


def reconcile_resolution_upscale(resolution, upscale, res_posted, up_posted):
    """Keep the pair coherent on save. None = "inherit". Returns
    (resolution, upscale, warning). A resolution that is set decides the
    upscale; an upscale posted alone decides the resolution."""
    warning = ""
    if upscale is not None:
        upscale = normalize_upscale(upscale)
    if res_posted and resolution:
        derived = upscale_for_resolution(resolution)
        if derived is not None:
            if up_posted and upscale is not None and upscale != derived:
                warning = (f"Upscale tier {upscale} did not match Render "
                           f"resolution {resolution} - set to {derived} "
                           f"(the resolution wins)")
            upscale = derived
    elif up_posted and not res_posted and upscale is not None:
        resolution = UPSCALE_RESOLUTION.get(upscale, resolution)
    return resolution, upscale, warning


FLOW_NATIVE_TIERS = ("off", "1k", "2k", "4k")
FLOW_NATIVE_TIER_LABELS = {
    "off": "No upscale (keep 1376x768)",
    "1k": "1080p (1920x1080)",
    "2k": "2K (2560x1440)",
    "4k": "4K (3840x2160)",
}

# Each entry: key, label, type, default, plus type-specific extras.
# type is one of: bool | int | str | choice | time
SPEC: list[dict] = [
    {
        "key": "llm_default", "type": "provider", "default": "",
        "label": "Default LLM",
        "help": "Which provider writes the style, script and shotlist when a "
                "production or its channel does not set one. Saved in the "
                "database. A channel's producer_llm_provider and a "
                "production's own choice both override this.",
    },
    {
        "key": "productions_root", "type": "str", "default": "",
        "label": "Production folders location",
        "help": "One folder for all productions. Each new production gets "
                "its own sub-folder here, named after its title, created "
                "automatically. Empty = data\\studio\\<id>. A production "
                "created with its own working folder keeps that one.",
    },
    {
        "key": "ai33_api_key", "type": "str", "default": "",
        "label": "OpenSpeaker (ai33.pro) API key",
        "help": "Narration key used by the audio stage's AI33 TTS hook and the "
                "voice picker. Get it from the OpenSpeaker app (API section). "
                "Empty = the WR_AI33_API_KEY / AI33_API_KEY environment "
                "variable, then the config.yaml ai33_api_key.",
    },
    {
        "key": "ai33_base_url", "type": "str", "default": "",
        "label": "OpenSpeaker base URL",
        "help": "Optional. Override the OpenSpeaker API host "
                "(default https://api.ai33.pro).",
    },
    {
        "key": "autorun_enabled", "type": "bool", "default": False,
        "label": "Enable Auto Run",
        "help": "Master switch for the unattended producer. Off = nothing "
                "runs by itself.",
    },
    {
        "key": "services_autostart", "type": "bool", "default": False,
        "label": "Start tools with the dashboard",
        "help": "Bring the Renderly API up when the dashboard starts, so "
                "there is no console to open. Off = it is started on demand "
                "by the images stage.",
    },
    {
        "key": "services_managed", "type": "bool", "default": False,
        "label": "Stop services after the images stage",
        "help": "WhisperRadar starts what the images stage needs and, with this "
                "on, stops the ones it started when the stage finishes (the "
                "Renderly API). Services you started yourself are never "
                "touched.",
    },
    {
        "key": "show_apply_all", "type": "bool", "default": False,
        "label": "Show \"apply to all channels\" buttons",
        "help": "Adds an \"apply to all channels\" button next to every "
                "production-default setting (here and on My Channels). It "
                "overwrites the per-channel values of every channel at once, "
                "so it stays hidden - and refused by the server - until you "
                "tick this.",
    },
    {
        "key": "notify_desktop", "type": "bool", "default": False,
        "label": "Desktop notification",
        "help": "Show a Windows balloon tip when an unattended run pauses or "
                "fails (and on success if enabled below).",
    },
    {
        "key": "notify_webhook_url", "type": "str", "default": "",
        "label": "Notification webhook URL",
        "help": "Optional. A Discord/Slack webhook, an ntfy URL (e.g. "
                "https://ntfy.sh/your-topic), or any endpoint that accepts a "
                "JSON POST. Empty = no webhook.",
    },
    {
        "key": "notify_webhook_kind", "type": "choice", "default": "discord",
        "choices": ["discord", "slack", "ntfy", "json"],
        "label": "Webhook format",
        "help": "How to shape the payload for the URL above. ntfy takes the "
                "message as the body; the others POST JSON.",
    },
    {
        "key": "notify_on_success", "type": "bool", "default": False,
        "label": "Notify on success too",
        "help": "Also notify when an unattended run finishes cleanly. Off = "
                "you only hear about pauses and failures.",
    },
    {
        "key": "autorun_resume", "type": "bool", "default": True,
        "label": "Resume unfinished productions",
        "help": "Before creating new productions, continue the auto-run ones "
                "that stopped mid-pipeline - images left over from the "
                "Images-per-run limit, or a Flow refusal that has since "
                "cleared. Only productions the producer created are touched; "
                "anything you are building by hand is left alone.",
    },
    {
        "key": "autorun_plan", "type": "bool", "default": True,
        "label": "Plan the packaging before the script (Auto Run)",
        "help": "Auto Run first writes a packaging plan - SEO title, promise, "
                "opening hook, thumbnail idea - with your API LLMs, judges "
                "it, makes the best title the production's title, and the "
                "script is then written to that promise. A plan that "
                "already exists is kept. If planning fails the run carries "
                "on with the original title.",
    },
    {
        "key": "resume_cooldown_minutes", "type": "int", "default": 60,
        "min": 5, "max": 1440,
        "label": "Resume cooldown (minutes)",
        "help": "Do not re-attempt a production until this long after its last "
                "attempt, so a Flow refusal is not hammered.",
    },
    {
        "key": "resume_per_run", "type": "int", "default": 2, "min": 1, "max": 10,
        "label": "Resumes per run",
        "help": "At most this many unfinished productions are continued in one "
                "auto-run, so a single run cannot sprawl.",
    },
    {
        "key": "scheduler_enabled", "type": "bool", "default": False,
        "label": "Scheduler",
        "help": "Let the running dashboard start Auto Run on its own every "
                "N minutes. It still obeys the run window and the caps above. "
                "Off = you press Produce from channels yourself.",
    },
    {
        "key": "scheduler_interval_minutes", "type": "int", "default": 60,
        "min": 5, "max": 1440,
        "label": "Scheduler interval (minutes)",
        "help": "How often the scheduler checks whether there is anything to "
                "produce. 60 = hourly.",
    },
    {
        "key": "per_day", "type": "int", "default": 1, "min": 0, "max": 50,
        "label": "Productions per day (cap)",
        "help": "Cost guard: the most productions auto-run may create in a "
                "day, across all channels. 0 = no cap.",
    },
    {
        "key": "shotlist_min_alignment", "type": "float", "default": 0.90,
        "min": 0.0, "max": 1.0,
        "label": "Shotlist: minimum prompt detail",
        "help": "Share of shots whose image prompt must state EVERY element "
                "the narration at its cues requires (who, what they are doing, "
                "where, which props, the specific information). A prompt that "
                "under-specifies its scene means the image gets regenerated by "
                "hand later. Checked after planning and BEFORE any image is "
                "rendered. 0.90 = 90% of shots.",
    },
    {
        "key": "shotlist_max_attempts", "type": "int", "default": 4,
        "min": 1, "max": 8,
        "label": "Shotlist: max attempts",
        "help": "How many times to re-plan the shotlist with the under-specified "
                "prompts, the pacing faults and their missing elements fed back. "
                "Structural faults (coverage, order, orphan assets, duplicates) "
                "must always be zero.",
    },
    {
        "key": "shotlist_judge_provider", "type": "provider", "default": "",
        "label": "Shotlist: judge LLM",
        "help": "Which provider audits prompt detail. Empty = automatically a "
                "different provider from the planner.",
    },
    {
        "key": "shotlist_judge_temperature", "type": "float", "default": 0.3,
        "min": 0.0, "max": 1.0,
        "label": "Shotlist: judge temperature",
        "help": "How much the completeness judge's own wording varies between "
                "calls. The planner stays creative (temperature 1.0, unaffected "
                "by this) so retries keep producing different shotlists - this "
                "only tunes how consistently the judge grades them. Lower is "
                "more consistent shot-to-shot and call-to-call; this judge "
                "defaults a bit higher than the script judge because its own "
                "criteria already carve out what a still image cannot show "
                "(recurrence, exact numbers, internal states, etc.), so some "
                "sampling variety here does little harm and this setting is "
                "not itself a strictness dial - the judge's instructions are.",
    },
    {
        "key": "shotlist_max_hold_seconds", "type": "float", "default": 12.0,
        "min": 4.0, "max": 12.0,
        "label": "Shotlist: max seconds per image",
        "help": "The hard maximum hold for any image - 12s is the ceiling, so this "
                "only lets you TIGHTEN it (4-12s). Checked from the SRT cue "
                "timings BEFORE any image renders. The number of images is NEVER "
                "fixed: the cues drive it, so an image may cover one cue or many "
                "and a dense passage can run to 7-8 images a minute. A plan with "
                "a longer hold re-plans with instructions to split at a meaning "
                "boundary and renumber the scene (a new image takes the next "
                "unused sub-beat). The brief's other rules (no long STATIC hold, "
                "ST only on short holds and ~10% of shots, no motion code above "
                "~40% - PL/PR tighter at ~10%) are enforced too, and "
                "one-image-per-cue is a fault.",
    },
    {
        "key": "script_min_rating", "type": "float", "default": 9.0,
        "min": 1.0, "max": 10.0,
        "label": "Script: minimum rating",
        "help": "The script stage rates each draft 1-10 by rubric and only "
                "accepts one at or above this. Below it, the draft is "
                "regenerated with the judge's feedback.",
    },
    {
        "key": "plan_min_rating", "type": "float", "default": 9.5,
        "min": 1.0, "max": 10.0,
        "label": "Packaging: minimum score",
        "help": "The packaging plan, the publish kit and the thumbnail "
                "concepts are each judged 1-10. A round only passes at or "
                "above this score with nothing left to fix; below it the "
                "writer revises. One bar for all three (was a fixed 8.0).",
    },
    {
        "key": "script_max_overlap", "type": "float", "default": 0.12,
        "min": 0.0, "max": 1.0,
        "label": "Script: target overlap",
        "help": "Share of the script's 5-word sequences allowed to also "
                "appear in the source transcript. 0.12 = 12%. Facts and names "
                "set a floor, so 0 is not realistic.",
    },
    {
        "key": "script_hard_overlap", "type": "float", "default": 0.20,
        "min": 0.0, "max": 1.0,
        "label": "Script: hard overlap limit",
        "help": "A draft above this is regenerated no matter how well it "
                "rated - at this level large runs are copied verbatim.",
    },
    {
        "key": "script_max_attempts", "type": "int", "default": 3,
        "min": 1, "max": 10,
        "label": "Script: max attempts",
        "help": "How many drafts to generate before settling for the best "
                "one. Each attempt is one script call plus one rating call.",
    },
    {
        "key": "script_judge_provider", "type": "provider", "default": "",
        "label": "Script: judge LLM",
        "help": "Which provider rates the script. Empty = automatically a "
                "DIFFERENT provider from the one that wrote it, to avoid "
                "self-preference bias.",
    },
    {
        "key": "script_judge_temperature", "type": "float", "default": 0.1,
        "min": 0.0, "max": 1.0,
        "label": "Script: judge temperature",
        "help": "How much the rating judge's own wording varies between "
                "calls. The writer stays creative (temperature 1.0, unaffected "
                "by this) so every regenerate keeps being a genuinely different "
                "script - this only tunes how consistently the judge scores "
                "and applies the copycat-overlap gate. Kept low by default so "
                "the same draft rates the same way twice, and the same "
                "overlap threshold is enforced every time rather than "
                "occasionally waved through by sampling luck.",
    },
    {
        "key": "producer_llm_provider", "type": "provider", "default": "",
        "label": "Producer LLM",
        "help": "Which configured LLM provider picks the topic and writes the "
                "title for Auto Run. Empty = the Default LLM above. Providers "
                "are managed in Settings > LLM providers.",
    },
    {
        "key": "llm_fallback_provider", "type": "provider", "default": "",
        "label": "Fallback LLM",
        "help": "Pinned second choice whenever ANY call needs a fallback - a "
                "provider that stalls, answers empty, or (a judge) rejects "
                "its temperature after the same-model retry. Empty = pick "
                "automatically: a provider on a different gateway than the "
                "failed one if one is configured, else the first other "
                "ready provider in the list above. Global only - the same "
                "fallback is used for every channel and every stage.",
    },
    {
        "key": "candidate_window_days", "type": "int", "default": 90,
        "min": 0, "max": 3650,
        "label": "Candidate window (days)",
        "help": "Only source videos published within this many days are "
                "considered for a new production. 0 = no limit.",
    },
    {
        "key": "default_engine", "type": "choice", "default": "renderly",
        "choices": ["renderly", "flowbatch"],
        "label": "Image engine",
        "help": "Which image pipeline new productions use. Renderly = its "
                "API for PL/PR wide shots, FlowBatch for everything else; "
                "FlowBatch = every shot on Google Flow.",
    },
    {
        "key": "default_render_mode", "type": "choice", "default": "auto",
        "choices": ["auto", "flow", "api"],
        "label": "Render mode",
        "help": "flow = every shot on Google Flow through FlowBatch (same as "
                "the FlowBatch engine); api = the Renderly engine (PL/PR "
                "through the API (Gemini), the rest through FlowBatch); "
                "auto = api.",
    },
    {
        "key": "default_upscale", "type": "int", "default": 2, "min": 0, "max": 4,
        "int_options": UPSCALE_LABELS,
        "label": "Upscale tier",
        "help": "Delivered image size. It is tied to Render resolution (Video "
                "render tab): 1080p = 1, 2K = 2 (recommended - meets "
                "ImgToVideo's 2304x1296 canvas spec), 4K = 4 (over-spec, "
                "slower), Flow native = 0. Changing one changes the other; "
                "if they ever disagree the resolution wins.",
    },
    {
        "key": "images_chunk_size", "type": "int", "default": 60,
        "min": 0, "max": 500,
        "label": "Images per run",
        "help": "Render at most this many images in one batch, then stop and "
                "leave the rest to the next run. Flow tolerates roughly 80-100 "
                "automated generations on an account before it starts refusing "
                "(and BOTH engines share the account), so chunking a big "
                "shotlist spreads it across sessions. 0 = no limit.",
    },
    {
        "key": "images_stop_on_failure", "type": "bool", "default": False,
        "label": "Stop the batch at the first failed image",
        "help": "Stop instead of grinding through the remaining cards: the "
                "FlowBatch (--fail-fast) stops at the "
                "FIRST failed card. Everything rendered is kept and the "
                "production stays resumable. Off (the default) = keep going "
                "and only stop on the 'N failures in a row' rule below. "
                "Flow's 'unusual activity' block always stops the batch.",
    },
    {
        "key": "images_max_consecutive_failures", "type": "int", "default": 5,
        "min": 1, "max": 50,
        "label": "Stop after N failures in a row",
        "help": "Stop the images batch once this many cards fail back-to-back "
                "- a broken session/UI fails every card after the break, so "
                "grinding on just burns the account. Shared by manual "
                "render and auto-run. Independent of 'Stop the batch at the first "
                "failed image' above (which stops on the FIRST failure).",
    },
    {
        "key": "images_resume_wait_minutes", "type": "int", "default": 10,
        "min": 0, "max": 180,
        "label": "Wait before auto-resuming after that stop",
        "help": "How long to pause before automatically resuming the batch "
                "after the consecutive-failure stop above (the account "
                "usually needs a while to clear). Used by manual render and "
                "auto-run alike. 0 = do not auto-resume - stop and wait for "
                "you to click Resume.",
    },
    {
        "key": "images_still_busy_wait_minutes", "type": "int", "default": 5,
        "min": 0, "max": 180,
        "label": "Wait before auto-resuming a 'still busy' wave",
        "help": "Flow's softer 'still busy' timeout leaves some cards "
                "unrendered without the session being refused. This is how "
                "long to pause before resuming those gaps (a shorter wait than "
                "the refusal one above - the condition clears faster). Used by "
                "manual render and auto-run alike. 0 = do not auto-resume - "
                "wait for you to click Resume.",
    },
    {
        "key": "images_throttle_wait_minutes", "type": "int", "default": 60,
        "min": 0, "max": 720,
        "label": "Wait after Flow's 'unusual activity' block",
        "help": "Google's 'We noticed some unusual activity' refusal means "
                "the account/session is being throttled: retrying at once "
                "only makes it worse, and it can take an hour or more to "
                "clear (FlowBatch has seen 2h40m-4h after heavy use). The "
                "batch stops at the first such refusal, waits this long, "
                "then resumes the missing cards. Applies to manual render "
                "and auto-run alike. "
                "0 = do not auto-resume - stop and wait for you to click "
                "Resume.",
    },
    {
        "key": "images_delay_seconds", "type": "int", "default": 0,
        "min": 0, "max": 600,
        "label": "Seconds between images",
        "help": "Wait after each rendered image. Google's reCAPTCHA scores the session "
                "partly on how fast it generates, so a longer gap means fewer "
                "'unusual activity' blocks, at the cost of a slower batch "
                "(e.g. 20 s x 75 images = 25 extra minutes). 0 = "
                "FlowBatch's own default (its config\\settings.json, 1.5 s). "
                "A value replaces FlowBatch's gap.",
    },
    {
        "key": "chrome_efficiency_off", "type": "bool", "default": True,
        "label": "Turn off Chrome Efficiency mode while rendering",
        "help": "Before FlowBatch opens its Chrome, switch Memory Saver "
                "and Energy Saver off in its own automation profile "
                "(FlowBatch's profile folder - never your everyday Chrome). Efficiency mode can slow or freeze "
                "the rendering window. Applied before every launch; skipped "
                "while that Chrome is already open.",
    },
    {
        "key": "generate_references", "type": "bool", "default": True,
        "label": "Generate missing references",
        "help": "Before rendering the shotlist images, generate the reference "
                "images it needs but has no supplied file for (using each "
                "ref's prompt) and put them where the engine can use them. "
                "Refs you DO supply are used as-is, with whatever name they "
                "have; generated ones are named per the CH_/BG_/OBJ_ "
                "convention, capped at 20 per production. Off = the refs "
                "stage is skipped and those refs are simply not attached.",
    },
    {
        "key": "render_resolution", "type": "choice", "default": "2k",
        "choices": ["1080p", "2k", "4k", "flow-native"],
        "choice_labels": {"1080p": "1920x1080 (Full HD)",
                          "2k": "2560x1440 (2K)",
                          "4k": "3840x2160 (4K)",
                          "flow-native": "1376x768 (Flow native)"},
        "label": "Render resolution",
        "help": "Output resolution written into the production's "
                "imgtovideo.json (output.width/height) for the preview build "
                "and the NLE export. 2K is ImgToVideo's own default. The "
                "preview draft stays at 960x540 for speed.",
    },
    {
        "key": "flow_native_upscale", "type": "choice", "default": "off",
        "choices": list(FLOW_NATIVE_TIERS),
        "choice_labels": FLOW_NATIVE_TIER_LABELS,
        "label": "Flow native: upscale level",
        "help": "Only used when Render resolution is Flow native. FlowBatch "
                "downloads the stills at Flow's native size, then ONE local Real-ESRGAN "
                "pass (FlowBatch's upscaler - no Flow, no Renderly backend) "
                "brings them to this level. Files already at the level are "
                "skipped, so the pass can be re-run from the images stage. "
                "The video output size is still the Render resolution above.",
    },
    {
        "key": "render_target", "type": "choice", "default": "premiere",
        "choices": ["premiere", "capcut"],
        "choice_labels": {"premiere": "Premiere Pro",
                          "capcut": "Final Cut (CapCut)"},
        "label": "Render target",
        "help": "What the merge stage exports to. Both targets first build a "
                "fast preview draft (out\\preview.mp4) you can watch in the "
                "dashboard, then write an NLE project: Premiere Pro = an FCP7 "
                "XML to import (File > Import); Final Cut (CapCut) = a CapCut "
                "draft folder to copy into CapCut's draft root.",
    },
    {
        "key": "topic_pick", "type": "choice", "default": "newest",
        "choices": ["newest", "llm"],
        "label": "Topic pick",
        "help": "newest = the latest un-produced source video; llm = let the "
                "model choose the best topic among the un-produced ones.",
    },
    {
        "key": "run_window_start", "type": "time", "default": "09:00",
        "label": "Run window start",
        "help": "Auto-run may only start between these times (local).",
    },
    {
        "key": "run_window_end", "type": "time", "default": "23:00",
        "label": "Run window end",
        "help": "End of the daily auto-run window.",
    },
]

SPEC_BY_KEY = {entry["key"]: entry for entry in SPEC}

# The settings page renders these groups in order; a key missing from every
# group lands in "Other", so adding a SPEC entry can never hide it.
GROUPS: list[tuple[str, list[str]]] = [
    ("LLM", ["llm_default", "producer_llm_provider", "llm_fallback_provider",
             "ai33_api_key", "ai33_base_url"]),
    ("Auto Run", [
        "autorun_enabled", "per_day", "run_window_start", "run_window_end",
        "candidate_window_days", "topic_pick",
        "autorun_plan",
        "autorun_resume", "resume_cooldown_minutes", "resume_per_run",
    ]),
    ("Script & shotlist", [
        "script_min_rating", "plan_min_rating", "script_max_overlap",
        "script_hard_overlap",
        "script_max_attempts", "script_judge_provider",
        "script_judge_temperature",
        "shotlist_min_alignment", "shotlist_max_attempts",
        "shotlist_max_hold_seconds",
        "shotlist_judge_provider", "shotlist_judge_temperature",
    ]),
    ("Production & images", [
        "default_engine", "default_render_mode", "default_upscale",
        "images_chunk_size", "images_stop_on_failure",
        "images_max_consecutive_failures", "images_resume_wait_minutes",
        "images_still_busy_wait_minutes", "images_throttle_wait_minutes",
        "images_delay_seconds", "chrome_efficiency_off", "generate_references",
    ]),
    ("Video render", ["render_target", "render_resolution",
                      "flow_native_upscale"]),
    ("Scheduler", ["scheduler_enabled", "scheduler_interval_minutes"]),
    ("Notifications", [
        "notify_desktop", "notify_webhook_url", "notify_webhook_kind",
        "notify_on_success",
    ]),
    ("Service handling", ["services_autostart", "services_managed",
                         "show_apply_all", "productions_root"]),
]


def grouped_spec() -> list[tuple[str, list[dict]]]:
    """[(group name, [spec entries])] for the settings page."""
    seen: set[str] = set()
    out: list[tuple[str, list[dict]]] = []
    for name, keys in GROUPS:
        entries = [SPEC_BY_KEY[k] for k in keys if k in SPEC_BY_KEY]
        seen.update(k for k in keys if k in SPEC_BY_KEY)
        if entries:
            out.append((name, entries))
    rest = [e for e in SPEC if e["key"] not in seen]
    if rest:
        out.append(("Other", rest))
    return out

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def defaults() -> dict:
    return {entry["key"]: entry["default"] for entry in SPEC}


def _coerce(entry: dict, raw):
    """Turn one posted/stored raw value into its typed form. Invalid input
    falls back to the spec default (save() reports it separately)."""
    kind = entry["type"]
    if raw is None:
        return entry["default"]
    text = raw if isinstance(raw, str) else str(raw)
    text = text.strip()
    if kind == "bool":
        return text.lower() in ("1", "true", "on", "yes")
    if kind == "int":
        if text == "":
            return entry["default"]
        try:
            value = int(float(text))
        except ValueError:
            return entry["default"]
        if "min" in entry:
            value = max(entry["min"], value)
        if "max" in entry:
            value = min(entry["max"], value)
        if entry["key"] == "default_upscale":
            value = normalize_upscale(value)
        return value
    if kind == "float":
        if text == "":
            return entry["default"]
        try:
            value = float(text)
        except ValueError:
            return entry["default"]
        if "min" in entry:
            value = max(entry["min"], value)
        if "max" in entry:
            value = min(entry["max"], value)
        return round(value, 3)
    if kind == "choice":
        return text if text in entry["choices"] else entry["default"]
    if kind == "time":
        return text if _TIME_RE.match(text) else entry["default"]
    return text


def load(conn) -> dict:
    """Typed settings: stored values with the spec defaults filled in."""
    from . import db

    stored = db.all_settings(conn)
    out = {}
    for entry in SPEC:
        out[entry["key"]] = _coerce(entry, stored.get(entry["key"]))
    return out


def row_get(row, key, default=None):
    """sqlite3.Row lookup that tolerates a missing column or a NULL."""
    if row is None:
        return default
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None or value == "" else value


def for_production(conn, prod) -> dict:
    """Effective values for a production, most specific wins:
    global settings <- the production's own channel <- the production.

    Per-channel fields are NULL/empty when the channel inherits the global,
    so a value only overrides when it is actually set. Returns the resolved
    dict plus the own-channel row and its Renderly mirror name.
    """
    from . import db

    glob = load(conn)
    own = db.get_own_channel(conn, row_get(prod, "own_channel_id")) \
        if prod is not None else None
    own_upscale = row_get(own, "default_upscale", glob["default_upscale"])
    own_per_day = row_get(own, "per_day")
    resolution = row_get(own, "render_resolution", glob["render_resolution"])
    configured = normalize_upscale(int(own_upscale))
    derived = upscale_for_resolution(resolution)
    # the resolution wins; warn only when someone explicitly set a tier that
    # disagrees (a channel that sets just the resolution is not a conflict)
    explicit = (row_get(own, "default_upscale") is not None
                or row_get(own, "render_resolution") is None)
    upscale_warning = ""
    if derived is not None and explicit and configured != derived:
        upscale_warning = (f"upscale tier {configured} does not match render "
                           f"resolution {resolution} - using {derived} "
                           f"(the resolution wins)")
    return {
        "voice": (row_get(prod, "voice") or row_get(own, "default_voice")
                  or None),
        "engine": row_get(own, "default_engine", glob["default_engine"]),
        "render_mode": (row_get(prod, "render_mode")
                        or row_get(own, "default_render_mode")
                        or glob["default_render_mode"]),
        "upscale": derived if derived is not None else configured,
        "upscale_warning": upscale_warning,
        # Flow native: the level one local Real-ESRGAN pass upscales the
        # downloaded stills to (per channel; only used when the resolved
        # render resolution is "flow-native")
        "flow_native_upscale": row_get(own, "flow_native_upscale",
                                       glob["flow_native_upscale"]),
        "per_day": (int(own_per_day) if own_per_day is not None
                    else int(glob["per_day"])),
        "topic_pick": row_get(own, "topic_pick", glob["topic_pick"]),
        # the rest of the auto-run criteria are per channel too
        "run_window_start": row_get(own, "run_window_start",
                                    glob["run_window_start"]),
        "run_window_end": row_get(own, "run_window_end",
                                  glob["run_window_end"]),
        "candidate_window_days": int(row_get(own, "candidate_window_days",
                                             glob["candidate_window_days"]) or 0),
        "producer_llm_provider": (row_get(own, "producer_llm_provider")
                                  or glob["producer_llm_provider"] or None),
        # script quality gate (see the SPEC help text for the semantics)
        "script_min_rating": float(row_get(own, "script_min_rating",
                                            glob["script_min_rating"])),
        # packaging plan / publish kit / thumbnail concepts share one bar
        "plan_min_rating": float(glob["plan_min_rating"]),
        "script_max_overlap": float(row_get(own, "script_max_overlap",
                                            glob["script_max_overlap"])),
        "script_hard_overlap": float(glob["script_hard_overlap"]),
        "script_max_attempts": int(row_get(own, "script_max_attempts",
                                           glob["script_max_attempts"])),
        "script_judge_provider": (row_get(own, "script_judge_provider")
                                  or glob["script_judge_provider"] or None),
        # global only, like script_hard_overlap above - the strictness comes
        # from the judge's own rubric, this just keeps its grading consistent
        "script_judge_temperature": float(glob["script_judge_temperature"]),
        # shotlist gate
        "shotlist_min_alignment": float(row_get(own, "shotlist_min_alignment",
                                                glob["shotlist_min_alignment"])),
        "shotlist_max_attempts": int(row_get(own, "shotlist_max_attempts",
                                             glob["shotlist_max_attempts"])),
        "shotlist_judge_provider": (row_get(own, "shotlist_judge_provider")
                                    or glob["shotlist_judge_provider"] or None),
        "shotlist_max_hold_seconds": float(row_get(
            own, "shotlist_max_hold_seconds",
            glob["shotlist_max_hold_seconds"])),
        "shotlist_judge_temperature": float(glob["shotlist_judge_temperature"]),
        "autorun_enabled": bool(glob["autorun_enabled"])
                           and bool(row_get(own, "autorun_enabled", 1)),
        "bible_dir": row_get(own, "bible_dir"),
        "refs_dir": row_get(own, "refs_dir"),
        # text defaults seeded into a new production's style.md / bible.md
        "style": row_get(own, "style"),
        "bible": row_get(own, "bible"),
        # Google Flow project URL for the FlowBatch engine, when the
        # channel sets one (callers fall back to the global config value).
        "flow_project_url": row_get(own, "flow_project_url"),
        # global-only: which NLE the merge stage exports to (premiere|capcut)
        "render_target": row_get(own, "render_target", glob["render_target"]),
        "render_resolution": resolution,
        # image-batch guards (global only - operational, not per production)
        "images_chunk_size": int(glob["images_chunk_size"]),
        "images_stop_on_failure": bool(glob["images_stop_on_failure"]),
        "images_max_consecutive_failures": int(
            glob["images_max_consecutive_failures"]),
        "images_resume_wait_minutes": int(glob["images_resume_wait_minutes"]),
        "images_still_busy_wait_minutes": int(
            glob["images_still_busy_wait_minutes"]),
        "images_throttle_wait_minutes": int(
            glob["images_throttle_wait_minutes"]),
        "images_delay_seconds": int(glob["images_delay_seconds"]),
        "chrome_efficiency_off": bool(glob["chrome_efficiency_off"]),
        # planning-brief profile (briefs.py): the channel's motion preset key
        # (None = standard) and its free-text presentation / narrator staging.
        # Per channel only - no global setting; nothing set = today's brief.
        "brief_motion": row_get(own, "brief_motion") or None,
        # shot-type caps and host-in-frame shares: per channel only
        "brief_types": briefs.effective_types(
            None, row_get(own, "brief_types")),
        "brief_presentation": row_get(own, "brief_presentation") or "",
        # reveal shots (items shown one at a time) in the planning brief:
        # per channel only, off unless the channel turns them on
        "brief_reveal": min(2, int(row_get(own, "brief_reveal") or 0)),
        # the channel's sound effect for reveal shots (default "pop")
        "brief_sfx": row_get(own, "brief_sfx") or "",
        # hold range (s) over the preset; None = the preset's / the global max
        "brief_min_hold": row_get(own, "brief_min_hold"),
        "brief_max_hold": row_get(own, "brief_max_hold"),
        # the channel's own motion spec, used when brief_motion == "custom"
        "brief_custom": briefs.normalize_custom(row_get(own, "brief_custom")),
        # the refs stage is per channel (a channel may have no refs at all)
        "generate_references": bool(row_get(own, "generate_references",
                                            int(glob["generate_references"]))),
        "own_channel": own,
        "own_channel_name": row_get(own, "name"),
        "renderly_channel_name": (row_get(own, "renderly_channel_name")
                                  or row_get(own, "name") or "whisperradar"),
    }


def save(conn, form: dict) -> tuple[dict, list[str]]:
    """Validate + persist a posted form. Returns (values, warnings).

    Absent keys are left untouched, so a partial form never wipes settings.
    """
    from . import db

    values: dict = {}
    warnings: list[str] = []
    for entry in SPEC:
        key = entry["key"]
        if key not in form:
            continue
        raw = form[key]
        values[key] = _coerce(entry, raw)
        if entry["type"] == "time" and not _TIME_RE.match((raw or "").strip()):
            warnings.append(f"{entry['label']}: expected HH:MM - kept "
                            f"{entry['default']}")
        elif entry["type"] == "int" and (raw or "").strip() != "":
            try:
                int(float(raw))
            except ValueError:
                warnings.append(f"{entry['label']}: not a number - kept "
                                f"{entry['default']}")
    # keep Render resolution and Upscale tier a coherent pair
    if "render_resolution" in values or "default_upscale" in values:
        res, up, warn = reconcile_resolution_upscale(
            values.get("render_resolution"), values.get("default_upscale"),
            "render_resolution" in values, "default_upscale" in values)
        if "render_resolution" in values:
            values["default_upscale"] = up
        elif res:
            values["render_resolution"] = res
        if warn:
            warnings.append(warn)
    for key, value in values.items():
        entry = SPEC_BY_KEY[key]
        stored = (("1" if value else "0") if entry["type"] == "bool"
                  else str(value))
        db.set_setting(conn, key, stored)
    return values, warnings
