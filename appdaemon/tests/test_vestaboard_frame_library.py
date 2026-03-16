"""
Unit tests for vestaboard_configuration_app.frame_library.

Covers:
- add_frame + get_frame
- update_frame changes fields
- delete_frame removes
- list_frames with creator filter
- list_frames with min_rating filter
- list_frames sort_by options (created_at, rating, name)
- Persistence: save + load round-trip (uses tmp_path fixture)
- Missing file: load creates empty library
- to_json serializes correctly
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

# Make vestaboard_configuration_app importable without AppDaemon installed
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

import pytest
from vestaboard_apps.vestaboard_configuration.frame_library import FrameLibrary, LibraryFrame

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BLANK_CHARS: list[list[int]] = [[0] * 22 for _ in range(6)]


def make_lib_frame(
    creator: str = "Tom",
    name: str = "Test Frame",
    rating: int = 3,
    created_at: float = 1000.0,
) -> LibraryFrame:
    return LibraryFrame(
        frame_id=uuid.uuid4().hex,
        characters=BLANK_CHARS,
        creator=creator,
        created_at=created_at,
        rating=rating,
        name=name,
    )


def make_library(tmp_path: Path) -> FrameLibrary:
    storage = str(tmp_path / "frame-library.json")
    logs: list[str] = []
    lib = FrameLibrary(storage_path=storage, log_fn=logs.append)
    lib.load()
    return lib


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TestAddAndGet:
    def test_add_then_get(self, tmp_path):
        lib = make_library(tmp_path)
        frame = make_lib_frame(name="Hello World")
        lib.add_frame(frame)
        retrieved = lib.get_frame(frame.frame_id)
        assert retrieved is not None
        assert retrieved.frame_id == frame.frame_id
        assert retrieved.name == "Hello World"

    def test_get_unknown_returns_none(self, tmp_path):
        lib = make_library(tmp_path)
        assert lib.get_frame("nonexistent") is None

    def test_add_multiple(self, tmp_path):
        lib = make_library(tmp_path)
        f1 = make_lib_frame(name="A")
        f2 = make_lib_frame(name="B")
        lib.add_frame(f1)
        lib.add_frame(f2)
        assert lib.get_frame(f1.frame_id) is not None
        assert lib.get_frame(f2.frame_id) is not None


class TestUpdateFrame:
    def test_update_name(self, tmp_path):
        lib = make_library(tmp_path)
        frame = make_lib_frame(name="Old Name")
        lib.add_frame(frame)
        result = lib.update_frame(frame.frame_id, name="New Name")
        assert result is True
        assert lib.get_frame(frame.frame_id).name == "New Name"

    def test_update_rating(self, tmp_path):
        lib = make_library(tmp_path)
        frame = make_lib_frame(rating=2)
        lib.add_frame(frame)
        lib.update_frame(frame.frame_id, rating=5)
        assert lib.get_frame(frame.frame_id).rating == 5

    def test_update_creator(self, tmp_path):
        lib = make_library(tmp_path)
        frame = make_lib_frame(creator="Tom")
        lib.add_frame(frame)
        lib.update_frame(frame.frame_id, creator="Mom")
        assert lib.get_frame(frame.frame_id).creator == "Mom"

    def test_update_characters(self, tmp_path):
        lib = make_library(tmp_path)
        frame = make_lib_frame()
        lib.add_frame(frame)
        new_chars = [[1] * 22 for _ in range(6)]
        lib.update_frame(frame.frame_id, characters=new_chars)
        assert lib.get_frame(frame.frame_id).characters == new_chars

    def test_update_immutable_frame_id_ignored(self, tmp_path):
        lib = make_library(tmp_path)
        frame = make_lib_frame()
        original_id = frame.frame_id
        lib.add_frame(frame)
        # frame_id is both the positional arg and an immutable field;
        # pass it only via kwargs by using a helper dict
        lib.update_frame(original_id, **{"name": "updated"})
        # Verify the frame_id didn't change even after update
        assert lib.get_frame(original_id).frame_id == original_id
        assert lib.get_frame(original_id).name == "updated"

    def test_update_immutable_created_at_ignored(self, tmp_path):
        lib = make_library(tmp_path)
        frame = make_lib_frame(created_at=1000.0)
        lib.add_frame(frame)
        lib.update_frame(frame.frame_id, created_at=9999.0)
        assert lib.get_frame(frame.frame_id).created_at == 1000.0

    def test_update_nonexistent_returns_false(self, tmp_path):
        lib = make_library(tmp_path)
        result = lib.update_frame("nonexistent", name="X")
        assert result is False

    def test_update_multiple_fields(self, tmp_path):
        lib = make_library(tmp_path)
        frame = make_lib_frame(name="Old", rating=1, creator="Tom")
        lib.add_frame(frame)
        lib.update_frame(frame.frame_id, name="New", rating=4, creator="Dad")
        f = lib.get_frame(frame.frame_id)
        assert f.name == "New"
        assert f.rating == 4
        assert f.creator == "Dad"


class TestDeleteFrame:
    def test_delete_existing(self, tmp_path):
        lib = make_library(tmp_path)
        frame = make_lib_frame()
        lib.add_frame(frame)
        result = lib.delete_frame(frame.frame_id)
        assert result is True
        assert lib.get_frame(frame.frame_id) is None

    def test_delete_nonexistent(self, tmp_path):
        lib = make_library(tmp_path)
        result = lib.delete_frame("ghost")
        assert result is False

    def test_delete_does_not_affect_others(self, tmp_path):
        lib = make_library(tmp_path)
        f1 = make_lib_frame(name="Keep")
        f2 = make_lib_frame(name="Delete")
        lib.add_frame(f1)
        lib.add_frame(f2)
        lib.delete_frame(f2.frame_id)
        assert lib.get_frame(f1.frame_id) is not None
        assert lib.get_frame(f2.frame_id) is None


# ---------------------------------------------------------------------------
# list_frames filtering and sorting
# ---------------------------------------------------------------------------


class TestListFrames:
    def _populated_lib(self, tmp_path) -> FrameLibrary:
        lib = make_library(tmp_path)
        frames = [
            make_lib_frame(creator="Tom", name="Zebra", rating=5, created_at=3000.0),
            make_lib_frame(creator="Mom", name="Apple", rating=3, created_at=1000.0),
            make_lib_frame(creator="Tom", name="Mango", rating=1, created_at=2000.0),
            make_lib_frame(creator="Dad", name="Berry", rating=4, created_at=4000.0),
        ]
        for f in frames:
            lib.add_frame(f)
        return lib

    def test_list_all(self, tmp_path):
        lib = self._populated_lib(tmp_path)
        results = lib.list_frames()
        assert len(results) == 4

    def test_filter_by_creator(self, tmp_path):
        lib = self._populated_lib(tmp_path)
        results = lib.list_frames(creator="Tom")
        assert all(f.creator == "Tom" for f in results)
        assert len(results) == 2

    def test_filter_by_creator_no_match(self, tmp_path):
        lib = self._populated_lib(tmp_path)
        results = lib.list_frames(creator="Jackson")
        assert results == []

    def test_filter_by_min_rating(self, tmp_path):
        lib = self._populated_lib(tmp_path)
        results = lib.list_frames(min_rating=4)
        assert all(f.rating >= 4 for f in results)
        assert len(results) == 2  # rating=5 and rating=4

    def test_filter_by_min_rating_zero(self, tmp_path):
        lib = self._populated_lib(tmp_path)
        results = lib.list_frames(min_rating=0)
        assert len(results) == 4

    def test_filter_combined(self, tmp_path):
        lib = self._populated_lib(tmp_path)
        # Tom + min_rating=3 → only Zebra (rating=5), not Mango (rating=1)
        results = lib.list_frames(creator="Tom", min_rating=3)
        assert len(results) == 1
        assert results[0].name == "Zebra"

    def test_sort_by_created_at_descending(self, tmp_path):
        lib = self._populated_lib(tmp_path)
        results = lib.list_frames(sort_by="created_at", ascending=False)
        created_ats = [f.created_at for f in results]
        assert created_ats == sorted(created_ats, reverse=True)

    def test_sort_by_created_at_ascending(self, tmp_path):
        lib = self._populated_lib(tmp_path)
        results = lib.list_frames(sort_by="created_at", ascending=True)
        created_ats = [f.created_at for f in results]
        assert created_ats == sorted(created_ats)

    def test_sort_by_rating_descending(self, tmp_path):
        lib = self._populated_lib(tmp_path)
        results = lib.list_frames(sort_by="rating", ascending=False)
        ratings = [f.rating for f in results]
        assert ratings == sorted(ratings, reverse=True)

    def test_sort_by_rating_ascending(self, tmp_path):
        lib = self._populated_lib(tmp_path)
        results = lib.list_frames(sort_by="rating", ascending=True)
        ratings = [f.rating for f in results]
        assert ratings == sorted(ratings)

    def test_sort_by_name_descending(self, tmp_path):
        lib = self._populated_lib(tmp_path)
        results = lib.list_frames(sort_by="name", ascending=False)
        names = [f.name for f in results]
        assert names == sorted(names, reverse=True)

    def test_sort_by_name_ascending(self, tmp_path):
        lib = self._populated_lib(tmp_path)
        results = lib.list_frames(sort_by="name", ascending=True)
        names = [f.name for f in results]
        assert names == sorted(names)

    def test_filter_by_category(self, tmp_path):
        lib = make_library(tmp_path)
        msg = make_lib_frame(name="Message Frame", created_at=1000.0)
        art = make_lib_frame(name="Art Frame", created_at=2000.0)
        art.category = "art"
        lib.add_frame(msg)
        lib.add_frame(art)
        assert len(lib.list_frames(category="message")) == 1
        assert lib.list_frames(category="message")[0].name == "Message Frame"
        assert len(lib.list_frames(category="art")) == 1
        assert lib.list_frames(category="art")[0].name == "Art Frame"

    def test_default_category_is_message(self, tmp_path):
        """Frames created without explicit category should default to 'message'."""
        lib = make_library(tmp_path)
        frame = make_lib_frame(name="Old Frame", created_at=1000.0)
        lib.add_frame(frame)
        assert frame.category == "message"
        results = lib.list_frames(category="message")
        assert any(f.name == "Old Frame" for f in results)

    def test_update_category(self, tmp_path):
        lib = make_library(tmp_path)
        frame = make_lib_frame(name="Movable")
        lib.add_frame(frame)
        assert frame.category == "message"
        lib.update_frame(frame.frame_id, category="art")
        assert lib.get_frame(frame.frame_id).category == "art"

    def test_category_persists_roundtrip(self, tmp_path):
        storage_path = str(tmp_path / "library.json")
        lib1 = FrameLibrary(storage_path=storage_path)
        lib1.load()
        frame = make_lib_frame(name="Art Piece")
        frame.category = "art"
        lib1.add_frame(frame)

        lib2 = FrameLibrary(storage_path=storage_path)
        lib2.load()
        retrieved = lib2.get_frame(frame.frame_id)
        assert retrieved.category == "art"

    def test_invalid_sort_by_defaults_to_created_at(self, tmp_path):
        lib = self._populated_lib(tmp_path)
        # Should not raise; falls back to created_at
        results = lib.list_frames(sort_by="nonexistent_field")
        assert len(results) == 4


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        storage_path = str(tmp_path / "library.json")
        logs: list[str] = []

        # Write
        lib1 = FrameLibrary(storage_path=storage_path, log_fn=logs.append)
        lib1.load()
        frame = make_lib_frame(name="Roundtrip Test", creator="Dad", rating=4)
        lib1.add_frame(frame)

        # Read fresh instance
        lib2 = FrameLibrary(storage_path=storage_path, log_fn=logs.append)
        lib2.load()
        retrieved = lib2.get_frame(frame.frame_id)

        assert retrieved is not None
        assert retrieved.name == "Roundtrip Test"
        assert retrieved.creator == "Dad"
        assert retrieved.rating == 4
        assert retrieved.characters == BLANK_CHARS

    def test_save_multiple_frames_roundtrip(self, tmp_path):
        storage_path = str(tmp_path / "library.json")
        lib1 = FrameLibrary(storage_path=storage_path)
        lib1.load()
        frames = [make_lib_frame(name=f"Frame {i}", created_at=float(i)) for i in range(5)]
        for f in frames:
            lib1.add_frame(f)

        lib2 = FrameLibrary(storage_path=storage_path)
        lib2.load()
        for f in frames:
            assert lib2.get_frame(f.frame_id) is not None

    def test_save_after_delete_persists(self, tmp_path):
        storage_path = str(tmp_path / "library.json")
        lib1 = FrameLibrary(storage_path=storage_path)
        lib1.load()
        f1 = make_lib_frame(name="Keep")
        f2 = make_lib_frame(name="Delete")
        lib1.add_frame(f1)
        lib1.add_frame(f2)
        lib1.delete_frame(f2.frame_id)

        lib2 = FrameLibrary(storage_path=storage_path)
        lib2.load()
        assert lib2.get_frame(f1.frame_id) is not None
        assert lib2.get_frame(f2.frame_id) is None

    def test_save_after_update_persists(self, tmp_path):
        storage_path = str(tmp_path / "library.json")
        lib1 = FrameLibrary(storage_path=storage_path)
        lib1.load()
        frame = make_lib_frame(name="Before", rating=2)
        lib1.add_frame(frame)
        lib1.update_frame(frame.frame_id, name="After", rating=5)

        lib2 = FrameLibrary(storage_path=storage_path)
        lib2.load()
        retrieved = lib2.get_frame(frame.frame_id)
        assert retrieved.name == "After"
        assert retrieved.rating == 5

    def test_atomic_write(self, tmp_path):
        """Verify that no .tmp file is left behind after save."""
        storage_path = str(tmp_path / "library.json")
        lib = FrameLibrary(storage_path=storage_path)
        lib.load()
        lib.add_frame(make_lib_frame())

        # No .tmp files should remain
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []


class TestMissingFile:
    def test_load_creates_empty_library_when_missing(self, tmp_path):
        storage_path = str(tmp_path / "subdir" / "frame-library.json")
        lib = FrameLibrary(storage_path=storage_path)
        lib.load()  # Should not raise
        assert lib.list_frames() == []

    def test_load_creates_file_when_missing(self, tmp_path):
        storage_path = str(tmp_path / "subdir" / "frame-library.json")
        lib = FrameLibrary(storage_path=storage_path)
        lib.load()
        assert os.path.exists(storage_path)

    def test_load_empty_lib_then_add(self, tmp_path):
        storage_path = str(tmp_path / "frame-library.json")
        lib = FrameLibrary(storage_path=storage_path)
        lib.load()
        frame = make_lib_frame()
        lib.add_frame(frame)
        assert lib.get_frame(frame.frame_id) is not None


# ---------------------------------------------------------------------------
# to_json
# ---------------------------------------------------------------------------


class TestToJson:
    def test_to_json_empty(self, tmp_path):
        lib = make_library(tmp_path)
        result = lib.to_json()
        parsed = json.loads(result)
        assert parsed == []

    def test_to_json_contains_all_frames(self, tmp_path):
        lib = make_library(tmp_path)
        f1 = make_lib_frame(name="Alpha")
        f2 = make_lib_frame(name="Beta")
        lib.add_frame(f1)
        lib.add_frame(f2)

        result = lib.to_json()
        parsed = json.loads(result)
        assert len(parsed) == 2
        names = {item["name"] for item in parsed}
        assert "Alpha" in names
        assert "Beta" in names

    def test_to_json_fields(self, tmp_path):
        lib = make_library(tmp_path)
        frame = make_lib_frame(creator="Tom", name="Hello World", rating=5, created_at=1234.5)
        lib.add_frame(frame)

        result = lib.to_json()
        parsed = json.loads(result)
        item = parsed[0]

        assert item["frame_id"] == frame.frame_id
        assert item["creator"] == "Tom"
        assert item["name"] == "Hello World"
        assert item["rating"] == 5
        assert item["created_at"] == 1234.5
        assert item["characters"] == BLANK_CHARS

    def test_to_json_is_valid_json_string(self, tmp_path):
        lib = make_library(tmp_path)
        for i in range(10):
            lib.add_frame(make_lib_frame(name=f"Frame {i}", created_at=float(i)))
        json_str = lib.to_json()
        # Should not raise
        parsed = json.loads(json_str)
        assert len(parsed) == 10
