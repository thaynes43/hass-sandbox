from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.secrets import resolve_arg_secret


def test_resolve_arg_secret_prefers_env_pointer():
    with patch("providers.secrets.resolve_secret", return_value="https://ha.example.com") as mock_resolve:
        value = resolve_arg_secret(
            {"ha_url": "http://legacy", "ha_url_env": "HA_URL"},
            "ha_url",
        )

    assert value == "https://ha.example.com"
    mock_resolve.assert_called_once_with("HA_URL")


def test_resolve_arg_secret_falls_back_to_direct_value():
    assert (
        resolve_arg_secret({"media_fs_root": "/mnt/media"}, "media_fs_root")
        == "/mnt/media"
    )


def test_resolve_arg_secret_returns_default_when_missing():
    assert resolve_arg_secret({}, "media_fs_root", default="/media") == "/media"


def test_resolve_arg_secret_raises_when_required():
    with pytest.raises(ValueError, match="Required config 'ha_url' is missing"):
        resolve_arg_secret({}, "ha_url", required=True)
