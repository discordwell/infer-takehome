from pathlib import Path

import pytest

from backend import storage


@pytest.fixture(autouse=True)
def tmp_storage_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(storage, "STORAGE_DIR", tmp_path)
    yield


def test_roundtrip():
    state = {"cookies": [{"name": "session", "value": "abc"}], "origins": []}
    storage.save("geico", "user@example.com", state)
    assert storage.load("geico", "user@example.com") == state


def test_load_missing_returns_none():
    assert storage.load("geico", "nobody") is None


def test_different_users_isolated():
    storage.save("geico", "a", {"cookies": [{"name": "x", "value": "1"}]})
    storage.save("geico", "b", {"cookies": [{"name": "x", "value": "2"}]})
    assert storage.load("geico", "a")["cookies"][0]["value"] == "1"
    assert storage.load("geico", "b")["cookies"][0]["value"] == "2"


def test_different_carriers_isolated():
    storage.save("geico", "u", {"cookies": [{"name": "x", "value": "g"}]})
    storage.save("progressive", "u", {"cookies": [{"name": "x", "value": "p"}]})
    assert storage.load("geico", "u")["cookies"][0]["value"] == "g"
    assert storage.load("progressive", "u")["cookies"][0]["value"] == "p"


def test_delete_removes_file():
    storage.save("geico", "u", {"cookies": []})
    storage.delete("geico", "u")
    assert storage.load("geico", "u") is None


def test_delete_missing_is_noop():
    storage.delete("geico", "nobody")  # should not raise


def test_corrupt_file_returns_none(tmp_path):
    storage.save("geico", "u", {"cookies": []})
    # corrupt the file
    f = next(tmp_path.iterdir())
    f.write_text("not json")
    assert storage.load("geico", "u") is None


def test_saved_at_returns_timestamp():
    import time

    before = time.time()
    storage.save("geico", "u", {"cookies": []})
    after = time.time()
    ts = storage.saved_at("geico", "u")
    assert ts is not None
    assert before <= ts <= after


def test_saved_at_missing_returns_none():
    assert storage.saved_at("geico", "nobody") is None
