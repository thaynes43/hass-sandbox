"""AI Message Generator automation app — uses LLM to generate witty board messages."""

from __future__ import annotations

import random
from typing import Any

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[4]))  # adds appdaemon/

import yaml

import hassapi as hass

from providers.vestaboard.character_encoding import (
    CHAR_TO_CODE,
    COLOR_CODES,
    COLS,
    text_to_grid,
)

from vestaboard_apps._shared.base import VestaboardAutomation

_AI_MESSAGE_PROMPT = """\
You write short messages for a split-flap display board in a family home's mudroom.

Your messages are warm, clever, and family-friendly. Vary the tone naturally: \
motivational quotes, fun facts, dad jokes, seasonal greetings, witty observations, \
or household reminders. Keep it fresh — avoid clichés.

## Board constraints (MANDATORY)
- The board is 22 characters wide and 6 rows tall.
- A colored border uses rows 1 and 6, and the first/last column of rows 2-5.
- That leaves a 20-character × 4-row text area for your message.
- Valid characters: A-Z, 0-9, space, and: ! @ # $ ( ) - + & = ; : ' " % , . / ?
- Use % for percentages (not "PCT"). Use & for "and" when space is tight.
- EVERY line must be 20 characters or fewer. Count carefully — this is critical.

## Message rules
- Write 1-4 SHORT lines of text. Each line must be 20 characters or fewer.
- Center your message visually — use fewer lines for punchy impact.
- ALL CAPS only (the board is uppercase).
- When given data (sensor values, etc.), incorporate it naturally into the message. \
Report the actual values — don't editorialize excessively.

## Border color
Choose a border color that matches the mood/sentiment of your message:
- "red" = alerts, warnings, urgent
- "orange" = caution, warm
- "yellow" = cheerful, sunny, attention
- "green" = good news, all-clear, nature
- "blue" = calm, informational, cool
- "violet" = creative, playful, whimsical

Return ONLY a JSON object: {"message": "YOUR MESSAGE HERE", "border_color": "COLOR"}
The message is plain text (1-4 lines separated by newlines). \
Do NOT include border characters — borders are added automatically.
"""


_VALID_BORDER_COLORS = {"red", "orange", "yellow", "green", "blue", "violet"}


def _build_bordered_grid(text: str, border_color: str | None = None) -> list[list[int]]:
    if border_color and border_color in _VALID_BORDER_COLORS:
        border_code = COLOR_CODES[border_color]
    else:
        border_code = random.choice([
            COLOR_CODES["red"], COLOR_CODES["orange"], COLOR_CODES["yellow"],
            COLOR_CODES["green"], COLOR_CODES["blue"], COLOR_CODES["violet"],
        ])

    grid = text_to_grid(text, justify="center", align="center")

    grid[0] = [border_code] * COLS
    grid[5] = [border_code] * COLS
    for row_idx in range(1, 5):
        grid[row_idx][0] = border_code
        grid[row_idx][21] = border_code

    return grid


