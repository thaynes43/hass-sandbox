"""Capability-specific AI provider builders and config parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from providers import secrets

from .model_settings.loader import resolve_capability_config

from .image_generation_provider import (
    ImageGenerationProvider,
    ImageProviderName,
)
from .multimodal_text_provider import MultimodalTextProvider, MultimodalProviderName
from .simple_text_provider import SimpleTextProvider, SimpleTextProviderName
from .provider_settings import (
    validate_image_model,
    validate_multimodal_model,
    validate_simple_text_model,
)

# Image generation
from .openai.openai_image_generation_provider import (
    OpenAIImageEditConfig,
    OpenAIImageGenerationProvider,
)
from .gemini.gemini_image_generation_provider import (
    GeminiImageGenerationConfig,
    GeminiImageGenerationProvider,
)
from .comfyui.comfyui_image_generation_provider import (
    ComfyUIImageGenerationConfig,
    ComfyUIImageGenerationProvider,
)
from .ollama.ollama_image_generation_provider import OllamaImageGenerationProvider

# Multimodal
from .openai.openai_multimodal_text_provider import (
    OpenAIMultimodalConfig,
    OpenAIMultimodalTextProvider,
)
from .gemini.gemini_multimodal_text_provider import (
    GeminiMultimodalConfig,
    GeminiMultimodalTextProvider,
)
from .ollama.ollama_multimodal_text_provider import (
    OllamaMultimodalConfig,
    OllamaMultimodalTextProvider,
)

# Simple text
from .openai.openai_simple_text_provider import (
    OpenAISimpleTextConfig,
    OpenAISimpleTextProvider,
)
from .gemini.gemini_simple_text_provider import (
    GeminiSimpleTextConfig,
    GeminiSimpleTextProvider,
)
from .ollama.ollama_simple_text_provider import (
    OllamaSimpleTextConfig,
    OllamaSimpleTextProvider,
)


# --- Image generation ---

@dataclass(frozen=True)
class ImageProviderConfig:
    provider: ImageProviderName
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    size: Optional[str] = None
    quality: Optional[str] = None
    output_format: Optional[str] = None
    timeout_s: Optional[float] = None
    image_prompt_augmentation: Optional[str] = None
    style_profile: Optional[str] = None
    style_variant_profile: Optional[str] = None
    provider_options: Optional[dict[str, Any]] = None


def build_image_provider(cfg: ImageProviderConfig) -> ImageGenerationProvider:
    if cfg.provider == ImageProviderName.OPENAI:
        ok, err = validate_image_model("openai", cfg.model or "gpt-image-1.5")
        if not ok and err:
            raise ValueError(err)
        return OpenAIImageGenerationProvider(
            OpenAIImageEditConfig(
                api_key=_require_api_key("openai", cfg.api_key),
                base_url=str(cfg.base_url or "https://api.openai.com"),
                model=str(cfg.model or "gpt-image-1.5"),
                size=str(cfg.size or "1536x1024"),
                quality=str(cfg.quality or "medium"),
                output_format=str(cfg.output_format or "png"),
                timeout_s=float(cfg.timeout_s or 90.0),
            )
        )

    if cfg.provider == ImageProviderName.GEMINI:
        ok, err = validate_image_model("gemini", cfg.model or "gemini-2.5-flash-image")
        if not ok and err:
            raise ValueError(err)
        return GeminiImageGenerationProvider(
            GeminiImageGenerationConfig(
                api_key=_require_api_key("gemini", cfg.api_key),
                base_url=str(cfg.base_url or "https://generativelanguage.googleapis.com/v1beta"),
                model=str(cfg.model or "gemini-2.5-flash-image"),
                timeout_s=float(cfg.timeout_s or 90.0),
            )
        )

    if cfg.provider == ImageProviderName.OLLAMA:
        ok, err = validate_image_model("ollama", cfg.model or "")
        if not ok and err:
            raise ValueError(err)
        return OllamaImageGenerationProvider(base_url=str(cfg.base_url or "http://localhost:11434"))

    if cfg.provider == ImageProviderName.COMFYUI:
        model = str(cfg.model or "qwen-image-edit-2509")
        ok, err = validate_image_model("comfyui", model)
        if not ok and err:
            raise ValueError(err)
        options = dict(cfg.provider_options or {})
        workflow_path = str(
            options.get("workflow_path")
            or "workflows/02_qwen_Image_edit_subgraphed_API.json"
        )
        return ComfyUIImageGenerationProvider(
            ComfyUIImageGenerationConfig(
                base_url=str(cfg.base_url or "http://localhost:8188"),
                workflow_path=workflow_path,
                model=model,
                timeout_s=float(cfg.timeout_s or 300.0),
                prompt_node_id=str(options.get("prompt_node_id") or "115:111"),
                negative_prompt_node_id=(
                    str(options["negative_prompt_node_id"])
                    if options.get("negative_prompt_node_id") is not None
                    else "115:110"
                ),
                load_image_node_id=str(options.get("load_image_node_id") or "78"),
                save_image_node_id=str(options.get("save_image_node_id") or "60"),
                sampler_node_id=(
                    str(options["sampler_node_id"])
                    if options.get("sampler_node_id") is not None
                    else "115:3"
                ),
                sampler_seed_input=str(options.get("sampler_seed_input") or "seed"),
                filename_prefix=str(options.get("filename_prefix") or "detection-summary"),
                upload_overwrite=bool(options.get("upload_overwrite", True)),
                poll_interval_s=float(options.get("poll_interval_s") or 1.0),
                provider_options=options,
                sampler_cfg=(
                    float(options["sampler_cfg"])
                    if options.get("sampler_cfg") is not None
                    else None
                ),
                sampler_steps=(
                    int(options["sampler_steps"])
                    if options.get("sampler_steps") is not None
                    else None
                ),
                sampler_denoise=(
                    float(options["sampler_denoise"])
                    if options.get("sampler_denoise") is not None
                    else None
                ),
                scale_node_id=(
                    str(options["scale_node_id"])
                    if options.get("scale_node_id") is not None
                    else None
                ),
                scale_megapixels=(
                    float(options["scale_megapixels"])
                    if options.get("scale_megapixels") is not None
                    else None
                ),
                min_input_pixels=(
                    int(options["min_input_pixels"])
                    if options.get("min_input_pixels") is not None
                    else None
                ),
            )
        )

    raise ValueError(f"Unsupported image provider: {cfg.provider}")


def _resolve_api_key(conf: dict[str, Any]) -> Optional[str]:
    """Resolve API key from api_key_env or direct api_key (for tests)."""
    if conf.get("api_key"):
        return str(conf["api_key"])
    api_key_env = conf.get("api_key_env")
    if api_key_env:
        return secrets.resolve_secret(api_key_env)
    return None


def _require_api_key(provider: str, api_key: Optional[str]) -> str:
    value = str(api_key or "").strip()
    if value:
        return value
    raise ValueError(
        f"Provider {provider!r} requires a configured API key. "
        "Set ai_provider_conf.api_key_env to an environment variable name."
    )


def provider_config_from_appdaemon_args(args: dict[str, Any]) -> ImageProviderConfig:
    """Parse ai_provider_conf for image generation.

    Supports capability-pointer refs (e.g. image: gemini-sota) and inline config.
    """
    conf = args.get("ai_provider_conf") or {}
    if not isinstance(conf, dict):
        conf = {}

    flat = resolve_capability_config(conf, "image", resolve_secret=secrets.resolve_secret)
    provider = ImageProviderName.parse(flat.get("provider", "openai"))
    timeout_s = flat.get("timeout_s")
    if timeout_s is not None:
        timeout_s = float(timeout_s)

    return ImageProviderConfig(
        provider=provider,
        api_key=flat.get("api_key"),
        base_url=flat.get("base_url"),
        model=flat.get("model"),
        size=flat.get("size"),
        quality=flat.get("quality"),
        output_format=flat.get("output_format"),
        timeout_s=timeout_s,
        image_prompt_augmentation=flat.get("image_prompt_augmentation"),
        style_profile=flat.get("style_profile"),
        style_variant_profile=flat.get("style_variant_profile"),
        provider_options=dict(flat.get("provider_options") or {}),
    )


# --- Multimodal text ---

@dataclass(frozen=True)
class MultimodalTextProviderConfig:
    provider: MultimodalProviderName
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    timeout_s: Optional[float] = None
    max_output_tokens: Optional[int] = None
    image_detail: Optional[str] = None


def build_multimodal_text_provider(cfg: MultimodalTextProviderConfig) -> MultimodalTextProvider:
    if cfg.provider == MultimodalProviderName.OPENAI:
        model = cfg.model or "gpt-5.2"
        ok, err = validate_multimodal_model("openai", model)
        if not ok and err:
            raise ValueError(err)
        return OpenAIMultimodalTextProvider(
            OpenAIMultimodalConfig(
                api_key=_require_api_key("openai", cfg.api_key),
                base_url=str(cfg.base_url or "https://api.openai.com"),
                model=model,
                timeout_s=float(cfg.timeout_s or 60.0),
                max_output_tokens=int(cfg.max_output_tokens or 300),
                image_detail=str(cfg.image_detail or "low"),
            )
        )

    if cfg.provider == MultimodalProviderName.GEMINI:
        model = cfg.model or "gemini-2.5-flash"
        ok, err = validate_multimodal_model("gemini", model)
        if not ok and err:
            raise ValueError(err)
        return GeminiMultimodalTextProvider(
            GeminiMultimodalConfig(
                api_key=_require_api_key("gemini", cfg.api_key),
                base_url=str(cfg.base_url or "https://generativelanguage.googleapis.com/v1beta"),
                model=model,
                timeout_s=float(cfg.timeout_s or 60.0),
                max_output_tokens=int(cfg.max_output_tokens or 300),
                image_detail=str(cfg.image_detail or "low"),
            )
        )

    if cfg.provider == MultimodalProviderName.OLLAMA:
        model = cfg.model or "qwen3.5:9b"
        ok, err = validate_multimodal_model("ollama", model)
        if not ok and err:
            raise ValueError(err)
        return OllamaMultimodalTextProvider(
            OllamaMultimodalConfig(
                base_url=str(cfg.base_url or "http://localhost:11434"),
                model=model,
                timeout_s=float(cfg.timeout_s or 300.0),
                max_output_tokens=int(cfg.max_output_tokens or 300),
            )
        )

    raise ValueError(f"Unsupported multimodal provider: {cfg.provider}")


def multimodal_text_config_from_appdaemon_args(args: dict[str, Any]) -> MultimodalTextProviderConfig:
    """Parse ai_provider_conf for multimodal (image+text) generation.

    Supports capability-pointer refs (e.g. multimodal: gemini-default) and inline config.
    """
    conf = args.get("ai_provider_conf") or {}
    if not isinstance(conf, dict):
        conf = {}

    flat = resolve_capability_config(conf, "multimodal", resolve_secret=secrets.resolve_secret)
    provider = MultimodalProviderName.parse(flat.get("provider", "openai"))
    timeout_s = flat.get("timeout_s")
    if timeout_s is not None:
        timeout_s = float(timeout_s)
    max_output_tokens = flat.get("max_output_tokens")
    if max_output_tokens is not None:
        max_output_tokens = int(max_output_tokens)
    image_detail = flat.get("image_detail") or flat.get("media_resolution")

    return MultimodalTextProviderConfig(
        provider=provider,
        api_key=flat.get("api_key"),
        base_url=flat.get("base_url"),
        model=flat.get("model"),
        timeout_s=timeout_s,
        max_output_tokens=max_output_tokens,
        image_detail=image_detail,
    )


# --- Simple text ---

@dataclass(frozen=True)
class SimpleTextProviderConfig:
    provider: SimpleTextProviderName
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    timeout_s: Optional[float] = None
    max_output_tokens: Optional[int] = None


def build_simple_text_provider(cfg: SimpleTextProviderConfig) -> SimpleTextProvider:
    if cfg.provider == SimpleTextProviderName.OPENAI:
        model = str(cfg.model or "gpt-5.2")
        ok, err = validate_simple_text_model("openai", model)
        if not ok and err:
            raise ValueError(err)
        return OpenAISimpleTextProvider(
            OpenAISimpleTextConfig(
                api_key=_require_api_key("openai", cfg.api_key),
                base_url=str(cfg.base_url or "https://api.openai.com"),
                model=model,
                timeout_s=float(cfg.timeout_s or 60.0),
                max_output_tokens=int(cfg.max_output_tokens or 300),
            )
        )

    if cfg.provider == SimpleTextProviderName.GEMINI:
        model = str(cfg.model or "gemini-2.5-flash-lite")
        ok, err = validate_simple_text_model("gemini", model)
        if not ok and err:
            raise ValueError(err)
        return GeminiSimpleTextProvider(
            GeminiSimpleTextConfig(
                api_key=_require_api_key("gemini", cfg.api_key),
                base_url=str(cfg.base_url or "https://generativelanguage.googleapis.com/v1beta"),
                model=model,
                timeout_s=float(cfg.timeout_s or 60.0),
                max_output_tokens=int(cfg.max_output_tokens or 300),
            )
        )

    if cfg.provider == SimpleTextProviderName.OLLAMA:
        return OllamaSimpleTextProvider(
            OllamaSimpleTextConfig(
                base_url=str(cfg.base_url or "http://localhost:11434"),
                model=str(cfg.model or "qwen3.5:9b"),
                timeout_s=float(cfg.timeout_s or 300.0),
                max_output_tokens=int(cfg.max_output_tokens or 300),
            )
        )

    raise ValueError(f"Unsupported simple-text provider: {cfg.provider}")


def simple_text_config_from_appdaemon_args(args: dict[str, Any]) -> SimpleTextProviderConfig:
    """Parse ai_provider_conf for simple text-to-JSON generation.

    Supports capability-pointer refs (e.g. simple_text: openai-budget) and inline config.
    """
    conf = args.get("ai_provider_conf") or {}
    if not isinstance(conf, dict):
        conf = {}

    flat = resolve_capability_config(conf, "simple_text", resolve_secret=secrets.resolve_secret)
    provider = SimpleTextProviderName.parse(flat.get("provider", "openai"))
    timeout_s = flat.get("timeout_s")
    if timeout_s is not None:
        timeout_s = float(timeout_s)
    max_output_tokens = flat.get("max_output_tokens")
    if max_output_tokens is not None:
        max_output_tokens = int(max_output_tokens)

    return SimpleTextProviderConfig(
        provider=provider,
        api_key=flat.get("api_key"),
        base_url=flat.get("base_url"),
        model=flat.get("model"),
        timeout_s=timeout_s,
        max_output_tokens=max_output_tokens,
    )


# --- Backward compatibility aliases ---

@dataclass(frozen=True)
class DataProviderConfig:
    """Legacy config; use MultimodalTextProviderConfig / SimpleTextProviderConfig instead."""
    provider: MultimodalProviderName  # type: ignore[assignment]
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    timeout_s: Optional[float] = None
    max_output_tokens: Optional[int] = None
    image_detail: Optional[str] = None


def build_data_provider(cfg: DataProviderConfig) -> MultimodalTextProvider:
    """Legacy builder; returns multimodal provider. Prefer build_multimodal_text_provider."""
    return build_multimodal_text_provider(
        MultimodalTextProviderConfig(
            provider=cfg.provider,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            model=cfg.model,
            timeout_s=cfg.timeout_s,
            max_output_tokens=cfg.max_output_tokens,
            image_detail=cfg.image_detail,
        )
    )


def data_provider_config_from_appdaemon_args(args: dict[str, Any]) -> DataProviderConfig:
    """Legacy config; returns config suitable for build_data_provider (multimodal)."""
    m = multimodal_text_config_from_appdaemon_args(args)
    return DataProviderConfig(
        provider=m.provider,
        api_key=m.api_key,
        base_url=m.base_url,
        model=m.model,
        timeout_s=m.timeout_s,
        max_output_tokens=m.max_output_tokens,
        image_detail=m.image_detail,
    )
