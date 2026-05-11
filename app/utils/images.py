"""Image ingestion helpers — HEIC normalization, EXIF rotation, size cap.

iPhone photos default to HEIC, which Anthropic's vision API doesn't accept.
Normalize everything to JPEG up-front. Also auto-rotates by EXIF and caps the
long edge at 1600px so we don't waste tokens on huge files.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

# Register HEIC opener with PIL.
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_AVAILABLE = True
except Exception:  # pragma: no cover
    HEIF_AVAILABLE = False


HEIC_SUFFIXES = {".heic", ".heif"}
ALLOWED_OUT = {".jpg", ".jpeg", ".png", ".webp"}
MAX_EDGE = 1600


def is_heic(path: Path) -> bool:
    return path.suffix.lower() in HEIC_SUFFIXES


def normalize(path: Path) -> Path:
    """Return a path Anthropic + eBay can ingest.

    HEIC → write a sibling .jpg, return that path.
    Otherwise → if the image needs rotation or shrinking, rewrite in place.
    Always returns a path that exists on disk.
    """
    if not path.exists():
        return path

    suffix = path.suffix.lower()
    needs_convert = suffix in HEIC_SUFFIXES
    needs_rotate_or_shrink = False

    try:
        with Image.open(path) as im:
            # detect rotation / size first to short-circuit
            w, h = im.size
            if max(w, h) > MAX_EDGE:
                needs_rotate_or_shrink = True
            exif = im.getexif()
            if exif and exif.get(274, 1) != 1:  # Orientation tag
                needs_rotate_or_shrink = True

            if not (needs_convert or needs_rotate_or_shrink):
                return path

            im2 = ImageOps.exif_transpose(im)
            if max(im2.size) > MAX_EDGE:
                im2.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
            if im2.mode in ("RGBA", "P"):
                im2 = im2.convert("RGB")

            if needs_convert:
                out = path.with_suffix(".jpg")
                im2.save(out, "JPEG", quality=88, optimize=True)
                return out
            else:
                im2.save(path)
                return path
    except Exception:
        # If we can't open it (corrupt, unknown format), return as-is and let
        # the downstream caller surface the error.
        return path
