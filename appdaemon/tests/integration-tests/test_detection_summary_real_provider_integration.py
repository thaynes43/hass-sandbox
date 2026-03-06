from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps"))

from providers.ai_providers.registry import (
    build_image_provider,
    build_multimodal_text_provider,
    multimodal_text_config_from_appdaemon_args,
    provider_config_from_appdaemon_args,
)
from detection_summary_app.selection import ScoreResult, adaptive_select_and_score


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


@pytest.mark.skipif(
    _env("RUN_REAL_PROVIDER_INTEGRATION_TESTS") != "1" or not _env("AI_PROVIDER_KEY") or not _env("DS_TEST_FRAMES_DIR"),
    reason="set RUN_REAL_PROVIDER_INTEGRATION_TESTS=1, AI_PROVIDER_KEY, and DS_TEST_FRAMES_DIR to run",
)
def test_real_provider_scores_and_generates_image_edit_smoke():
    frames_dir = Path(_env("DS_TEST_FRAMES_DIR"))
    assert frames_dir.exists() and frames_dir.is_dir()

    src_frames = sorted([p for p in frames_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"}])
    assert src_frames, "DS_TEST_FRAMES_DIR must contain at least one image"

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        # Normalize filenames so the selector code can address frame_{idx:03d}.jpg
        for i, src in enumerate(src_frames[:12]):
            dst = work / f"frame_{i:03d}.jpg"
            shutil.copyfile(src, dst)

        args = {
            "ai_provider_conf": {
                "provider": "openai",
                "api_key": _env("AI_PROVIDER_KEY"),
                "multimodal_model": _env("DS_DATA_MODEL") or "gpt-5.2",
                "multimodal_timeout_s": float(_env("DS_DATA_TIMEOUT_S") or "60"),
                "multimodal_max_output_tokens": int(_env("DS_DATA_MAX_OUTPUT_TOKENS") or "300"),
                "multimodal_image_detail": _env("DS_DATA_IMAGE_DETAIL") or "low",
                "image_model": _env("DS_IMAGE_MODEL") or "gpt-image-1.5",
                "image_timeout_s": float(_env("DS_IMAGE_TIMEOUT_S") or "90"),
            }
        }

        data_provider = build_multimodal_text_provider(multimodal_text_config_from_appdaemon_args(args))
        image_provider = build_image_provider(provider_config_from_appdaemon_args(args))

        instructions = _env("DS_DATA_INSTRUCTIONS") or (
            "You are analyzing ONE security camera snapshot for a push notification.\n"
            "Focus ONLY on the people and any animals.\n"
            "Return ONLY a JSON object, no extra text.\n"
            "Summary must be 1 sentence <= 140 characters.\n"
            "Return male_count, female_count, and animal_count as integer counts (0 if none).\n"
            "Provide integer-ish 0-10 scores to rank frames."
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

        def score(i: int) -> ScoreResult:
            img = work / f"frame_{i:03d}.jpg"
            data = data_provider.generate_from_image(
                input_image_path=str(img),
                instructions=instructions,
                expected_keys=expected_keys,
            )
            male_count = int(data.get("male_count", 0) or 0)
            female_count = int(data.get("female_count", 0) or 0)
            animal_count = int(data.get("animal_count", 0) or 0)
            person = float(data.get("person_score", 0) or 0)
            face = float(data.get("face_score", 0) or 0)
            frame = float(data.get("frame_score", person) or person)
            pose = str(data.get("pose") or "")
            summary = str(data.get("summary") or "")
            return ScoreResult(
                male_count,
                female_count,
                animal_count,
                person,
                face,
                frame,
                pose,
                summary,
                data,
            )

        total = len(list(work.glob("frame_*.jpg")))
        scored, meta = adaptive_select_and_score(
            total_frames=total,
            budget=min(6, total),
            score_index=score,
            seed="integration-seed",
            no_people_threshold=1.0,
        )

        assert scored, "expected at least one scored frame"
        best_idx = meta.best_idx
        best = work / f"frame_{best_idx:03d}.jpg"
        assert best.exists()

        prompt = _env("DS_IMAGE_PROMPT") or "Create a simple illustrative image of the detected person(s)."
        out = work / "generated.png"
        res = image_provider.edit_image(
            input_image_paths=[str(best)],
            prompt=prompt,
            output_image_path=str(out),
        )
        assert out.exists() and out.stat().st_size > 0
        assert isinstance(res, dict)
