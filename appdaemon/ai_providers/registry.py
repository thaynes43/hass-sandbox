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
    Parse the subset of an AppDaemon app's args that configure external image gen.
    """
    provider = ImageProviderName.parse(args.get("external_image_gen_provider", "openai"))
    timeout_raw = args.get("external_image_gen_timeout_s")
    timeout_s = float(timeout_raw) if timeout_raw is not None else None
    return ImageProviderConfig(
        provider=provider,
        api_key=args.get("external_image_gen_api_key"),
        base_url=args.get("external_image_gen_base_url"),
        model=args.get("external_image_gen_model"),
        size=args.get("external_image_gen_size"),
        quality=args.get("external_image_gen_quality"),
        output_format=args.get("external_image_gen_output_format"),
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
    Parse the subset of an AppDaemon app's args that configure external data gen.
    """
    provider = DataProviderName.parse(args.get("external_data_provider", "openai"))
    timeout_raw = args.get("external_data_timeout_s")
    timeout_s = float(timeout_raw) if timeout_raw is not None else None
    max_tokens_raw = args.get("external_data_max_output_tokens")
    max_output_tokens = int(max_tokens_raw) if max_tokens_raw is not None else None
    return DataProviderConfig(
        provider=provider,
        api_key=args.get("external_data_api_key") or args.get("external_image_gen_api_key"),
        base_url=args.get("external_data_base_url") or args.get("external_image_gen_base_url"),
        model=args.get("external_data_model"),
        timeout_s=timeout_s,
        max_output_tokens=max_output_tokens,
        image_detail=args.get("external_data_image_detail"),
    )

