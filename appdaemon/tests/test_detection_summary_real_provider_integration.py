from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from ai_providers.registry import (
    build_data_provider,
    build_image_provider,
    data_provider_config_from_appdaemon_args,
    provider_config_from_appdaemon_args,
)
from detection_summary_app.selection import ScoreResult, adaptive_select_and_score


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


@pytest.mark.skipif(
    not _env("AI_PROVIDER_KEY") or not _env("DS_TEST_FRAMES_DIR"),
    reason="requires AI_PROVIDER_KEY and DS_TEST_FRAMES_DIR (directory of real JPG frames)",
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
            "external_data_provider": "openai",
            "external_data_api_key": _env("AI_PROVIDER_KEY"),
            "external_data_model": _env("DS_DATA_MODEL") or "gpt-5.2",
            "external_data_timeout_s": float(_env("DS_DATA_TIMEOUT_S") or "60"),
            "external_data_max_output_tokens": int(_env("DS_DATA_MAX_OUTPUT_TOKENS") or "300"),
            "external_data_image_detail": _env("DS_DATA_IMAGE_DETAIL") or "low",
            "external_image_gen_provider": "openai",
            "external_image_gen_api_key": _env("AI_PROVIDER_KEY"),
            "external_image_gen_model": _env("DS_IMAGE_MODEL") or "gpt-image-1.5",
            "external_image_gen_timeout_s": float(_env("DS_IMAGE_TIMEOUT_S") or "90"),
        }

        data_provider = build_data_provider(data_provider_config_from_appdaemon_args(args))
        image_provider = build_image_provider(provider_config_from_appdaemon_args(args))

        instructions = _env("DS_DATA_INSTRUCTIONS") or (
            "You are analyzing ONE security camera snapshot for a push notification.\n"
            "Focus ONLY on the people.\n"
            "Return JSON with keys: person_score, face_score, frame_score, pose, summary.\n"
            "Scores are 0-10. Heavily favor clear, unobstructed faces."
        )
        expected_keys = ["person_score", "face_score", "frame_score", "pose", "summary"]

        def score(i: int) -> ScoreResult:
            img = work / f"frame_{i:03d}.jpg"
            data = data_provider.generate_data_from_image(
                input_image_path=str(img),
                instructions=instructions,
                expected_keys=expected_keys,
            )
            person = float(data.get("person_score", 0) or 0)
            face = float(data.get("face_score", 0) or 0)
            frame = float(data.get("frame_score", person) or person)
            pose = str(data.get("pose") or "")
            summary = str(data.get("summary") or "")
            return ScoreResult(person, face, frame, pose, summary, data)

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
            input_image_path=str(best),
            prompt=prompt,
            output_image_path=str(out),
        )
        assert out.exists() and out.stat().st_size > 0
        assert isinstance(res, dict)

