"""
Integration tests (optional) that call Home Assistant AI Task services.

These are NOT unit tests and are skipped by default.

Enable by setting:
  RUN_HA_INTEGRATION_TESTS=1

Auth/config resolution:
  - Prefer env vars: HA_URL, HA_TOKEN
  - Fallback: read from appdaemon/appdaemon.yaml (ha_url) and appdaemon/secrets.yaml (token)

Optional: Use a saved snapshot file via a local_file camera:
  - LOCAL_FILE_CAMERA_ENTITY_ID: e.g. camera.photo_frame_image
  - LOCAL_FILE_CAMERA_PATH: e.g. /config/www/detection-summary/garage/buffer/slot_00.jpg

If not set, we attach the live camera:
  - CAMERA_ENTITY_ID (default: camera.garage_g5_dome_medium_resolution_channel)
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp
import pytest
import yaml


def _read_ha_url_from_appdaemon_yaml(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    # Avoid YAML parsing because appdaemon.yaml uses !secret.
    text = path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"^\s*ha_url:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'")


def _read_token_from_secrets_yaml(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        token = data.get("token")
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


def _get_ha_config() -> tuple[str, str]:
    ha_url = os.environ.get("HA_URL")
    ha_token = os.environ.get("HA_TOKEN")

    repo_root = Path(__file__).resolve().parents[2]  # .../appdaemon/tests -> repo root
    appdaemon_yaml = repo_root / "appdaemon" / "appdaemon.yaml"
    secrets_yaml = repo_root / "appdaemon" / "secrets.yaml"

    if not ha_url:
        ha_url = _read_ha_url_from_appdaemon_yaml(appdaemon_yaml)
    if not ha_token:
        ha_token = _read_token_from_secrets_yaml(secrets_yaml)

    if not ha_url or not ha_token:
        raise RuntimeError(
            "Missing HA_URL/HA_TOKEN env vars and couldn't read appdaemon/appdaemon.yaml + secrets.yaml"
        )
    return ha_url.rstrip("/"), ha_token


async def _ha_ws_call(
    session: aiohttp.ClientSession,
    ws: aiohttp.ClientWebSocketResponse,
    msg: dict[str, Any],
    *,
    timeout_s: float,
) -> dict[str, Any]:
    await ws.send_json(msg)
    start = time.time()
    while True:
        remaining = timeout_s - (time.time() - start)
        if remaining <= 0:
            raise asyncio.TimeoutError(f"Timed out waiting for response to id={msg.get('id')}")
        incoming = await ws.receive(timeout=remaining)
        if incoming.type == aiohttp.WSMsgType.TEXT:
            data = incoming.json()
            if isinstance(data, dict) and data.get("type") == "result" and data.get("id") == msg.get("id"):
                return data
        elif incoming.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            raise RuntimeError(f"Websocket closed while waiting for id={msg.get('id')}")


@pytest.mark.skipif(os.environ.get("RUN_HA_INTEGRATION_TESTS") != "1", reason="set RUN_HA_INTEGRATION_TESTS=1")
@pytest.mark.asyncio
async def test_ai_task_generate_data_and_image():
    ha_url, token = _get_ha_config()
    ws_url = f"{ha_url}/api/websocket"

    timeout_s = float(os.environ.get("HA_WS_TIMEOUT_S", "120"))

    ai_task_data_entity_id = os.environ.get("AI_TASK_DATA_ENTITY_ID", "ai_task.openai_ai_task")
    ai_task_image_entity_id = os.environ.get("AI_TASK_IMAGE_ENTITY_ID", ai_task_data_entity_id)
    # Default off because many HA installs don't have local media configured yet,
    # and ai_task.generate_image persists output to the first media directory.
    run_generate_image = os.environ.get("RUN_GENERATE_IMAGE", "0") == "1"

    camera_entity_id = os.environ.get("CAMERA_ENTITY_ID", "camera.garage_g5_dome_medium_resolution_channel")
    attachment_media_id = f"media-source://camera/{camera_entity_id}"
    attachment_media_type = "image/jpeg"

    local_file_camera = os.environ.get("LOCAL_FILE_CAMERA_ENTITY_ID")
    local_file_path = os.environ.get("LOCAL_FILE_CAMERA_PATH")

    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(ws_url)
        try:
            # auth_required
            first = await ws.receive(timeout=timeout_s)
            assert first.type == aiohttp.WSMsgType.TEXT
            first_data = first.json()
            assert first_data.get("type") == "auth_required"

            await ws.send_json({"type": "auth", "access_token": token})
            auth_resp = await ws.receive(timeout=timeout_s)
            assert auth_resp.type == aiohttp.WSMsgType.TEXT
            assert auth_resp.json().get("type") == "auth_ok"

            msg_id = 1

            # Optional: point a local_file camera at a saved snapshot so we can test without live capture.
            if local_file_camera and local_file_path:
                upd = {
                    "id": msg_id,
                    "type": "call_service",
                    "domain": "local_file",
                    "service": "update_file_path",
                    "target": {"entity_id": local_file_camera},
                    "service_data": {"file_path": local_file_path},
                }
                upd_res = await _ha_ws_call(session, ws, upd, timeout_s=timeout_s)
                assert upd_res.get("success") is True
                attachment_media_id = f"media-source://camera/{local_file_camera}"
                msg_id += 1

            # --- generate_data ---
            gen_data = {
                "id": msg_id,
                "type": "call_service",
                "domain": "ai_task",
                "service": "generate_data",
                "return_response": True,
                "service_data": {
                    "entity_id": ai_task_data_entity_id,
                    "task_name": "integration test detection summary",
                    "instructions": "Return JSON with score 0-10 and a 1-sentence summary.",
                    "structure": {
                        "score": {"selector": {"number": {"min": 0, "max": 10}}},
                        "summary": {"selector": {"text": {}}},
                    },
                    "attachments": [
                        {
                            "media_content_id": attachment_media_id,
                            "media_content_type": attachment_media_type,
                        }
                    ],
                },
            }
            data_res = await _ha_ws_call(session, ws, gen_data, timeout_s=timeout_s)
            assert data_res.get("success") is True
            response = (data_res.get("result") or {}).get("response") or {}
            out = response.get("data") or {}
            assert isinstance(out, dict)
            assert "score" in out and "summary" in out
            msg_id += 1

            # --- generate_image ---
            if not run_generate_image:
                return

            gen_img = {
                "id": msg_id,
                "type": "call_service",
                "domain": "ai_task",
                "service": "generate_image",
                "return_response": True,
                "service_data": {
                    "entity_id": ai_task_image_entity_id,
                    "task_name": "integration test image",
                    "instructions": "Generate a simple illustrative image based on the attachment.",
                    "attachments": [
                        {
                            "media_content_id": attachment_media_id,
                            "media_content_type": attachment_media_type,
                        }
                    ],
                },
            }
            img_res = await _ha_ws_call(session, ws, gen_img, timeout_s=timeout_s)
            assert img_res.get("success") is True
            img_response = (img_res.get("result") or {}).get("response") or {}
            assert "media_source_id" in img_response
        finally:
            await ws.close()

