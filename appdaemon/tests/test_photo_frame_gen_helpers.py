"""Unit tests for photo_frame_viewer.gen_helpers (pure-Python helpers)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mock hassapi before importing anything from the app package
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from photo_frame_viewer.gen_helpers import (
    basename,
    build_label_maps,
    compute_fingerprint,
    gen_path_to_local_url,
    parse_gen_id_from_url,
    source_paths_to_gen_paths,
)


class TestComputeFingerprint:
    def test_deterministic(self):
        paths = ["/config/www/a/img1.jpg", "/config/www/a/img2.jpg"]
        assert compute_fingerprint(paths) == compute_fingerprint(paths)

    def test_order_independent(self):
        a = ["/config/www/a/img1.jpg", "/config/www/a/img2.jpg"]
        b = ["/config/www/a/img2.jpg", "/config/www/a/img1.jpg"]
        assert compute_fingerprint(a) == compute_fingerprint(b)

    def test_changes_on_different_input(self):
        a = ["/config/www/a/img1.jpg"]
        b = ["/config/www/a/img2.jpg"]
        assert compute_fingerprint(a) != compute_fingerprint(b)

    def test_empty_list(self):
        fp = compute_fingerprint([])
        assert isinstance(fp, str) and len(fp) == 64


class TestBuildLabelMaps:
    def test_simple_labels(self):
        paths = ["/a/foo.jpg", "/b/bar.png"]
        labels, l2p, p2l = build_label_maps(paths)
        assert labels == ["foo.jpg", "bar.png"]
        assert l2p["foo.jpg"] == "/a/foo.jpg"
        assert p2l["/b/bar.png"] == "bar.png"

    def test_disambiguation(self):
        paths = ["/a/pic.jpg", "/b/pic.jpg", "/c/pic.jpg"]
        labels, l2p, _ = build_label_maps(paths)
        assert labels == ["pic.jpg", "pic.jpg (2)", "pic.jpg (3)"]
        assert l2p["pic.jpg"] == "/a/pic.jpg"
        assert l2p["pic.jpg (2)"] == "/b/pic.jpg"

    def test_options_max(self):
        paths = [f"/a/{i}.jpg" for i in range(200)]
        labels, _, _ = build_label_maps(paths, options_max=5)
        assert len(labels) == 5


class TestSourcePathsToGenPaths:
    def test_basic_mapping(self):
        src = ["/config/www/immich-album/foo.jpg", "/config/www/immich-album/bar.png"]
        result = source_paths_to_gen_paths(src, "2")
        assert result == [
            "/config/www/photo-frame/live/2/foo.jpg",
            "/config/www/photo-frame/live/2/bar.png",
        ]

    def test_custom_live_dir(self):
        src = ["/somewhere/pic.jpg"]
        result = source_paths_to_gen_paths(src, "7", live_ha_dir="/config/www/custom")
        assert result == ["/config/www/custom/7/pic.jpg"]


class TestGenPathToLocalUrl:
    def test_basic(self):
        url = gen_path_to_local_url(
            "/config/www/photo-frame/live/2/foo.jpg",
            ha_local_url_base="/local/photo-frame/live",
            gen_id="2",
        )
        assert url == "/local/photo-frame/live/2/foo.jpg"

    def test_space_encoding(self):
        url = gen_path_to_local_url(
            "/config/www/photo-frame/live/3/my photo.jpg",
            ha_local_url_base="/local/photo-frame/live",
            gen_id="3",
        )
        assert url == "/local/photo-frame/live/3/my%20photo.jpg"

    def test_special_chars(self):
        url = gen_path_to_local_url(
            "/config/www/photo-frame/live/1/DSC00993 (2).JPG",
            ha_local_url_base="/local/photo-frame/live",
            gen_id="1",
        )
        assert "DSC00993%20%282%29.JPG" in url

    def test_fallback_no_gen_id(self):
        url = gen_path_to_local_url(
            "/config/www/immich-album/pic.jpg",
            ha_local_url_base="/local/photo-frame/live",
            gen_id="",
        )
        assert url == "/local/immich-album/pic.jpg"


class TestParseGenIdFromUrl:
    def test_extracts_gen_id(self):
        url = "/local/photo-frame/live/3/IMG_2322.JPG"
        assert parse_gen_id_from_url(url) == "3"

    def test_large_gen_id(self):
        url = "/local/photo-frame/live/42/pic.jpg"
        assert parse_gen_id_from_url(url) == "42"

    def test_no_match_wrong_prefix(self):
        url = "/local/immich-album/pic.jpg"
        assert parse_gen_id_from_url(url) is None

    def test_no_match_empty(self):
        assert parse_gen_id_from_url("") is None
        assert parse_gen_id_from_url(None) is None  # type: ignore[arg-type]

    def test_encoded_filename(self):
        url = "/local/photo-frame/live/5/my%20photo.jpg"
        assert parse_gen_id_from_url(url) == "5"
