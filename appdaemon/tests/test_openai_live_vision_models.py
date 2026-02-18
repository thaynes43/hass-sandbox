from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure `appdaemon/` is on sys.path so `ai_providers.*` imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_providers.registry import build_data_provider, data_provider_config_from_appdaemon_args


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


@pytest.mark.skipif(
    not _env("AI_PROVIDER_KEY") or not _env("DS_TEST_IMAGE_PATH"),
    reason="requires AI_PROVIDER_KEY and DS_TEST_IMAGE_PATH (path to a real JPG/PNG snapshot)",
)
@pytest.mark.parametrize("model", ["gpt-5-mini", "gpt-5.2"])
def test_openai_live_vision_models_return_json(model: str) -> None:
    img = Path(_env("DS_TEST_IMAGE_PATH"))
    assert img.exists() and img.is_file(), f"DS_TEST_IMAGE_PATH does not exist: {img}"

    args = {
        "ai_provider_conf": {
            "provider": "openai",
            "api_key": _env("AI_PROVIDER_KEY"),
            "base_url": _env("AI_PROVIDER_BASE_URL") or "https://api.openai.com",
            "data_model": model,
            "data_timeout_s": float(_env("DS_DATA_TIMEOUT_S") or "60"),
            # Keep this at the default we use in prod unless explicitly overridden.
            "data_max_output_tokens": int(_env("DS_DATA_MAX_OUTPUT_TOKENS") or "300"),
            "data_image_detail": _env("DS_DATA_IMAGE_DETAIL") or "auto",
        }
    }

    provider = build_data_provider(data_provider_config_from_appdaemon_args(args))

    # Keep instructions close to the production intent: short summary + counts + scores.
    # The goal is to validate that the model returns a valid JSON object for the schema
    # the detection summary app depends on (including gender counts).
    instructions = (
        "You are analyzing ONE security camera snapshot for a push notification.\n"
        "Focus ONLY on the people and any animals.\n"
        "Return ONLY a JSON object, no extra text.\n"
        "Summary must be 1 sentence <= 140 characters.\n"
        "Return male_count and female_count as integer counts by gender (0 if none).\n"
        "Provide integer-ish 0-10 scores to rank frames.\n"
    )
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

    try:
        data = provider.generate_data_from_image(
            input_image_path=str(img),
            instructions=instructions,
            expected_keys=expected_keys,
        )
    except Exception as e:
        pytest.fail(f"live OpenAI call failed for model={model!r}: {e!r}")

    assert isinstance(data, dict)
    missing = [k for k in expected_keys if k not in data]
    assert not missing, f"model={model!r} missing keys: {missing}; keys={sorted(list(data.keys()))[:50]}"

    # Basic sanity: summary should be a string (may be empty in edge cases, but usually not)
    assert isinstance(data.get("summary"), (str, type(None)))

