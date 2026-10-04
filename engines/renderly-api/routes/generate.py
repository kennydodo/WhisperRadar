import io
import json
import re
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from itertools import islice
from pathlib import Path

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from config import (
    GEMINI_IMAGE_MODEL,
    IMAGE_PRICE_USD,
    STORAGE_DIR,
)
from db import get_db
from models import Asset, Channel, Generation, Project
from services import gemini_client, thumbs, upscaler
from services.gemini_client import QuotaExceededError

router = APIRouter(prefix="/api", tags=["generate"])

AspectRatio = Literal["1:1", "16:9", "9:16", "4:3", "3:4", "21:9"]
RefStrength = Literal["loose", "balanced", "strict"]
ImageSize = Literal["1K", "2K", "4K"]
ResolutionTier = Literal["HD", "2K", "4K"]

NAME_TOKEN_RE = re.compile(r"^\s*([\w\-]+\.(?:png|jpe?g))\s+(.*)$", re.IGNORECASE | re.DOTALL)


def split_prompt_name(prompt: str) -> tuple[str | None, str]:
    """Split a leading 'NAME.png' / 'NAME.jpg' token off a prompt, if present."""
    match = NAME_TOKEN_RE.match(prompt.strip())
    if not match:
        return None, prompt.strip()
    name = match.group(1).strip()
    rest = match.group(2).strip()
    if not rest:
        return None, prompt.strip()
    return name, rest


def _sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\-.]", "_", name).strip("._ ")
    # Avoid doubled extensions like "NAME.png.png"
    if cleaned.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        cleaned = cleaned.rsplit(".", 1)[0]
    return cleaned or "image"


def _unique_filename(directory: Path, filename: str) -> str:
    """Return filename (relative name only) made unique within directory."""
    target = directory / filename
    if not target.exists():
        return filename
    stem = target.stem
    ext = target.suffix
    counter = 2
    while True:
        candidate = f"{stem}-{counter}{ext}"
        if not (directory / candidate).exists():
            return candidate
        counter += 1

STRENGTH_INSTRUCTIONS = {
    "loose": (
        "Treat the attached reference image(s) as loose inspiration only; "
        "you may deviate freely from them."
    ),
    "strict": (
        "Reproduce the attached reference image(s) as faithfully as possible, "
        "matching their composition, colors, and details."
    ),
}


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1)
    name: str | None = Field(default=None, max_length=200)
    asset_ids: list[int] = Field(default_factory=list)
    generation_ids: list[int] = Field(default_factory=list)
    project_id: int | None = None
    aspect_ratio: AspectRatio = "16:9"
    ref_strength: RefStrength = "balanced"
    image_size: ImageSize = "1K"  # always 1K from the API; upscaling is local
    upscale_level: int | None = None  # 0 = off; None = use the saved setting


class BatchItem(BaseModel):
    prompt: str = Field(min_length=1)
    name: str | None = Field(default=None, max_length=200)
    asset_ids: list[int] = Field(default_factory=list)
    generation_ids: list[int] = Field(default_factory=list)


class BatchGenerateRequest(BaseModel):
    items: list[BatchItem] = Field(min_length=1)
    asset_ids: list[int] = Field(default_factory=list)
    generation_ids: list[int] = Field(default_factory=list)
    project_id: int | None = None
    aspect_ratio: AspectRatio = "16:9"
    ref_strength: RefStrength = "balanced"
    image_size: ImageSize = "1K"  # always 1K from the API; upscaling is local
    upscale_level: int | None = None  # 0 = off; None = use the saved setting
    parallel: bool = False


class GenerationPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    hidden: bool | None = None
    recent_removed: bool | None = None
    category: str | None = None  # image | character | video


class RegenerateRequest(BaseModel):
    prompt: str | None = Field(default=None, min_length=1)
    aspect_ratio: AspectRatio | None = None
    ref_strength: RefStrength | None = None
    image_size: ImageSize | None = None
    upscale_level: int | None = None  # 0 = off; None = use the saved setting


class SaveAsAssetRequest(BaseModel):
    channel_id: int


class UpscaleRequest(BaseModel):
    tier: ResolutionTier | None = None
    scale: int | None = None  # legacy 2x/4x request, mapped onto the nearest tier


