"""Unit tests for detection profile system."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from detection_summary_app.profiles import (
    BUILTIN_PROFILES,
    PROFILE_ANIMALS,
    PROFILE_DEFAULT,
    PROFILE_PACKAGES,
    PROFILE_VEHICLES,
    DetectionProfile,
    SubjectCategory,
    load_default_profile,
    load_profile_by_name,
    load_profile_from_dict,
)
from detection_summary_app.prompting.schema_specs import DEFAULT_SCORE_FIELDS


class TestDefaultProfile:
    def test_default_profile_matches_current_behavior(self):
        """Default profile has people+animals categories, 8 score fields."""
        p = PROFILE_DEFAULT
        assert p.name == "default"
        cat_names = [c.name for c in p.categories]
        assert "people" in cat_names
        assert "animals" in cat_names
        assert len(p.score_fields) == 8
        assert p.score_fields == DEFAULT_SCORE_FIELDS

    def test_default_profile_is_frozen(self):
        with pytest.raises(AttributeError):
            PROFILE_DEFAULT.name = "changed"  # type: ignore[misc]

    def test_subject_category_is_frozen(self):
        cat = SubjectCategory(name="test")
        with pytest.raises(AttributeError):
            cat.name = "changed"  # type: ignore[misc]

    def test_subject_category_people_signals(self):
        """People category count_signals = (male_count, female_count)."""
        people = next(c for c in PROFILE_DEFAULT.categories if c.name == "people")
        assert people.count_signals == ("male_count", "female_count")

    def test_default_profile_consensus_strategy(self):
        assert PROFILE_DEFAULT.consensus_strategy == "mode"


class TestPackagesProfile:
    def test_packages_profile_has_package_count(self):
        """Packages profile adds package_count field."""
        p = PROFILE_PACKAGES
        field_keys = [f.key for f in p.score_fields]
        assert "package_count" in field_keys
        assert len(p.score_fields) == 9  # 8 default + package_count

    def test_packages_profile_has_packages_category(self):
        cat_names = [c.name for c in PROFILE_PACKAGES.categories]
        assert "packages" in cat_names
        pkg_cat = next(c for c in PROFILE_PACKAGES.categories if c.name == "packages")
        assert pkg_cat.required_for_publish is True
        assert "package_count" in pkg_cat.count_signals


class TestVehiclesProfile:
    def test_vehicles_profile_has_vehicle_signals(self):
        """Vehicles profile adds vehicle_count + vehicle_type."""
        p = PROFILE_VEHICLES
        field_keys = [f.key for f in p.score_fields]
        assert "vehicle_count" in field_keys
        assert "vehicle_type" in field_keys
        assert len(p.score_fields) == 10  # 8 default + 2

    def test_vehicles_profile_has_vehicles_category(self):
        cat_names = [c.name for c in PROFILE_VEHICLES.categories]
        assert "vehicles" in cat_names


class TestAnimalsProfile:
    def test_animals_profile_has_animals_required(self):
        """Animals profile requires animals for publish, people are context only."""
        p = PROFILE_ANIMALS
        animals_cat = next(c for c in p.categories if c.name == "animals")
        assert animals_cat.required_for_publish is True
        assert "animal_count" in animals_cat.count_signals

    def test_animals_profile_people_not_required(self):
        """People category is NOT required for publish in animals profile."""
        people_cat = next(c for c in PROFILE_ANIMALS.categories if c.name == "people")
        assert people_cat.required_for_publish is False

    def test_animals_profile_no_extra_score_fields(self):
        """Animals profile uses only the 8 default score fields (no extras)."""
        assert len(PROFILE_ANIMALS.score_fields) == 8
        assert PROFILE_ANIMALS.score_fields == DEFAULT_SCORE_FIELDS

    def test_load_profile_by_name_animals(self):
        p = load_profile_by_name("animals")
        assert p.name == "animals"
        assert p is PROFILE_ANIMALS


class TestProfileLoading:
    def test_load_profile_by_name_default(self):
        p = load_profile_by_name("default")
        assert p.name == "default"
        assert p is PROFILE_DEFAULT

    def test_load_profile_by_name_packages(self):
        p = load_profile_by_name("packages")
        assert p.name == "packages"
        assert p is PROFILE_PACKAGES

    def test_load_profile_by_name_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown detection profile"):
            load_profile_by_name("nonexistent")

    def test_load_default_profile(self):
        p = load_default_profile()
        assert p is PROFILE_DEFAULT

    def test_load_profile_from_dict_minimal(self):
        """Inline dict with just categories works."""
        data = {
            "name": "custom_test",
            "categories": [
                {
                    "name": "people",
                    "required_for_publish": True,
                    "count_signals": ["male_count", "female_count"],
                },
            ],
        }
        p = load_profile_from_dict(data)
        assert p.name == "custom_test"
        assert len(p.categories) == 1
        assert p.categories[0].name == "people"
        assert p.categories[0].count_signals == ("male_count", "female_count")
        # Should still have default score fields
        assert p.score_fields == DEFAULT_SCORE_FIELDS

    def test_load_profile_from_dict_with_extra_fields(self):
        """Inline dict with extra_score_fields works."""
        data = {
            "name": "custom_with_extras",
            "categories": [
                {"name": "packages", "required_for_publish": True, "count_signals": ["package_count"]},
            ],
            "extra_score_fields": [
                {
                    "key": "package_count",
                    "type_hint": "int",
                    "default": 0,
                    "prompt_guidance": "count of packages",
                },
            ],
            "consensus_strategy": "median",
        }
        p = load_profile_from_dict(data)
        assert p.name == "custom_with_extras"
        assert len(p.score_fields) == 9  # 8 default + 1 extra
        field_keys = [f.key for f in p.score_fields]
        assert "package_count" in field_keys
        assert p.consensus_strategy == "median"

    def test_builtin_profiles_count(self):
        assert len(BUILTIN_PROFILES) >= 4
        assert "default" in BUILTIN_PROFILES
        assert "packages" in BUILTIN_PROFILES
        assert "vehicles" in BUILTIN_PROFILES
        assert "animals" in BUILTIN_PROFILES
