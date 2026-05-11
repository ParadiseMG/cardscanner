"""HEIC + EXIF + size normalization."""
import io
from pathlib import Path

from PIL import Image

from app.utils import images


def test_jpeg_under_max_unchanged(tmp_path):
    p = tmp_path / "small.jpg"
    Image.new("RGB", (400, 600), "red").save(p)
    out = images.normalize(p)
    assert out == p
    with Image.open(out) as im:
        assert im.size == (400, 600)


def test_oversized_jpeg_shrunk(tmp_path):
    p = tmp_path / "big.jpg"
    Image.new("RGB", (3000, 4000), "blue").save(p)
    out = images.normalize(p)
    assert out == p
    with Image.open(out) as im:
        assert max(im.size) <= images.MAX_EDGE


def test_heic_converted_to_jpeg(tmp_path):
    if not images.HEIF_AVAILABLE:
        import pytest; pytest.skip("pillow-heif not installed")
    p = tmp_path / "card.heic"
    # Build a real HEIC by encoding through pillow_heif
    from pillow_heif import from_pillow
    from_pillow(Image.new("RGB", (400, 600), "green")).save(str(p))
    out = images.normalize(p)
    assert out.suffix.lower() in {".jpg", ".jpeg"}
    assert out.exists()
    with Image.open(out) as im:
        assert im.format == "JPEG"


def test_corrupt_image_returns_path(tmp_path):
    p = tmp_path / "bad.jpg"
    p.write_bytes(b"not an image")
    out = images.normalize(p)
    assert out == p  # graceful: return as-is