class AiMessageGeneratorApp(hass.Hass, VestaboardAutomation):
    """On-demand automation that generates witty messages via LLM.

    Supports random interval scheduling when enabled with frequency config.
    """

    automation_type = "ai_message_generator"
    display_name = "AI Message Generator"
    display_description = "Uses AI to generate inspirational messages for your board."
    default_ttl_s = None
    default_max_age_s = None
    default_should_expire = True

    DEFAULT_UI_CONFIG = {
        "enabled": False,
        "ttl_minutes": 5,
        "should_expire": True,
        "frequency_min_minutes": 60,
        "frequency_max_minutes": 240,
    }

    _FALLBACK_MESSAGES = [
        "STAY CURIOUS\nSTAY CLEVER",
        "TODAY IS A GOOD DAY",
        "MAKE SOMETHING AMAZING",
        "ONE MOMENT AT A TIME",
        "YOU LOOK GREAT TODAY",
        "HAVE A WONDERFUL DAY",
        "EVERY DAY IS\nA FRESH START",
        "SOMETHING GOOD\nIS COMING",
        "BE KIND TO YOURSELF",
        "ENJOY THE LITTLE THINGS",
        "KEEP GOING\nYOU GOT THIS",
        "TAKE A DEEP BREATH",
        "GOOD THINGS TAKE TIME",
    ]

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "enabled": {"type": "bool", "label": "Enabled", "default": False},
            "ttl_minutes": {"type": "int", "label": "TTL (minutes)", "min": 1, "max": 1440, "default": 5},
            "should_expire": {"type": "bool", "label": "Should Expire", "default": True},
            "frequency_min_minutes": {"type": "int", "label": "Min Frequency (minutes)", "min": 1, "max": 1440, "default": 60},
            "frequency_max_minutes": {"type": "int", "label": "Max Frequency (minutes)", "min": 1, "max": 1440, "default": 240},
        }

    def get_preview_frame(self) -> list[list[int]]:
        border = COLOR_CODES["violet"]
        text = "AI MESSAGE"
        padded = text.center(20)
        interior: list[int] = []
        for ch in padded:
            interior.append(CHAR_TO_CODE.get(ch.upper(), 0))

        blank_interior = [0] * 20
        border_row = [border] * COLS

        def _bordered(cells: list[int]) -> list[int]:
            return [border] + cells + [border]

        return [
            border_row,
            _bordered(blank_interior),
            _bordered(interior),
            _bordered(blank_interior),
            _bordered(blank_interior),
            border_row,
        ]

    def initialize(self) -> None:
        self._bundles_path = (self.args or {}).get("prompt_data_bundles_path")
        self._bundles_missing_warned = False
        self.register_with_controller()
        # Do NOT start interval here — wait for config event from controller

    def terminate(self) -> None:
        self._cancel_random_interval()
        self.deregister_from_controller()

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self._start_random_interval()
        else:
            self._cancel_random_interval()

    def on_config_updated(self, config: dict[str, Any]) -> None:
        super().on_config_updated(config)
        if "enabled" in config:
            if config["enabled"]:
                self._start_random_interval()
            else:
                self._cancel_random_interval()
        elif "frequency_min_minutes" in config or "frequency_max_minutes" in config:
            if self.args.get("enabled", False):
                self._start_random_interval()

    def _start_random_interval(self) -> None:
        cfg = self.args or {}
        freq_min = cfg.get("frequency_min_minutes", 60)
        freq_max = cfg.get("frequency_max_minutes", 240)
        self._schedule_random_interval(
            self._on_random_fire,
            min_minutes=float(freq_min),
            max_minutes=float(freq_max),
        )

    def _on_random_fire(self, kwargs: dict) -> None:
        self._clear_random_interval_handle()  # handle already fired
        self.create_task(self._generate_and_push())
        self._start_random_interval()

    async def generate_frame(self, **kwargs) -> list[list[int]]:
        cfg = self.args or {}
        ai_provider_conf = cfg.get("ai_provider_conf")
        if ai_provider_conf:
            try:
                bundle = self._pick_bundle()
                return await self._generate_ai_frame(ai_provider_conf, bundle)
            except Exception as exc:
                self.log(f"AI generation failed, using fallback: {exc!r}", level="WARNING")

        return self._generate_fallback_frame()

    def _load_bundles(self) -> list[dict]:
        """Load prompt data bundles from the external YAML file.

        Re-reads every call (no caching). Returns [] on missing file or parse error.
        """
        if not self._bundles_path:
            return []

        try:
            with open(self._bundles_path, "r") as fh:
                raw = yaml.safe_load(fh)
        except FileNotFoundError:
            if not self._bundles_missing_warned:
                self.log(
                    f"Prompt data bundles file not found: {self._bundles_path}",
                    level="WARNING",
                )
                self._bundles_missing_warned = True
            return []
        except Exception as exc:
            self.log(
                f"Failed to read prompt data bundles file {self._bundles_path}: {exc!r}",
                level="WARNING",
            )
            return []

        if not isinstance(raw, list):
            self.log(
                f"Prompt data bundles file is not a YAML list: {type(raw).__name__}",
                level="WARNING",
            )
            return []

        valid: list[dict] = []
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict) or "description" not in entry:
                self.log(
                    f"Skipping malformed bundle entry at index {i}: "
                    f"must be a dict with a 'description' key",
                    level="WARNING",
                )
                continue
            valid.append(entry)

        self.log(
            f"Loaded {len(valid)} prompt data bundle(s) from {self._bundles_path}",
            level="INFO",
        )
        return valid

    def _pick_bundle(self) -> dict | None:
        """Randomly select a prompt data bundle, or return None if none available."""
        bundles = self._load_bundles()
        if not bundles:
            return None
        return random.choice(bundles)

    async def _build_bundle_prompt_section(self, bundle: dict) -> str:
        """Resolve entity values in the bundle and return a formatted prompt section."""
        from vestaboard_apps._shared.template_resolver import resolve_entities

        description = bundle.get("description", "")
        entities = bundle.get("entities", [])

        # Pre-resolve all entity states (get_state may return a coroutine in async context)
        state_cache: dict[str, str] = {}
        for ent in entities:
            eid = ent.get("entity_id", "")
            if eid:
                state = self.get_state(eid)
                if hasattr(state, "__await__"):
                    state = await state
                state_cache[eid] = str(state) if state is not None else None

        enriched = resolve_entities(entities, lambda eid: state_cache.get(eid))

        data_lines = [
            f"- {e.get('description', e.get('entity_id', ''))}: {e['current_value']}"
            for e in enriched
        ]

        self.log(
            f"Bundle selected: {description!r} | "
            f"Resolved entities: {[{e.get('entity_id'): e['current_value']} for e in enriched]}",
            level="INFO",
        )

        data_section = "\n".join(data_lines)
        return (
            f"For this message, write about: {description}. "
            f"Here is the current data:\n{data_section}"
        )

    async def _generate_ai_frame(
        self, ai_provider_conf: dict, bundle: dict | None = None
    ) -> list[list[int]]:
        from providers.ai_providers.registry import (
            build_simple_text_provider,
            simple_text_config_from_appdaemon_args,
        )

        provider_cfg = simple_text_config_from_appdaemon_args(
            {"ai_provider_conf": ai_provider_conf}
        )
        provider = build_simple_text_provider(provider_cfg)

        if bundle:
            bundle_section = await self._build_bundle_prompt_section(bundle)
            input_text = f"Write a board message.\n\n{bundle_section}"
            self.log("Generating AI message with data bundle...", level="INFO")
        else:
            input_text = "Write a board message."
            self.log("Generating AI message...", level="INFO")

        result = provider.generate_from_text(
            input_text=input_text,
            instructions=_AI_MESSAGE_PROMPT,
            expected_keys=["message"],
        )

        message_text = str(result.get("message", "")).strip()
        if not message_text:
            raise ValueError("AI returned empty message")

        border_color = str(result.get("border_color", "")).strip().lower()
        if border_color not in _VALID_BORDER_COLORS:
            border_color = None

        # Enforce 20-char line limit — truncate any lines that exceed it
        lines = message_text.split("\n")
        truncated = [line[:20] for line in lines[:4]]
        message_text = "\n".join(truncated)

        self.log(
            f"AI message generated: {message_text!r} border={border_color!r}",
            level="INFO",
        )

        return _build_bordered_grid(message_text, border_color=border_color)

    def _generate_fallback_frame(self) -> list[list[int]]:
        message = random.choice(self._FALLBACK_MESSAGES)
        self.log(f"Using fallback message: {message!r}", level="INFO")
        return _build_bordered_grid(message)
