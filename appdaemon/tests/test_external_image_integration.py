"""
Optional integration test that hits an external image provider (OpenAI) directly.

Staged workflow:
1) Run detection_summary to produce /media/.../runs/<run_id>/best.jpg (via HA camera.snapshot)
2) Run this test to generate /media/.../runs/<run_id>/generated.png from best.jpg
3) (Optional) Create a local_file camera in HA pointing at generated.png, then run your
   existing HA WS tests to attach it to notifications, etc.

This test is skipped unless RUN_EXTERNAL_IMAGE_TESTS=1.
"""

import os
from pathlib import Path

import pytest


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default)).strip()


def _load_secrets() -> dict:
    """
    AppDaemon secrets (`appdaemon/secrets.yaml`) are NOT automatically exported as environment
    variables for pytest. This helper lets you run staged integration tests without manually
    exporting AI_PROVIDER_KEY every time.

    Only used as a fallback when env vars are not set.
    """
    secrets_path = Path(__file__).resolve().parents[1] / "secrets.yaml"
    if not secrets_path.exists():
        return {}

    try:
        import yaml  # type: ignore

        data = yaml.safe_load(secrets_path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        # very small fallback parser for `key: "value"` lines
        out: dict[str, str] = {}
        for line in secrets_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
        return out


def _resolve_media_path(p: Path) -> Path:
    """
    If you pass an HA-style path like `/media/...` but your dev mount is elsewhere
    (e.g. WSL `/mnt/...`), set HA_MEDIA_FS_ROOT to map it.

    Example:
      HA_MEDIA_FS_ROOT=/mnt/cephfs-hdd/misc/hass-media
      EXTERNAL_IMAGE_INPUT_PATH=/media/detection-summary/garage/runs/<run_id>/best.jpg
    """
    if p.exists():
        return p
    root = _env("HA_MEDIA_FS_ROOT") or _env("MEDIA_FS_ROOT")
    if not root:
        return p
    try:
        # Only rewrite HA-style /media paths
        if str(p).startswith("/media/") or str(p) == "/media":
            rel = str(p)[len("/media/") :] if str(p).startswith("/media/") else ""
            mapped = Path(root) / rel
            return mapped
    except Exception:
        return p
    return p


@pytest.mark.skipif(_env("RUN_EXTERNAL_IMAGE_TESTS") != "1", reason="set RUN_EXTERNAL_IMAGE_TESTS=1 to run")
def test_external_openai_image_edit_writes_png() -> None:
    import sys

    # Make `appdaemon/` importable for `ai_providers.*`
    sys.path.append(str(Path(__file__).resolve().parents[1]))

    from ai_providers.openai_provider import OpenAIImageEditConfig, OpenAIImageProvider

    secrets = _load_secrets()
    api_key = _env("AI_PROVIDER_KEY") or str(secrets.get("openapi_token") or secrets.get("ai_provider_key") or "")
    assert api_key, "Set AI_PROVIDER_KEY (or set openapi_token in appdaemon/secrets.yaml)"

    input_path = _resolve_media_path(Path(_env("EXTERNAL_IMAGE_INPUT_PATH")))
    assert input_path.exists(), f"Set EXTERNAL_IMAGE_INPUT_PATH to an existing image; got {input_path}"

    output_path_s = _env("EXTERNAL_IMAGE_OUTPUT_PATH")
    output_path = _resolve_media_path(Path(output_path_s)) if output_path_s else (input_path.parent / "generated.png")

    prompt = _env(
        "EXTERNAL_IMAGE_PROMPT",
        "Create a simple, clean illustration that represents this security camera scene. Keep it unobtrusive.",
    )

    cfg = OpenAIImageEditConfig(
        api_key=api_key,
        base_url=_env("OPENAI_BASE_URL", "https://api.openai.com"),
        model=_env("OPENAI_IMAGE_MODEL", "gpt-image-1.5"),
        size=_env("OPENAI_IMAGE_SIZE", "1024x1024"),
        quality=_env("OPENAI_IMAGE_QUALITY", "medium"),
        output_format=_env("OPENAI_IMAGE_FORMAT", "png"),
        timeout_s=float(_env("OPENAI_TIMEOUT_S", "120")),
    )

    provider = OpenAIImageProvider(cfg)
    meta = provider.edit_image(input_image_path=str(input_path), prompt=prompt, output_image_path=str(output_path))

    assert output_path.exists()
    assert output_path.stat().st_size > 0
    assert output_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert meta["provider"] == "openai"