class GenerationOut(BaseModel):
    id: int
    channel_id: int
    name: str
    prompt: str
    model: str
    status: str
    image_url: str | None
    error: str | None
    cost_usd: float
    hidden: bool
    recent_removed: bool = False
    category: str = "image"
    project_id: int | None = None
    batch_id: str | None
    aspect_ratio: str
    ref_strength: str
    image_size: str
    created_at: datetime

    model_config = {"from_attributes": True}


def _get_channel_or_404(db: Session, channel_id: int) -> Channel:
    channel = db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return channel


def _generation_file(generation: Generation):
    rel = (generation.image_url or "").removeprefix("/storage/")
    rel = rel.split("?", 1)[0]  # drop cache-buster query markers
    return STORAGE_DIR / rel


def _resolve_asset_map(db: Session, asset_ids: list[int]) -> dict[int, tuple[bytes, str]]:
    unique = list(dict.fromkeys(asset_ids))
    if not unique:
        return {}
    assets = list(db.scalars(select(Asset).where(Asset.id.in_(unique))).all())
    if len(assets) != len(unique):
        raise HTTPException(status_code=400, detail="One or more asset_ids do not exist")
    result = {}
    for asset in assets:
        path = STORAGE_DIR / str(asset.channel_id) / asset.stored_name
        if not path.exists():
            raise HTTPException(status_code=400, detail=f"Asset file missing for asset {asset.id}")
        result[asset.id] = (path.read_bytes(), asset.mime_type)
    return result


def _resolve_generation_map(db: Session, generation_ids: list[int]) -> dict[int, tuple[bytes, str]]:
    unique = list(dict.fromkeys(generation_ids))
    if not unique:
        return {}
    generations = list(db.scalars(select(Generation).where(Generation.id.in_(unique))).all())
    if len(generations) != len(unique):
        raise HTTPException(status_code=400, detail="One or more generation_ids do not exist")
    result = {}
    for generation in generations:
        if generation.status != "done" or not generation.image_url:
            raise HTTPException(
                status_code=400,
                detail=f"Generation {generation.id} has no image to reference",
            )
        result[generation.id] = (_generation_file(generation).read_bytes(), "image/png")
    return result


def _ordered_refs(
    id_list: list[int], ref_map: dict[int, tuple[bytes, str]]
) -> list[tuple[bytes, str]]:
    refs = []
    for ref_id in dict.fromkeys(id_list):
        if ref_id not in ref_map:
            raise HTTPException(status_code=400, detail=f"Reference id {ref_id} is invalid")
        refs.append(ref_map[ref_id])
    return refs


def _load_reference_bytes(
    db: Session, asset_ids: list[int], generation_ids: list[int]
) -> list[tuple[bytes, str]]:
    """Resolve reference images from any channel's assets and/or past generations."""
    refs = list(_resolve_asset_map(db, asset_ids).values())
    refs += list(_resolve_generation_map(db, generation_ids).values())
    return refs


def _auto_name(prompt: str) -> str:
    words = " ".join(prompt.split()).split(" ")
    name = " ".join(words[:5])[:48].strip(" -,;:.\"'")
    return name or "Untitled"


def _default_project_id(db: Session, channel_id: int) -> int | None:
    """First project created for the channel (imports land there by default)."""
    return db.scalar(
        select(Project.id).where(Project.channel_id == channel_id).order_by(Project.id)
    )


def _create_generation(
    db: Session,
    channel: Channel,
    prompt: str,
    aspect_ratio: str,
    ref_strength: str,
    image_size: str,
    ref_asset_ids: list[int],
    ref_generation_ids: list[int],
    batch_id: str | None = None,
    name: str | None = None,
    project_id: int | None = None,
) -> Generation:
    generation = Generation(
        channel_id=channel.id,
        project_id=project_id,
        prompt=prompt,
        name=(name or "").strip(),
        model=GEMINI_IMAGE_MODEL,
        status="pending",
        aspect_ratio=aspect_ratio,
        ref_strength=ref_strength,
        image_size=image_size,
        ref_asset_ids=json.dumps(ref_asset_ids),
        ref_generation_ids=json.dumps(ref_generation_ids),
        batch_id=batch_id,
    )
    db.add(generation)
    db.commit()
    db.refresh(generation)
    return generation


