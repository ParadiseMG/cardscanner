"""Drive file pairing logic — pure functions."""
from app.services import drive_inbox


def test_pair_front_back():
    files = [
        {"id": "1", "name": "card_001_front.jpg"},
        {"id": "2", "name": "card_001_back.jpg"},
        {"id": "3", "name": "card_002_front.jpg"},
        {"id": "4", "name": "card_002_back.jpg"},
    ]
    pairs = drive_inbox.pair_files(files)
    assert len(pairs) == 2
    for f, b in pairs:
        assert f and b
        assert f["name"].endswith("_front.jpg")
        assert b["name"].endswith("_back.jpg")


def test_singleton_unpaired():
    files = [
        {"id": "1", "name": "topps_chrome_acuna.jpg"},
        {"id": "2", "name": "card_only_front.jpg"},
    ]
    pairs = drive_inbox.pair_files(files)
    # one is unpaired regular, one has front but no back
    assert len(pairs) == 2


def test_mixed_pairs_and_singletons():
    files = [
        {"id": "1", "name": "x_front.jpg"},
        {"id": "2", "name": "x_back.jpg"},
        {"id": "3", "name": "y.jpg"},
    ]
    pairs = drive_inbox.pair_files(files)
    assert len(pairs) == 2
