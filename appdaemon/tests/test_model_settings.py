"""Tests for model_settings loader, schema, and bundle resolution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.model_settings.loader import (
    clear_cache,
    load_bundle,
    load_provider_settings,
    resolve_capability_config,
)
from providers.ai_providers.model_settings.schema import BundleConfig, ProviderDefaults


def _resolve_secret(env_var: str) -> str:
    """Test double: returns test value for known env vars."""
    if env_var == "GEMINI_API_KEY":
        return "test-gemini-key"
    if env_var == "OPENAPI_TOKEN":
        return "test-openai-key"
    if env_var == "OLLAMA_URL":
        return "http://localhost:11434"
    if env_var == "COMFYUI_URL":
        return "https://comfyui.haynesops.com"
    raise ValueError(f"Unknown env var: {env_var}")


def test_load_provider_settings_gemini() -> None:
    settings = load_provider_settings("gemini")
    assert settings.provider == "gemini"
    assert settings.defaults.api_key_env == "GEMINI_API_KEY"
    assert settings.defaults.base_url == "https://generativelanguage.googleapis.com/v1beta"
    assert "gemini-default" in settings.bundles
    assert "gemini-sota" in settings.bundles
    assert "gemini-budget" in settings.bundles


def test_load_provider_settings_openai() -> None:
    settings = load_provider_settings("openai")
    assert settings.provider == "openai"
    assert settings.defaults.api_key_env == "OPENAPI_TOKEN"
    assert "openai-default" in settings.bundles
    assert "openai-budget" in settings.bundles
    assert "openai-sota" in settings.bundles


def test_load_provider_settings_ollama() -> None:
    settings = load_provider_settings("ollama")
    assert settings.provider == "ollama"
    assert settings.defaults.base_url_env == "OLLAMA_URL"
    assert "ollama-qwen9b" in settings.bundles


def test_load_provider_settings_comfyui() -> None:
    settings = load_provider_settings("comfyui")
    assert settings.provider == "comfyui"
    assert settings.defaults.base_url_env == "COMFYUI_URL"
    assert "comfyui-qwen-edit" in settings.bundles


def test_load_bundle_gemini_default() -> None:
    bundle = load_bundle("gemini-default")
    assert isinstance(bundle, BundleConfig)
    assert bundle.provider == "gemini"
    assert bundle.bundle_id == "gemini-default"
    assert bundle.multimodal_model == "gemini-2.5-flash"
    assert bundle.simple_text_model == "gemini-2.5-flash-lite"
    assert bundle.image_model == "gemini-3-pro-image-preview"
    assert bundle.image_prompt_augmentation is None


def test_load_bundle_openai_budget() -> None:
    bundle = load_bundle("openai-budget")
    assert bundle.provider == "openai"
    assert bundle.multimodal_model == "gpt-5.2"
    assert bundle.simple_text_model == "gpt-5-nano"
    assert bundle.image_model == "gpt-image-1.5"


def test_load_bundle_openai_default_uses_gpt52_for_simple_text() -> None:
    bundle = load_bundle("openai-default")
    assert bundle.provider == "openai"
    assert bundle.simple_text_model == "gpt-5.2"


def test_load_bundle_ollama_qwen9b() -> None:
    bundle = load_bundle("ollama-qwen9b")
    assert bundle.provider == "ollama"
    assert bundle.multimodal_model == "qwen3.5:9b"
    assert bundle.simple_text_model == "qwen3.5:9b"
    assert bundle.image_model is None


def test_load_bundle_comfyui_qwen_edit() -> None:
    bundle = load_bundle("comfyui-qwen-edit")
    assert bundle.provider == "comfyui"
    assert bundle.image_model == "qwen-image-edit-2509"
    assert bundle.provider_options["workflow_path"] == "workflows/02_qwen_Image_edit_subgraphed_API.json"


def test_load_bundle_not_found() -> None:
    with pytest.raises(ValueError) as exc_info:
        load_bundle("nonexistent-bundle")
    assert "not found" in str(exc_info.value).lower()


def test_resolve_capability_config_pointer_simple_text() -> None:
    conf = {"simple_text": "openai-budget"}
    flat = resolve_capability_config(conf, "simple_text", resolve_secret=_resolve_secret)
    assert flat["provider"] == "openai"
    assert flat["model"] == "gpt-5-nano"
    assert flat["api_key"] == "test-openai-key"


def test_resolve_capability_config_pointer_multimodal() -> None:
    conf = {"multimodal": "gemini-default"}
    flat = resolve_capability_config(conf, "multimodal", resolve_secret=_resolve_secret)
    assert flat["provider"] == "gemini"
    assert flat["model"] == "gemini-2.5-flash"
    assert flat["api_key"] == "test-gemini-key"
    assert flat.get("image_detail") or flat.get("media_resolution")


def test_resolve_capability_config_pointer_image() -> None:
    conf = {"image": "gemini-sota"}
    flat = resolve_capability_config(conf, "image", resolve_secret=_resolve_secret)
    assert flat["provider"] == "gemini"
    assert flat["model"] == "gemini-3.1-flash-image-preview"


def test_resolve_capability_config_pointer_image_comfyui() -> None:
    conf = {"image": "comfyui-qwen-edit"}
    flat = resolve_capability_config(conf, "image", resolve_secret=_resolve_secret)
    assert flat["provider"] == "comfyui"
    assert flat["model"] == "qwen-image-edit-2509"
    assert flat["base_url"] == "https://comfyui.haynesops.com"
    assert flat["provider_options"]["workflow_path"] == "workflows/02_qwen_Image_edit_subgraphed_API.json"


def test_resolve_capability_config_scoped_bundle_override() -> None:
    conf = {
        "image": {
            "bundle": "comfyui-qwen-edit",
            "base_url": "https://override.example.com",
        }
    }
    flat = resolve_capability_config(conf, "image", resolve_secret=_resolve_secret)
    assert flat["provider"] == "comfyui"
    assert flat["base_url"] == "https://override.example.com"
    assert flat["model"] == "qwen-image-edit-2509"


def test_resolve_capability_config_uses_any_nonempty_bundle_ref(monkeypatch) -> None:
    fake_bundle = BundleConfig(provider="openai", bundle_id="customref", simple_text_model="gpt-5-nano")
    monkeypatch.setattr(
        "providers.ai_providers.model_settings.loader.load_bundle",
        lambda ref: fake_bundle if ref == "customref" else (_ for _ in ()).throw(ValueError("bad ref")),
    )
    flat = resolve_capability_config({"simple_text": "customref"}, "simple_text", resolve_secret=_resolve_secret)
    assert flat["provider"] == "openai"
    assert flat["model"] == "gpt-5-nano"


def test_resolve_capability_config_pointer_image_ollama_raises() -> None:
    conf = {"image": "ollama-qwen9b"}
    with pytest.raises(ValueError) as exc_info:
        resolve_capability_config(conf, "image", resolve_secret=_resolve_secret)
    assert "ollama" in str(exc_info.value).lower()
    assert "image" in str(exc_info.value).lower()


def test_resolve_capability_config_inline() -> None:
    conf = {
        "provider": "openai",
        "api_key": "test-key",
        "multimodal_model": "gpt-5.2",
        "multimodal_image_detail": "high",
    }
    flat = resolve_capability_config(conf, "multimodal", resolve_secret=None)
    assert flat["provider"] == "openai"
    assert flat["model"] == "gpt-5.2"
    assert flat["image_detail"] == "high"
    assert flat["api_key"] == "test-key"


def test_resolve_capability_config_inline_legacy_data_keys() -> None:
    conf = {
        "provider": "openai",
        "api_key": "k",
        "data_model": "gpt-4o",
        "data_timeout_s": 60,
        "data_max_output_tokens": 300,
        "data_image_detail": "low",
    }
    flat = resolve_capability_config(conf, "multimodal", resolve_secret=None)
    assert flat["model"] == "gpt-4o"
    assert flat["timeout_s"] == 60
    assert flat["max_output_tokens"] == 300
    assert flat["image_detail"] == "low"


def test_provider_defaults_timeout_is_inherited_when_bundle_timeout_missing(tmp_path, monkeypatch) -> None:
    provider_yaml = tmp_path / "dummy.yaml"
    provider_yaml.write_text(
        """
defaults:
  timeout_s: 42
bundles:
  dummy-bundle:
    simple_text_model: gpt-5-nano
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("providers.ai_providers.model_settings.loader._MODEL_SETTINGS_DIR", tmp_path)
    clear_cache()
    bundle = load_bundle("dummy-bundle")
    assert bundle.simple_text_timeout_s == 42


def test_image_bundle_exposes_style_hooks() -> None:
    bundle = load_bundle("gemini-default")
    # Style hooks removed from bundle config — random selection happens at prompt build time
    assert bundle.style_profile is None


def test_clear_cache() -> None:
    load_provider_settings("gemini")
    clear_cache()
    # Should still work after cache clear
    bundle = load_bundle("gemini-default")
    assert bundle.provider == "gemini"