def _produce_image(
    prompt: str,
    references: list[tuple[bytes, str]],
    aspect_ratio: str,
    ref_strength: str,
    image_size: str,
) -> bytes:
    """Run the Gemini call (safe to run in a worker thread - no DB access)."""
    final_prompt = prompt
    if references and ref_strength in STRENGTH_INSTRUCTIONS:
        final_prompt = f"{final_prompt} {STRENGTH_INSTRUCTIONS[ref_strength]}"
    return gemini_client.generate_image(final_prompt, references, aspect_ratio, image_size)


def _complete_generation(
    db: Session, generation: Generation, image_bytes: bytes,
    upscale_level: int | None = None,
) -> None:
    if generation.name:
        filename = _unique_filename(
            STORAGE_DIR / str(generation.channel_id),
            _sanitize_filename(generation.name) + ".png",
        )
    else:
        filename = f"{uuid.uuid4().hex}.png"
    target = STORAGE_DIR / str(generation.channel_id) / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(image_bytes)
    generation.image_url = f"/storage/{generation.channel_id}/{filename}"
    generation.status = "done"
    generation.cost_usd = IMAGE_PRICE_USD.get(generation.image_size, 0.0)
    if not generation.name:
        generation.name = _auto_name(generation.prompt)
    db.commit()
    db.refresh(generation)

    # Auto-upscale + auto-download per the user's settings. Runs in a daemon
    # thread so the gallery shows "done" immediately; the upscaled file is
    # picked up via the cache-busted image_url on the next poll.
    threading.Thread(
        target=_auto_post_process, args=(generation.id, upscale_level), daemon=True,
    ).start()


def _auto_post_process(generation_id: int, level_override: int | None = None) -> None:
    import shutil

    from db import SessionLocal
    from routes.settings import auto_process_settings, normalize_level

    db = SessionLocal()
    try:
        generation = db.get(Generation, generation_id)
        if generation is None or generation.status != "done" or not generation.image_url:
            return
        settings_level, auto_download, download_dir = auto_process_settings(db)
        level = settings_level if level_override is None else normalize_level(level_override)
        tier = upscaler.level_to_tier(level)
        if tier and upscaler.is_available():
            try:
                _upscale_in_place(db, generation, tier)
            except Exception as exc:
                print(f"[auto-upscale] generation {generation_id}: {exc}")
        if not auto_download or not download_dir:
            return
        channel = db.get(Channel, generation.channel_id)
        src = _generation_file(generation)
        if channel is None or not src.exists():
            return
        target_dir = Path(download_dir) / _sanitize_filename(channel.name)
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(src, target_dir / src.name)
            print(f"[auto-download] {src.name} -> {target_dir / src.name}")
        except OSError as exc:
            print(f"[auto-download] generation {generation_id}: {exc}")
    finally:
        db.close()


def _fail_generation(db: Session, generation: Generation, exc: Exception) -> None:
    generation.status = "error"
    generation.error = str(exc)
    # Failed generations never belong in the gallery - auto-hide them;
    # they surface in Settings -> Hidden images for removal.
    generation.hidden = True
    db.commit()
    db.refresh(generation)


class _BatchSkipped(Exception):
    """Raised in a worker that must not call the API (batch already stopped)."""


def _skip_generation(db: Session, generation: Generation) -> None:
    """Mark a batch row that was never attempted because the quota ran out."""
    generation.status = "error"
    generation.error = "Skipped — batch stopped: quota/billing limit reached"
    generation.hidden = True
    db.commit()
    db.refresh(generation)


def _run_single(
    db: Session,
    channel: Channel,
    prompt: str,
    references: list[tuple[bytes, str]],
    aspect_ratio: str,
    ref_strength: str,
    image_size: str,
    ref_asset_ids: list[int],
    ref_generation_ids: list[int],
    batch_id: str | None = None,
    name: str | None = None,
    project_id: int | None = None,
    upscale_level: int | None = None,
) -> Generation:
    generation = _create_generation(
        db, channel, prompt, aspect_ratio, ref_strength, image_size,
        ref_asset_ids, ref_generation_ids, batch_id, name, project_id,
    )
    try:
        image_bytes = _produce_image(prompt, references, aspect_ratio, ref_strength, image_size)
        _complete_generation(db, generation, image_bytes, upscale_level)
    except Exception as exc:  # noqa: BLE001 - persist any failure on the row
        _fail_generation(db, generation, exc)
    return generation


