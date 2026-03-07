"""Loader for model settings YAML files and capability bundle resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .schema import BundleConfig, ProviderDefaults, ProviderModelSettings

# Directory containing provider YAML files (sibling to this module)
_MODEL_SETTINGS_DIR = Path(__file__).resolve().parent

# In-memory cache of loaded provider settings
_provider_cache: Dict[str, ProviderModelSettings] = {}


def _load_yaml_file(path: Path) -> Dict[str, Any]:
    """Load a YAML file. Uses PyYAML if available."""
    try:
        import yaml
    except ImportError:
        raise ImportError(
            "PyYAML is required for model_settings. Install with: pip install pyyaml"
        ) from None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _parse_defaults(raw: Dict[str, Any], provider: str) -> ProviderDefaults:
    """Parse defaults section from provider YAML."""
    return ProviderDefaults(
        api_key_env=raw.get("api_key_env"),
        base_url=raw.get("base_url"),
        base_url_env=raw.get("base_url_env") or raw.get("base_url_secret"),
        timeout_s=float(raw["timeout_s"]) if raw.get("timeout_s") is not None else None,
    )


def _parse_bundle(
    provider: str, bundle_id: str, raw: Dict[str, Any], defaults: ProviderDefaults
) -> BundleConfig:
    """Parse a single bundle from provider YAML."""
    return BundleConfig(
        provider=provider,
        bundle_id=bundle_id,
        multimodal_model=raw.get("multimodal_model"),
        simple_text_model=raw.get("simple_text_model"),
        image_model=raw.get("image_model"),
        multimodal_max_output_tokens=raw.get("multimodal_max_output_tokens"),
        simple_text_max_output_tokens=raw.get("simple_text_max_output_tokens"),
        multimodal_media_resolution=raw.get("multimodal_media_resolution"),
        multimodal_image_detail=raw.get("multimodal_image_detail"),
        multimodal_timeout_s=(
            float(raw["multimodal_timeout_s"])
            if raw.get("multimodal_timeout_s") is not None
            else defaults.timeout_s
        ),
        simple_text_timeout_s=(
            float(raw["simple_text_timeout_s"])
            if raw.get("simple_text_timeout_s") is not None
            else defaults.timeout_s
        ),
        image_timeout_s=(
            float(raw["image_timeout_s"]) if raw.get("image_timeout_s") is not None else defaults.timeout_s
        ),
        image_size=raw.get("image_size"),
        image_quality=raw.get("image_quality"),
        image_output_format=raw.get("image_output_format"),
        image_prompt_augmentation=raw.get("image_prompt_augmentation"),
        style_profile=raw.get("style_profile"),
        style_variant_profile=raw.get("style_variant_profile"),
        provider_options=dict(raw.get("provider_options") or {}),
        api_key_env=defaults.api_key_env,
        base_url=defaults.base_url,
        base_url_env=defaults.base_url_env,
    )


def load_provider_settings(provider: str) -> ProviderModelSettings:
    """Load model settings for a provider from YAML. Results are cached."""
    provider_lower = (provider or "").strip().lower()
    if provider_lower in _provider_cache:
        return _provider_cache[provider_lower]

    path = _MODEL_SETTINGS_DIR / f"{provider_lower}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Model settings not found for provider: {provider}")

    raw = _load_yaml_file(path)
    defaults_raw = raw.get("defaults") or {}
    defaults = _parse_defaults(defaults_raw, provider_lower)

    bundles: Dict[str, BundleConfig] = {}
    for bid, braw in (raw.get("bundles") or {}).items():
        if isinstance(braw, dict):
            bundles[bid] = _parse_bundle(provider_lower, bid, braw, defaults)

    settings = ProviderModelSettings(provider=provider_lower, defaults=defaults, bundles=bundles)
    _provider_cache[provider_lower] = settings
    return settings


def load_bundle(bundle_ref: str) -> BundleConfig:
    """
    Resolve a bundle reference to a BundleConfig.

    Bundle refs are either:
    - "provider-bundleid" (e.g. "gemini-default", "openai-budget")
    - "bundleid" when unique (loader checks all providers)

    Returns the first matching bundle. Raises if not found.
    """
    ref = (bundle_ref or "").strip()
    if not ref:
        raise ValueError("Bundle reference is empty")

    # Try "provider-bundleid" format first
    if "-" in ref:
        parts = ref.split("-", 1)
        provider = parts[0].strip().lower()
        bundle_id = parts[1].strip()
        if provider and bundle_id:
            try:
                settings = load_provider_settings(provider)
                if bundle_id in settings.bundles:
                    return settings.bundles[bundle_id]
            except FileNotFoundError:
                pass

    # Fallback: search all provider files for a matching bundle_id
    for yaml_path in _MODEL_SETTINGS_DIR.glob("*.yaml"):
        provider_name = yaml_path.stem.lower()
        if provider_name in ("__init__", "schema", "loader"):
            continue
        try:
            settings = load_provider_settings(provider_name)
            if ref in settings.bundles:
                return settings.bundles[ref]
        except (FileNotFoundError, Exception):
            continue

    raise ValueError(f"Bundle not found: {bundle_ref!r}")


def resolve_capability_config(
    ai_provider_conf: Dict[str, Any],
    capability: str,
    resolve_secret: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    """
    Resolve ai_provider_conf to a flat config dict for the given capability.

    Supports two modes:
    1. Capability-pointer: ai_provider_conf = { simple_text: "openai-budget", ... }
       Resolves the ref and returns merged bundle + defaults.
    2. Inline: ai_provider_conf = { provider: "openai", api_key_env: "...", ... }
       Returns the inline config as-is (backward compatibility).

    capability: "simple_text" | "multimodal" | "image"
    resolve_secret: optional callable(env_var_name) -> str for api_key/base_url resolution
    """
    conf = ai_provider_conf or {}
    if not isinstance(conf, dict):
        return {}

    # Map capability to the key used in pointer config
    pointer_key = capability
    bundle_ref = conf.get(pointer_key)

    # If the capability points at a string, treat it as a bundle ref first.
    if isinstance(bundle_ref, str) and bundle_ref.strip():
        ref = bundle_ref.strip()
        try:
            bundle = load_bundle(ref)
        except (ValueError, FileNotFoundError):
            raise ValueError(
                f"Capability {capability!r} references unknown bundle {ref!r}"
            ) from None
        else:
            return _bundle_to_flat_config(bundle, capability, resolve_secret)

    # Capability-scoped dict config:
    #   simple_text:
    #     bundle: ollama-qwen9b
    #     base_url: !secret ollama_url
    if isinstance(bundle_ref, dict):
        scoped = dict(bundle_ref)
        ref = str(scoped.get("bundle") or scoped.get("ref") or "").strip()
        if ref:
            try:
                bundle = load_bundle(ref)
            except (ValueError, FileNotFoundError):
                raise ValueError(
                    f"Capability {capability!r} references unknown bundle {ref!r}"
                ) from None
            flat = _bundle_to_flat_config(bundle, capability, resolve_secret)
            return _merge_capability_overrides(flat, scoped, capability)
        return _inline_to_flat_config(scoped, capability, resolve_secret)

    # Inline config path (backward compatibility)
    return _inline_to_flat_config(conf, capability, resolve_secret)


def _resolve_secret_safe(
    env_var: Optional[str], resolve_secret: Optional[Callable[[str], str]]
) -> Optional[str]:
    """Resolve secret from env var if resolver provided."""
    if not env_var or not resolve_secret:
        return None
    try:
        return resolve_secret(env_var)
    except Exception:
        return None


def _bundle_to_flat_config(
    bundle: BundleConfig,
    capability: str,
    resolve_secret: Optional[Callable[[str], str]],
) -> Dict[str, Any]:
    """Convert BundleConfig to flat dict for registry consumption."""
    provider = bundle.provider

    # Base URLs: prefer direct, then env
    base_url = bundle.base_url
    if not base_url and bundle.base_url_env and resolve_secret:
        base_url = _resolve_secret_safe(bundle.base_url_env, resolve_secret)
    if not base_url:
        # Defaults from provider
        defaults_map = {
            "openai": "https://api.openai.com",
            "gemini": "https://generativelanguage.googleapis.com/v1beta",
            "ollama": "http://localhost:11434",
            "comfyui": "http://localhost:8188",
        }
        base_url = defaults_map.get(provider, "")

    api_key = _resolve_secret_safe(bundle.api_key_env, resolve_secret)

    result: Dict[str, Any] = {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
    }

    if capability == "simple_text":
        result["model"] = bundle.simple_text_model
        result["timeout_s"] = bundle.simple_text_timeout_s
        result["max_output_tokens"] = bundle.simple_text_max_output_tokens
    elif capability == "multimodal":
        result["model"] = bundle.multimodal_model
        result["timeout_s"] = bundle.multimodal_timeout_s
        result["max_output_tokens"] = bundle.multimodal_max_output_tokens
        # OpenAI uses image_detail; Gemini uses media_resolution (adapter maps)
        result["image_detail"] = bundle.multimodal_image_detail or bundle.multimodal_media_resolution
        result["media_resolution"] = bundle.multimodal_media_resolution
    elif capability == "image":
        if provider == "ollama":
            raise ValueError(
                "Provider ollama does not support image generation in this project. "
                "Use a gemini or openai bundle for image capability."
            )
        result["model"] = bundle.image_model
        result["timeout_s"] = bundle.image_timeout_s
        result["size"] = bundle.image_size
        result["quality"] = bundle.image_quality
        result["output_format"] = bundle.image_output_format
        result["image_prompt_augmentation"] = bundle.image_prompt_augmentation
        result["style_profile"] = bundle.style_profile
        result["style_variant_profile"] = bundle.style_variant_profile
        result["provider_options"] = dict(bundle.provider_options)

    return result


def _inline_to_flat_config(
    conf: Dict[str, Any],
    capability: str,
    resolve_secret: Optional[Callable[[str], str]],
) -> Dict[str, Any]:
    """Convert inline ai_provider_conf to flat config (backward compatibility)."""
    api_key = conf.get("api_key")
    if not api_key and conf.get("api_key_env") and resolve_secret:
        api_key = _resolve_secret_safe(str(conf.get("api_key_env")), resolve_secret)

    result: Dict[str, Any] = {
        "provider": conf.get("provider", "openai"),
        "api_key": api_key,
        "base_url": conf.get("base_url"),
    }

    if capability == "simple_text":
        result["model"] = conf.get("simple_text_model") or conf.get("data_model")
        result["timeout_s"] = conf.get("simple_text_timeout_s") or conf.get("data_timeout_s")
        result["max_output_tokens"] = (
            conf.get("simple_text_max_output_tokens") or conf.get("data_max_output_tokens")
        )
    elif capability == "multimodal":
        result["model"] = conf.get("multimodal_model") or conf.get("data_model")
        result["timeout_s"] = conf.get("multimodal_timeout_s") or conf.get("data_timeout_s")
        result["max_output_tokens"] = (
            conf.get("multimodal_max_output_tokens") or conf.get("data_max_output_tokens")
        )
        result["image_detail"] = (
            conf.get("multimodal_image_detail") or conf.get("data_image_detail")
        )
        result["media_resolution"] = conf.get("multimodal_media_resolution")
    elif capability == "image":
        result["model"] = conf.get("image_model")
        result["timeout_s"] = conf.get("image_timeout_s")
        result["size"] = conf.get("image_size")
        result["quality"] = conf.get("image_quality")
        result["output_format"] = conf.get("image_output_format")
        result["image_prompt_augmentation"] = conf.get("image_prompt_augmentation")
        result["provider_options"] = dict(conf.get("provider_options") or {})

    return result


def _merge_capability_overrides(
    base: Dict[str, Any],
    overrides: Dict[str, Any],
    capability: str,
) -> Dict[str, Any]:
    """Merge capability-scoped overrides onto a resolved bundle config."""
    merged = dict(base)

    common_keys = ("provider", "api_key", "base_url")
    for key in common_keys:
        if overrides.get(key) is not None:
            merged[key] = overrides.get(key)

    if capability == "simple_text":
        for key in ("model", "timeout_s", "max_output_tokens"):
            if overrides.get(key) is not None:
                merged[key] = overrides.get(key)
    elif capability == "multimodal":
        for key in ("model", "timeout_s", "max_output_tokens", "image_detail", "media_resolution"):
            if overrides.get(key) is not None:
                merged[key] = overrides.get(key)
    elif capability == "image":
        for key in (
            "model",
            "timeout_s",
            "size",
            "quality",
            "output_format",
            "image_prompt_augmentation",
            "style_profile",
            "style_variant_profile",
        ):
            if overrides.get(key) is not None:
                merged[key] = overrides.get(key)
        if overrides.get("provider_options") is not None:
            merged["provider_options"] = dict(overrides.get("provider_options") or {})

    return merged


def clear_cache() -> None:
    """Clear the provider settings cache (for tests)."""
    _provider_cache.clear()
