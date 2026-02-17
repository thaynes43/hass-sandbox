from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ai_providers.ollama_data_provider import OllamaDataProvider
from ai_providers.ollama_provider import OllamaImageProvider
from ai_providers.openai_data_provider import OpenAIChatVisionDataConfig, OpenAIDataProvider
from ai_providers.openai_provider import OpenAIImageEditConfig, OpenAIImageProvider
from ai_providers.types import DataProvider, DataProviderName, ImageProvider, ImageProviderName


@dataclass(frozen=True)
class ImageProviderConfig:
    provider: ImageProviderName

    # Shared-ish config knobs (not all providers will use these)
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    size: Optional[str] = None
    quality: Optional[str] = None
    output_format: Optional[str] = None
    timeout_s: Optional[float] = None


def build_image_provider(cfg: ImageProviderConfig) -> ImageProvider:
    if cfg.provider == ImageProviderName.OPENAI:
        return OpenAIImageProvider(
            OpenAIImageEditConfig(
                api_key=str(cfg.api_key or ""),
                base_url=str(cfg.base_url or "https://api.openai.com"),
                model=str(cfg.model or "gpt-image-1.5"),
                size=str(cfg.size or "1024x1024"),
                quality=str(cfg.quality or "medium"),
                output_format=str(cfg.output_format or "png"),
                timeout_s=float(cfg.timeout_s or 90.0),
            )
        )

    if cfg.provider == ImageProviderName.OLLAMA:
        return OllamaImageProvider(base_url=str(cfg.base_url or "http://localhost:11434"))

    raise ValueError(f"Unsupported provider: {cfg.provider}")


def provider_config_from_appdaemon_args(args: dict[str, Any]) -> ImageProviderConfig:
    """
    Parse `ai_provider_conf` from an AppDaemon app's args for image generation.
    """
    conf = args.get("ai_provider_conf") or {}
    if not isinstance(conf, dict):
        conf = {}

    provider = ImageProviderName.parse(conf.get("provider", "openai"))
    timeout_raw = conf.get("image_timeout_s")
    timeout_s = float(timeout_raw) if timeout_raw is not None else None
    return ImageProviderConfig(
        provider=provider,
        api_key=conf.get("api_key"),
        base_url=conf.get("base_url"),
        model=conf.get("image_model"),
        size=conf.get("image_size"),
        quality=conf.get("image_quality"),
        output_format=conf.get("image_output_format"),
        timeout_s=timeout_s,
    )


@dataclass(frozen=True)
class DataProviderConfig:
    provider: DataProviderName

    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    timeout_s: Optional[float] = None
    max_output_tokens: Optional[int] = None
    image_detail: Optional[str] = None


def build_data_provider(cfg: DataProviderConfig) -> DataProvider:
    if cfg.provider == DataProviderName.OPENAI:
        return OpenAIDataProvider(
            OpenAIChatVisionDataConfig(
                api_key=str(cfg.api_key or ""),
                base_url=str(cfg.base_url or "https://api.openai.com"),
                model=str(cfg.model or "gpt-5.2"),
                timeout_s=float(cfg.timeout_s or 60.0),
                max_output_tokens=int(cfg.max_output_tokens or 300),
                image_detail=str(cfg.image_detail or "low"),
            )
        )

    if cfg.provider == DataProviderName.OLLAMA:
        return OllamaDataProvider(base_url=str(cfg.base_url or "http://localhost:11434"))

    raise ValueError(f"Unsupported provider: {cfg.provider}")


def data_provider_config_from_appdaemon_args(args: dict[str, Any]) -> DataProviderConfig:
    """
    Parse `ai_provider_conf` from an AppDaemon app's args for vision/text data generation.
    """
    conf = args.get("ai_provider_conf") or {}
    if not isinstance(conf, dict):
        conf = {}

    provider = DataProviderName.parse(conf.get("provider", "openai"))
    timeout_raw = conf.get("data_timeout_s")
    timeout_s = float(timeout_raw) if timeout_raw is not None else None
    max_tokens_raw = conf.get("data_max_output_tokens")
    max_output_tokens = int(max_tokens_raw) if max_tokens_raw is not None else None
    return DataProviderConfig(
        provider=provider,
        api_key=conf.get("api_key"),
        base_url=conf.get("base_url"),
        model=conf.get("data_model"),
        timeout_s=timeout_s,
        max_output_tokens=max_output_tokens,
        image_detail=conf.get("data_image_detail"),
    )