@router.post("/channels/{channel_id}/generate", response_model=GenerationOut)
def generate_single(
    channel_id: int,
    body: GenerateRequest,
    db: Session = Depends(get_db),
):
    channel = _get_channel_or_404(db, channel_id)
    # A leading "NAME.png" / "NAME.jpg" token in the prompt names the output file
    token_name, clean_prompt = split_prompt_name(body.prompt.strip())
    name = (body.name or "").strip() or token_name
    references = _load_reference_bytes(db, body.asset_ids, body.generation_ids)
    return _run_single(
        db,
        channel,
        clean_prompt,
        references,
        body.aspect_ratio,
        body.ref_strength,
        "1K",  # always 1K from the API; upscaling is local
        body.asset_ids,
        body.generation_ids,
        name=name,
        project_id=body.project_id,
        upscale_level=body.upscale_level,
    )


@router.post("/channels/{channel_id}/generate/batch", response_model=list[GenerationOut])
def generate_batch(
    channel_id: int,
    body: BatchGenerateRequest,
    db: Session = Depends(get_db),
):
    channel = _get_channel_or_404(db, channel_id)
    items = [item for item in body.items if item.prompt.strip()]
    if not items:
        raise HTTPException(status_code=400, detail="No valid prompts supplied")

    all_asset_ids = list(body.asset_ids) + [a for item in items for a in item.asset_ids]
    all_generation_ids = list(body.generation_ids) + [
        g for item in items for g in item.generation_ids
    ]
    asset_map = _resolve_asset_map(db, all_asset_ids)
    gen_map = _resolve_generation_map(db, all_generation_ids)

    main_refs = _ordered_refs(body.asset_ids, asset_map) + _ordered_refs(
        body.generation_ids, gen_map
    )
    main_asset_ids = list(dict.fromkeys(body.asset_ids))
    main_generation_ids = list(dict.fromkeys(body.generation_ids))

    batch_id = uuid.uuid4().hex
    jobs: list[tuple[Generation, list[tuple[bytes, str]]]] = []
    for item in items:
        # Per-item "NAME.png" token (or explicit name field) names the file
        token_name, clean_prompt = split_prompt_name(item.prompt.strip())
        item_name = (item.name or "").strip() or token_name
        if item.asset_ids or item.generation_ids:
            # Row-level override: this prompt uses only its own references.
            refs = _ordered_refs(item.asset_ids, asset_map) + _ordered_refs(
                item.generation_ids, gen_map
            )
            ref_a, ref_g = list(dict.fromkeys(item.asset_ids)), list(
                dict.fromkeys(item.generation_ids)
            )
        else:
            # No attachments on this row: fall back to the main references.
            refs = main_refs
            ref_a, ref_g = main_asset_ids, main_generation_ids
        generation = _create_generation(
            db, channel, clean_prompt, body.aspect_ratio, body.ref_strength,
            "1K", ref_a, ref_g, batch_id, item_name,
            project_id=body.project_id,
        )
        jobs.append((generation, refs))

    if body.parallel and len(jobs) > 1:
        # Hard stop: jobs are submitted on demand, so once the quota is hit
        # no further request is ever sent to the API.
        stop = threading.Event()
        pending_jobs = iter(jobs)

        def guarded_produce(prompt, refs):
            if stop.is_set():
                raise _BatchSkipped()
            return _produce_image(prompt, refs, body.aspect_ratio, body.ref_strength, "1K")

        in_flight: dict = {}
        with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:

            def submit_next(n):
                # Resolve gen.prompt HERE (main thread) - Generation objects
                # must not be touched from worker threads.
                for gen, refs in islice(pending_jobs, n):
                    in_flight[pool.submit(guarded_produce, gen.prompt, refs)] = gen

            submit_next(min(4, len(jobs)))
            while in_flight:
                done_futures, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done_futures:
                    generation = in_flight.pop(future)
                    try:
                        _complete_generation(
                            db, generation, future.result(), body.upscale_level
                        )
                    except _BatchSkipped:
                        _skip_generation(db, generation)
                    except QuotaExceededError as exc:
                        _fail_generation(db, generation, exc)
                        stop.set()
                    except Exception as exc:  # noqa: BLE001
                        _fail_generation(db, generation, exc)
                if not stop.is_set():
                    submit_next(len(done_futures))
            # Rows never submitted because the batch was stopped:
            for gen, _ in pending_jobs:
                _skip_generation(db, gen)
    else:
        quota_stopped = False
        for generation, refs in jobs:
            if quota_stopped:
                # Quota/billing limit hit: never send another request.
                _skip_generation(db, generation)
                continue
            try:
                image_bytes = _produce_image(
                    generation.prompt, refs, body.aspect_ratio, body.ref_strength, "1K"
                )
                _complete_generation(db, generation, image_bytes, body.upscale_level)
            except QuotaExceededError as exc:
                # Fail this row, then stop the whole batch immediately.
                _fail_generation(db, generation, exc)
                quota_stopped = True
            except Exception as exc:  # noqa: BLE001
                _fail_generation(db, generation, exc)

    return list(
        db.scalars(
            select(Generation).where(Generation.batch_id == batch_id).order_by(Generation.id)
        ).all()
    )


