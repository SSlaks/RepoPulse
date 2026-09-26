from io import BytesIO

from app.avatar_cache import AVATAR_MAX_EDGE, downscale_avatar
from PIL import Image


def _png(size: tuple[int, int], *, noisy: bool) -> bytes:
    image = (
        Image.effect_noise(size, 80).convert("RGB")
        if noisy
        else Image.new("RGB", size, (30, 120, 200))
    )
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_downscale_shrinks_large_avatar() -> None:
    original = _png((1024, 1024), noisy=True)

    shrunk = downscale_avatar(original)

    assert len(shrunk) < len(original)
    with Image.open(BytesIO(shrunk)) as result:
        assert max(result.size) <= AVATAR_MAX_EDGE


def test_downscale_keeps_small_avatar_unchanged() -> None:
    original = _png((64, 64), noisy=False)

    assert downscale_avatar(original) == original


def test_downscale_returns_invalid_content_unchanged() -> None:
    original = b"not-an-image"

    assert downscale_avatar(original) == original
