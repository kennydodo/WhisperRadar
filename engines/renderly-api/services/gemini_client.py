"""Thin wrapper around the google-genai SDK for image generation."""

import logging
import time

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_IMAGE_MODEL

logger = logging.getLogger(__name__)

_client: genai.Client | None = None


class QuotaExceededError(RuntimeError):
    """Google rejected the request because a quota/billing limit is hit.

    Fatal by design: never retried, and it aborts the rest of a batch.
    """


def get_client() -> genai.Client:
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Add it to backend/.env "
                "(get a key at https://aistudio.google.com/apikey)."
            )
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def _is_quota_error(exc: Exception) -> bool:
    """True when the failure means 'stop generating' (quota or billing)."""
    code = getattr(exc, "code", None)
    text = str(exc).lower()
    if code == 429 or "429" in text or "resource_exhausted" in text:
        return True
    if "quota" in text or "rate limit" in text:
        return True
    # 403 means the account side refused (billing disabled, key restricted) —
    # retrying cannot help, so stop.
    return code == 403 or ("billing" in text and "403" in text)


# Google answers 503 "high demand" / 500 / 504 when the model is overloaded. The
# request was not processed (and not billed), so retrying is safe. Quota and
# billing errors are never retried.
TRANSIENT_RETRY_DELAYS = (4.0, 12.0, 30.0)


def _is_transient_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code in (500, 502, 503, 504):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("503", "unavailable", "high demand", "overloaded", "deadline_exceeded")
    )


def generate_image(
    prompt: str,
    reference_images: list[tuple[bytes, str]] | None = None,
    aspect_ratio: str | None = None,
    image_size: str | None = None,
) -> bytes:
    """Generate one image and return the raw image bytes.

    reference_images: optional list of (data, mime_type) tuples used as
    subject/style references for the model.
    aspect_ratio: optional "1:1", "16:9", "9:16", "4:3", "3:4", "21:9".
    image_size: optional "1K", "2K", "4K".

    Transient overload errors (503 etc.) are retried with backoff; quota/billing
    errors and everything else fail immediately.
    """
    client = get_client()

    contents: list = [prompt]
    for data, mime_type in reference_images or []:
        contents.append(types.Part.from_bytes(data=data, mime_type=mime_type))

    image_config_kwargs = {}
    if aspect_ratio:
        image_config_kwargs["aspect_ratio"] = aspect_ratio
    if image_size:
        image_config_kwargs["image_size"] = image_size

    config = None
    if image_config_kwargs:
        config = types.GenerateContentConfig(
            image_config=types.ImageConfig(**image_config_kwargs)
        )

    attempt = 0
    while True:
        try:
            response = client.models.generate_content(
                model=GEMINI_IMAGE_MODEL,
                contents=contents,
                config=config,
            )
            break
        except Exception as exc:  # noqa: BLE001 - classify before surfacing
            if _is_quota_error(exc):
                logger.warning("Quota/billing limit hit: %s", exc)
                raise QuotaExceededError(
                    f"Quota/billing limit reached — generation stopped. ({exc})"
                ) from exc
            if _is_transient_error(exc) and attempt < len(TRANSIENT_RETRY_DELAYS):
                delay = TRANSIENT_RETRY_DELAYS[attempt]
                attempt += 1
                logger.warning("Gemini overloaded (%s); retry %d in %.0fs", exc, attempt, delay)
                time.sleep(delay)
                continue
            raise

    for part in response.parts:
        inline = getattr(part, "inline_data", None)
        if inline and inline.data:
            return inline.data

    text = getattr(response, "text", None)
    raise RuntimeError(f"Model returned no image data: {text or 'empty response'}")