@router.post("/generations/{generation_id}/regenerate", response_model=GenerationOut)
def regenerate_generation(
    generation_id: int,
    body: RegenerateRequest | None = None,
    db: Session = Depends(get_db),
):
    source = db.get(Generation, generation_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    channel = _get_channel_or_404(db, source.channel_id)

    body = body or RegenerateRequest()
    prompt = (body.prompt or source.prompt).strip()
    aspect_ratio = body.aspect_ratio or source.aspect_ratio
    ref_strength = body.ref_strength or source.ref_strength
    image_size = "1K"  # always 1K from the API; upscaling is local

    ref_asset_ids = json.loads(source.ref_asset_ids or "[]")
    ref_generation_ids = json.loads(source.ref_generation_ids or "[]")
    references = _load_reference_bytes(db, ref_asset_ids, ref_generation_ids)
    return _run_single(
        db,
        channel,
        prompt,
        references,
        aspect_ratio,
        ref_strength,
        image_size,
        ref_asset_ids,
        ref_generation_ids,
        project_id=source.project_id,
        upscale_level=body.upscale_level,
    )


@router.post("/generations/{generation_id}/retry", response_model=GenerationOut)
def retry_generation(generation_id: int, db: Session = Depends(get_db)):
    """Manually re-run a failed generation in place (no auto-retries exist)."""
    generation = db.get(Generation, generation_id)
    if generation is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    if generation.status != "error":
        raise HTTPException(status_code=400, detail="Only failed generations can be retried")

    # Reset the same row so history keeps one entry for this prompt.
    generation.status = "pending"
    generation.error = None
    generation.hidden = False
    db.commit()
    db.refresh(generation)

    ref_asset_ids = json.loads(generation.ref_asset_ids or "[]")
    ref_generation_ids = json.loads(generation.ref_generation_ids or "[]")
    try:
        references = _load_reference_bytes(db, ref_asset_ids, ref_generation_ids)
        image_bytes = _produce_image(
            generation.prompt,
            references,
            generation.aspect_ratio,
            generation.ref_strength,
            "1K",  # always 1K from the API; upscaling is local
        )
        _complete_generation(db, generation, image_bytes, None)
    except Exception as exc:  # noqa: BLE001 - persist any failure on the row
        _fail_generation(db, generation, exc)
    return generation


@router.patch("/generations/{generation_id}", response_model=GenerationOut)
def patch_generation(
    generation_id: int,
    body: GenerationPatch,
    db: Session = Depends(get_db),
):
    generation = db.get(Generation, generation_id)
    if generation is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    if body.name is not None:
        generation.name = body.name.strip()
    if body.hidden is not None:
        generation.hidden = body.hidden
    if body.recent_removed is not None:
        generation.recent_removed = body.recent_removed
    if body.category is not None:
        if body.category not in ("image", "character", "video"):
            raise HTTPException(status_code=400, detail="Invalid category")
        generation.category = body.category
    db.commit()
    db.refresh(generation)
    return generation


@router.delete("/generations/{generation_id}")
def delete_generation(generation_id: int, db: Session = Depends(get_db)):
    generation = db.get(Generation, generation_id)
    if generation is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    file_path = _generation_file(generation)
    db.delete(generation)
    db.commit()
    if generation.image_url:
        file_path.unlink(missing_ok=True)
    return {"ok": True}


@router.post("/generations/{generation_id}/save-as-asset", status_code=201)
def save_generation_as_asset(
    generation_id: int,
    body: SaveAsAssetRequest,
    db: Session = Depends(get_db),
):
    generation = db.get(Generation, generation_id)
    if generation is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    if generation.status != "done" or not generation.image_url:
        raise HTTPException(status_code=400, detail="Generation has no image to save")

    channel = db.get(Channel, body.channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Target channel not found")

    data = _generation_file(generation).read_bytes()
    stored_name = f"{uuid.uuid4().hex}.png"
    target_dir = STORAGE_DIR / str(channel.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / stored_name).write_bytes(data)

    asset = Asset(
        channel_id=channel.id,
        original_name=(generation.name or f"generation-{generation.id}") + ".png",
        stored_name=stored_name,
        url_path=f"/storage/{channel.id}/{stored_name}",
        mime_type="image/png",
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return {
        "id": asset.id,
        "channel_id": asset.channel_id,
        "original_name": asset.original_name,
        "url_path": asset.url_path,
        "mime_type": asset.mime_type,
        "created_at": asset.created_at,
    }


@router.get("/upscale/status")
def upscale_status():
    return upscaler.info()


@router.get("/thumb/gen/{generation_id}")
def generation_thumb(generation_id: int, db: Session = Depends(get_db)):
    generation = db.get(Generation, generation_id)
    if generation is None or not generation.image_url:
        raise HTTPException(status_code=404, detail="No image for this generation")
    path = _generation_file(generation)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Image file missing")
    try:
        thumb = thumbs.ensure_thumb(path, f"gen{generation_id}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Thumbnail failed: {exc}")
    return FileResponse(thumb, media_type="image/png")


def _upscale_in_place(db: Session, source: Generation, tier: str) -> Generation:
    """Upscale the generation's image and update the SAME record — the
    gallery keeps one entry, one file, and the ORIGINAL name (no 2x/4x
    suffixes). A query marker on image_url busts the browser cache."""
    src_path = _generation_file(source)
    tmp_path = src_path.with_name(f"tmp_upscale_{uuid.uuid4().hex}.png")
    try:
        width, height = upscaler.upscale(src_path, tmp_path, tier)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    try:
        src_path.unlink()
    except OSError:
        pass
    tmp_path.replace(src_path)
    source.image_url = f"/storage/{source.channel_id}/{src_path.name}?v={uuid.uuid4().hex[:8]}"
    source.image_size = upscaler.classify_size(width, height)
    db.commit()
    db.refresh(source)
    return source


@router.post("/generations/{generation_id}/upscale", response_model=GenerationOut)
def upscale_generation(
    generation_id: int,
    body: UpscaleRequest,
    db: Session = Depends(get_db),
):
    if not upscaler.is_available():
        raise HTTPException(status_code=503, detail=upscaler._NOT_INSTALLED)
    source = db.get(Generation, generation_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    if source.status != "done" or not source.image_url:
        raise HTTPException(status_code=400, detail="Generation has no image to upscale")
    try:
        return _upscale_in_place(db, source, upscaler.resolve_tier(body.tier, body.scale))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/channels/{channel_id}/import", response_model=GenerationOut)
async def import_generation(
    channel_id: int,
    file: UploadFile = File(...),
    prompt: str = Form(default="Imported from Google Flow"),
    name: str = Form(default=""),
    project_id: int = Form(default=None),
    db: Session = Depends(get_db),
):
    channel = _get_channel_or_404(db, channel_id)
    target_project_id = _default_project_id(db, channel.id)
    if project_id:
        project = db.get(Project, project_id)
        if project is None or project.channel_id != channel.id:
            raise HTTPException(
                status_code=400, detail="Project does not belong to this channel"
            )
        target_project_id = project.id
    mime_type = file.content_type or "image/png"
    if not mime_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="Only image files can be imported")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    filename = f"{uuid.uuid4().hex}.png"
    target = STORAGE_DIR / str(channel.id) / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    try:
        width, height = upscaler.png_dimensions(target)
        image_size = upscaler.classify_size(width, height)
    except Exception:
        image_size = "1K"

    prompt_text = (prompt or "Imported from Google Flow").strip() or "Imported from Google Flow"
    token_name, clean_prompt = split_prompt_name(prompt_text)
    prompt_text = clean_prompt
    display_name = (name or "").strip() or token_name or _auto_name(prompt_text)
    # Honor the token name for the stored file too (strip the extension)
    if token_name:
        stored_name = _unique_filename(
            STORAGE_DIR / str(channel.id), _sanitize_filename(token_name) + ".png"
        )
        stored = STORAGE_DIR / str(channel.id) / stored_name
        target.replace(stored)
        filename = stored_name
    generation = Generation(
        channel_id=channel.id,
        project_id=target_project_id,
        name=display_name,
        prompt=prompt_text,
        model="google-flow-import",
        status="done",
        image_url=f"/storage/{channel.id}/{filename}",
        aspect_ratio="16:9",
        ref_strength="balanced",
        image_size=image_size,
        cost_usd=0.0,
        hidden=False,
    )
    db.add(generation)
    db.commit()
    db.refresh(generation)
    return generation


@router.get("/spend")
def spend_summary(
    channel_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    query = select(Generation).where(Generation.status == "done")
    if channel_id is not None:
        query = query.where(Generation.channel_id == channel_id)
    if date_from:
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="date_from must be YYYY-MM-DD")
        query = query.where(Generation.created_at >= start)
    if date_to:
        try:
            end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        except ValueError:
            raise HTTPException(status_code=400, detail="date_to must be YYYY-MM-DD")
        query = query.where(Generation.created_at < end)

    rows = list(db.scalars(query).all())
    total = sum(r.cost_usd for r in rows)
    failed = len(
        db.scalars(
            select(Generation.id).where(
                Generation.status == "error",
                *([Generation.channel_id == channel_id] if channel_id is not None else []),
            )
        ).all()
    )
    return {
        "total_usd": round(total, 4),
        "image_count": len(rows),
        "failed_count": failed,
        "prices": {size: round(price, 4) for size, price in IMAGE_PRICE_USD.items()},
    }


@router.get("/generations", response_model=list[GenerationOut])
def list_generations(
    channel_id: int | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    hidden: str | None = None,
    category: str | None = None,
    project_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    query = select(Generation).order_by(Generation.created_at.desc())

    if hidden == "only":
        query = query.where(Generation.hidden == True)  # noqa: E712
    elif hidden == "all":
        pass
    else:
        query = query.where(Generation.hidden == False)  # noqa: E712

    if category in ("image", "character", "video"):
        query = query.where(Generation.category == category)

    if project_id is not None:
        query = query.where(Generation.project_id == project_id)

    if channel_id is not None:
        query = query.where(Generation.channel_id == channel_id)
    if status:
        query = query.where(Generation.status == status)
    if search:
        like = f"%{search.strip()}%"
        query = query.where(or_(Generation.name.ilike(like), Generation.prompt.ilike(like)))
    if date_from:
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="date_from must be YYYY-MM-DD")
        query = query.where(Generation.created_at >= start)
    if date_to:
        try:
            end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        except ValueError:
            raise HTTPException(status_code=400, detail="date_to must be YYYY-MM-DD")
        query = query.where(Generation.created_at < end)

    return list(db.scalars(query.limit(limit).offset(offset)).all())


@router.get("/generations/{generation_id}", response_model=GenerationOut)
def get_generation(generation_id: int, db: Session = Depends(get_db)):
    generation = db.get(Generation, generation_id)
    if generation is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    return generation
