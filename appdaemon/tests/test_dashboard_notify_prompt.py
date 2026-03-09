"""Tests for dashboard_notify prompt builder."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.style_variants import (
    StyleProfile,
    EnvironmentVariant,
)
from dashboard_notify.prompt_builder import (
    build_notification_prompt,
    build_placeholder_prompt,
)


class TestBuildNotificationPrompt:
    def test_basic_prompt_structure(self):
        prompt = build_notification_prompt(
            notification_text="Time to catch the bus!",
            prompt_hint="children at a bus stop",
            image_class="BasicTextImage",
            style_profile=StyleProfile(id="default", prompt_suffix=""),
            environment_variant=EnvironmentVariant(id="default", prompt_suffix=""),
        )
        assert "16:9" in prompt
        assert "Time to catch the bus!" in prompt
        assert "children at a bus stop" in prompt
        assert "wall display" in prompt.lower()

    def test_includes_class_modifier(self):
        prompt = build_notification_prompt(
            notification_text="Alert!",
            prompt_hint="warning scene",
            image_class="UrgentImage",
            style_profile=StyleProfile(id="default", prompt_suffix=""),
            environment_variant=EnvironmentVariant(id="default", prompt_suffix=""),
        )
        assert "bold" in prompt.lower() or "attention" in prompt.lower()

    def test_fun_class_modifier(self):
        prompt = build_notification_prompt(
            notification_text="No snacks!",
            prompt_hint="closed kitchen",
            image_class="FunPictureImage",
            style_profile=StyleProfile(id="default", prompt_suffix=""),
            environment_variant=EnvironmentVariant(id="default", prompt_suffix=""),
        )
        assert "whimsical" in prompt.lower() or "playful" in prompt.lower()

    def test_style_profile_appended(self):
        style = StyleProfile(
            id="cartoon",
            prompt_suffix="Render in cartoon style.",
        )
        prompt = build_notification_prompt(
            notification_text="Test",
            prompt_hint="scene",
            image_class="BasicTextImage",
            style_profile=style,
            environment_variant=EnvironmentVariant(id="default", prompt_suffix=""),
        )
        assert "Render in cartoon style." in prompt

    def test_environment_variant_appended(self):
        env = EnvironmentVariant(
            id="space",
            prompt_suffix="Set in outer space.",
        )
        prompt = build_notification_prompt(
            notification_text="Test",
            prompt_hint="scene",
            image_class="BasicTextImage",
            style_profile=StyleProfile(id="default", prompt_suffix=""),
            environment_variant=env,
        )
        assert "Set in outer space." in prompt

    def test_content_safety_included(self):
        prompt = build_notification_prompt(
            notification_text="Test",
            prompt_hint="scene",
            image_class="BasicTextImage",
            style_profile=StyleProfile(id="default", prompt_suffix=""),
            environment_variant=EnvironmentVariant(id="default", prompt_suffix=""),
        )
        assert "branded" in prompt.lower() or "copyrighted" in prompt.lower()

    def test_random_style_when_none(self):
        # Just ensure it doesn't crash with None style/env
        prompt = build_notification_prompt(
            notification_text="Test",
            prompt_hint="scene",
            image_class="BasicTextImage",
        )
        assert len(prompt) > 50

    def test_empty_prompt_hint(self):
        prompt = build_notification_prompt(
            notification_text="Test message",
            prompt_hint="",
            image_class="BasicTextImage",
            style_profile=StyleProfile(id="default", prompt_suffix=""),
            environment_variant=EnvironmentVariant(id="default", prompt_suffix=""),
        )
        assert "Test message" in prompt
        assert "Scene:" not in prompt


class TestBuildPlaceholderPrompt:
    def test_placeholder_structure(self):
        prompt = build_placeholder_prompt(
            style_profile=StyleProfile(id="default", prompt_suffix=""),
            environment_variant=EnvironmentVariant(id="default", prompt_suffix=""),
        )
        assert "16:9" in prompt
        assert "calm" in prompt.lower() or "soothing" in prompt.lower()
        assert "wall display" in prompt.lower()

    def test_placeholder_random(self):
        # Just ensure it doesn't crash
        prompt = build_placeholder_prompt()
        assert len(prompt) > 50
