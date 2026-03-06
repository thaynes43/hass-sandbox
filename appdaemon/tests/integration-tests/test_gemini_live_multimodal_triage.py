from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Ensure `appdaemon/` is on sys.path so `providers.ai_providers.*` imports work
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from providers.ai_providers.registry import (  # noqa: E402
    build_multimodal_text_provider,
    multimodal_text_config_from_appdaemon_args,
)


DEFAULT_CAPTURED_FRAMES_DIR = (
    "/mnt/cephfs-hdd/misc/hass-media/detection-summary/bulkhead/runs/"
    "682c0d6a-7ebf-4327-aefa-f62675b9e442/captured"
)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _bulkhead_triage_instructions() -> str:
    # Match the detection_summary_bulkhead_dev prompt as closely as possible,
    # including the additional schema constraints appended in manager.py.
    base = (
        "You are analyzing ONE security camera snapshot from a basement bulkhead entrance.\n"
        "Focus ONLY on the people and any animals (ignore the environment/vehicles/objects\n"
        "unless it is directly relevant).\n"
        "The summary should be short and notification-friendly: 1 sentence, <= 140 characters.\n"
        "Include only: person/animal count, what they are doing, and where they are in\n"
        'frame (e.g. "near left", "center", "back").\n'
        "Do NOT describe or name any branded merchandise, collectible figurines, licensed\n"
        'characters, or copyrighted items visible in the background. Treat them as generic\n'
        '"decorative items" or "shelf items" if you must reference them at all.\n'
        "Also return male_count and female_count as integer counts of people by gender\n"
        "(0 if none).\n"
        "Also return animal_count as an integer count of visible animals/pets (0 if none).\n"
        "Return scores on a 0-10 scale (integers preferred) with enough spread to rank\n"
        "frames.\n"
        "Heavily favor frames where at least one person's FACE is clearly visible (front/3-4\n"
        "view, not fully occluded).\n"
        "If no face is visible, the frame should score significantly lower than a similar\n"
        "frame with a visible face.\n"
        "face_score meaning: 0=no face visible, 10=clear unobstructed face (ideally looking\n"
        "toward camera).\n"
        "Prefer frames where the person is clearly visible and stationary (standing/sitting),\n"
        "not partially occluded or blurred in motion.\n"
        "If animals are present and clearly visible, increase frame_score appropriately."
    )
    extra = (
        "\n\nAdditional required fields:\n"
        "- male_count: integer count of men/boys visible in the frame (0 if none)\n"
        "- female_count: integer count of women/girls visible in the frame (0 if none)\n"
        "- animal_count: integer count of animals/pets visible in the frame (0 if none)\n"
        "\nScoring guidance:\n"
        "- If animals are present and clearly visible, increase frame_score appropriately.\n"
        "- face_score can be influenced by clearly visible faces of people OR animals.\n"
        "- pose should describe the main subject (person or animal) when possible.\n"
        "- summary should mention animals when they are relevant, but remain 1 sentence <= 140 characters.\n"
    )
    return f"{base}{extra}"


@pytest.mark.skipif(
    _env("RUN_GEMINI_TRIAGE_TESTS") != "1" or not _env("GEMINI_API_KEY"),
    reason="set RUN_GEMINI_TRIAGE_TESTS=1 and GEMINI_API_KEY to run manual Gemini live triage",
)
def test_gemini_live_multimodal_bulkhead_frames_triage() -> None:
    """
    Manual triage test for Gemini multimodal scoring against preserved bulkhead frames.

    Defaults to the preserved failed-run frames directory from the recent debugging session.
    Override with:
    - GEMINI_TRIAGE_FRAMES_DIR
    - GEMINI_TRIAGE_MODEL
    - GEMINI_TRIAGE_TIMEOUT_S
    - GEMINI_TRIAGE_MAX_OUTPUT_TOKENS
    - GEMINI_TRIAGE_MAX_FRAMES
    """

    frames_dir = Path(_env("GEMINI_TRIAGE_FRAMES_DIR", DEFAULT_CAPTURED_FRAMES_DIR))
    if not frames_dir.exists() or not frames_dir.is_dir():
        pytest.skip(f"triage frames dir does not exist: {frames_dir}")

    frames = sorted([p for p in frames_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"}])
    if not frames:
        pytest.skip(f"triage frames dir has no images: {frames_dir}")

    max_frames_raw = _env("GEMINI_TRIAGE_MAX_FRAMES")
    max_frames = int(max_frames_raw) if max_frames_raw else len(frames)
    selected_frames = frames[: max(1, min(len(frames), max_frames))]

    args = {
        "ai_provider_conf": {
            "provider": "gemini",
            "api_key": _env("GEMINI_API_KEY"),
            "multimodal_model": _env("GEMINI_TRIAGE_MODEL", "gemini-2.5-flash"),
            "multimodal_timeout_s": float(_env("GEMINI_TRIAGE_TIMEOUT_S", "60")),
            "multimodal_max_output_tokens": int(_env("GEMINI_TRIAGE_MAX_OUTPUT_TOKENS", "300")),
            "multimodal_image_detail": _env("GEMINI_TRIAGE_IMAGE_DETAIL", "low"),
        }
    }

    provider = build_multimodal_text_provider(multimodal_text_config_from_appdaemon_args(args))
    instructions = _bulkhead_triage_instructions()
    expected_keys = [
        "male_count",
        "female_count",
        "animal_count",
        "person_score",
        "face_score",
        "frame_score",
        "pose",
        "summary",
    ]

    successes: list[dict[str, object]] = []
    failures: list[str] = []

    for frame in selected_frames:
        try:
            data = provider.generate_from_image(
                input_image_path=str(frame),
                instructions=instructions,
                expected_keys=expected_keys,
            )
            payload = {
                "frame": frame.name,
                "keys": sorted(list(data.keys())),
                "summary": str(data.get("summary") or ""),
                "meta": data.get("_meta") if isinstance(data, dict) else None,
            }
            successes.append(payload)
            print(f"[gemini-triage] success {frame.name}: {json.dumps(payload, default=str)}")
        except Exception as exc:
            msg = f"{frame.name}: {exc!r}"
            failures.append(msg)
            print(f"[gemini-triage] failure {msg}")

    assert successes, "expected at least one successful Gemini multimodal result"
    if failures:
        pytest.fail(
            "Gemini live triage had failures.\n"
            f"frames_dir={frames_dir}\n"
            f"model={args['ai_provider_conf']['multimodal_model']}\n"
            + "\n".join(failures)
        )
