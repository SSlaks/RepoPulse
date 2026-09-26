from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

from app.config import get_settings

AVATAR_MAX_EDGE = 128
AVATAR_RECOMPRESS_THRESHOLD_BYTES = 20_480


def cache_dir() -> Path:
    path = Path(get_settings().avatar_cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def avatar_path(owner_id: int) -> Path:
    return cache_dir() / f"{owner_id}.img"


def is_fresh(path: Path) -> bool:
    return path.exists() and datetime.fromtimestamp(path.stat().st_mtime, UTC) > datetime.now(
        UTC
    ) - timedelta(days=get_settings().avatar_refresh_days)


def downscale_avatar(content: bytes) -> bytes:
    """Shrink an avatar so the slow cross-border link carries a small file.

    Avatars render at 42-58 px, so multi-hundred-kilobyte originals waste
    bandwidth.  Images that are already small, or that cannot be decoded, are
    returned unchanged so a bad download never blocks the cache.
    """
    try:
        image = Image.open(BytesIO(content))
    except (OSError, ValueError):
        return content
    try:
        image_format = (image.format or "JPEG").upper()
        if (
            max(image.size) <= AVATAR_MAX_EDGE
            and len(content) <= AVATAR_RECOMPRESS_THRESHOLD_BYTES
        ):
            return content
        image.thumbnail((AVATAR_MAX_EDGE, AVATAR_MAX_EDGE), Image.Resampling.LANCZOS)
        output = BytesIO()
        if image_format == "PNG":
            image.save(output, "PNG", optimize=True)
        elif image_format == "WEBP":
            image.save(output, "WEBP", quality=82)
        else:
            image.convert("RGB").save(output, "JPEG", quality=82, optimize=True)
        return output.getvalue()
    except (OSError, ValueError):
        return content
    finally:
        image.close()


def download_avatar(owner_id: int, url: str) -> Path:
    if not url.startswith(("https://github.com/", "https://avatars.githubusercontent.com/")):
        raise ValueError("unsupported avatar host")
    target = avatar_path(owner_id)
    with httpx.Client(
        timeout=get_settings().avatar_request_timeout, follow_redirects=True
    ) as client:
        response = client.get(url, headers={"User-Agent": "RepoPulse/0.1"})
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if content_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            raise ValueError("unsupported avatar content type")
        if len(response.content) > 1_048_576:
            raise ValueError("avatar too large")
        stored = downscale_avatar(response.content)
        fd, temporary = tempfile.mkstemp(prefix=f"{owner_id}-", dir=cache_dir())
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(stored)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return target
