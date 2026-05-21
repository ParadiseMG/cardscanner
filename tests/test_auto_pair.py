"""EXIF-time auto-pairing + visual verification."""
import os
import random
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import piexif
from PIL import Image, ImageDraw

from app.services.auto_pair import auto_pair, verify_pair, _phash, _hamming
from app.utils.images import read_capture_time


def _stamp_exif(path: Path, when: datetime) -> Path:
    """Save the image at `path` with EXIF DateTimeOriginal set."""
    s = when.strftime("%Y:%m:%d %H:%M:%S").encode()
    exif = {"Exif": {piexif.ExifIFD.DateTimeOriginal: s}}
    with Image.open(path) as im:
        im.save(path, "JPEG", exif=piexif.dump(exif))
    return path


def _photo(tmp_path, name, color, when, size=(400, 560), seed=None, kind="card_front"):
    """Create a non-trivial JPEG so phash distinguishes it from others.

    `kind`:
      - "card_front": colorful, randomized blocks → high saturation, high entropy
      - "card_back":  grayscale text-like pattern → low saturation, high entropy
      - "duplicate_of": pass another path here to make a near-identical image
    """
    p = tmp_path / name
    rng = random.Random(seed if seed is not None else hash(name))
    img = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(img)
    if kind == "card_front":
        # 20 random colored rectangles → noisy phash, high saturation
        for _ in range(20):
            x0, y0 = rng.randint(0, size[0]-1), rng.randint(0, size[1]-1)
            x1, y1 = x0 + rng.randint(20, 100), y0 + rng.randint(20, 100)
            c = (rng.randint(50, 255), rng.randint(50, 255), rng.randint(50, 255))
            draw.rectangle([x0, y0, x1, y1], fill=c)
    elif kind == "card_back":
        # Grayscale lines (mimicking stat blocks) → low saturation
        for y in range(20, size[1] - 20, 12):
            shade = rng.randint(60, 200)
            draw.rectangle([20, y, size[0] - 20, y + 6], fill=(shade, shade, shade))
    img.save(p, "JPEG", quality=85)
    if when is not None:
        _stamp_exif(p, when)
    return p


def test_read_capture_time_round_trip(tmp_path):
    p = _photo(tmp_path, "a.jpg", "red", datetime(2026, 5, 11, 12, 0, 0))
    t = read_capture_time(p)
    assert t == datetime(2026, 5, 11, 12, 0, 0)


def test_read_capture_time_returns_none_when_missing(tmp_path):
    p = tmp_path / "nostamp.jpg"
    Image.new("RGB", (100, 140), "blue").save(p, "JPEG")
    assert read_capture_time(p) is None


def test_pair_matches_within_15_seconds(tmp_path):
    """Front and back photographed 5s apart should pair."""
    base_t = datetime(2026, 5, 11, 12, 0, 0)
    front = _photo(tmp_path, "f.jpg", "red", base_t, seed=1, kind="card_front")
    back = _photo(tmp_path, "b.jpg", (240, 240, 240),
                  base_t + timedelta(seconds=5), seed=2, kind="card_back")

    pairs = auto_pair([front, back])
    assert len(pairs) == 1
    assert pairs[0].front == front
    assert pairs[0].back == back


def test_far_apart_not_paired(tmp_path):
    """20s apart > WINDOW_SECONDS → both singletons."""
    t = datetime(2026, 5, 11, 12, 0, 0)
    a = _photo(tmp_path, "a.jpg", "red", t)
    b = _photo(tmp_path, "b.jpg", "blue", t + timedelta(seconds=20))
    pairs = auto_pair([a, b])
    assert len(pairs) == 2
    assert all(p.back is None for p in pairs)


def test_explicit_filename_pair_wins(tmp_path):
    """`*_front.jpg` + `*_back.jpg` always pair, even with no EXIF."""
    f = tmp_path / "card_front.jpg"
    b = tmp_path / "card_back.jpg"
    Image.new("RGB", (200, 280), "red").save(f, "JPEG")
    Image.new("RGB", (200, 280), "white").save(b, "JPEG")
    pairs = auto_pair([f, b])
    assert len(pairs) == 1
    assert pairs[0].front == f and pairs[0].back == b


def test_verification_rejects_two_fronts(tmp_path):
    """Two saturated photos taken close in time should NOT pair."""
    t = datetime(2026, 5, 11, 12, 0, 0)
    # Both highly saturated (vibrant red + vibrant blue, full color)
    a = _photo(tmp_path, "a.jpg", (255, 30, 30), t)
    b = _photo(tmp_path, "b.jpg", (30, 30, 255), t + timedelta(seconds=5))
    pairs = auto_pair([a, b])
    # Should be split into two singletons
    assert len(pairs) == 2


def test_verification_rejects_duplicate(tmp_path):
    """Two near-identical photos should NOT pair (likely accidental dup)."""
    import shutil
    t = datetime(2026, 5, 11, 12, 0, 0)
    a = _photo(tmp_path, "a.jpg", (200, 200, 200), t, seed=42, kind="card_front")
    # b: literal copy → identical phash
    b = tmp_path / "b.jpg"
    shutil.copy(a, b)
    _stamp_exif(b, t + timedelta(seconds=3))
    pairs = auto_pair([a, b])
    # Both kept as singletons; never paired as front+back
    assert all(p.back is None for p in pairs)


def test_verification_rejects_aspect_mismatch(tmp_path):
    """Different aspect ratios → not the same physical card."""
    t = datetime(2026, 5, 11, 12, 0, 0)
    a = _photo(tmp_path, "a.jpg", (180, 100, 100), t, size=(400, 560))
    # Square: very different aspect
    b = _photo(tmp_path, "b.jpg", (220, 220, 220), t + timedelta(seconds=4), size=(560, 560))
    pairs = auto_pair([a, b])
    assert all(p.back is None for p in pairs)


def test_phash_distance_self_zero(tmp_path):
    """A photo's phash matches itself with distance 0."""
    p = _photo(tmp_path, "x.jpg", (123, 200, 50), None)
    with Image.open(p) as im:
        h = _phash(im)
    assert _hamming(h, h) == 0


def test_burst_of_four_pairs_correctly(tmp_path):
    """Two cards photographed front-back-front-back should yield 2 pairs."""
    t = datetime(2026, 5, 11, 12, 0, 0)
    a_front = _photo(tmp_path, "ca_f.jpg", (220, 30, 30), t, seed=10, kind="card_front")
    a_back = _photo(tmp_path, "ca_b.jpg", (230, 230, 230),
                    t + timedelta(seconds=4), seed=11, kind="card_back")
    # 40-second gap (well > WINDOW)
    b_front = _photo(tmp_path, "cb_f.jpg", (30, 80, 220),
                     t + timedelta(seconds=40), seed=12, kind="card_front")
    b_back = _photo(tmp_path, "cb_b.jpg", (215, 215, 215),
                    t + timedelta(seconds=44), seed=13, kind="card_back")

    pairs = auto_pair([a_front, a_back, b_front, b_back])
    assert len(pairs) == 2
    assert all(p.back is not None for p in pairs)
