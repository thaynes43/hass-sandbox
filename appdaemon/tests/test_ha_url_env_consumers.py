"""HA URL environment pointers reach provisioning after config migration."""

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml


CASES = [
    ("test_health_check_controller", "health_checks.controller.health_check_controller", "_provision_entities"),
    ("test_repairable_device_checker", "health_checks.checker_apps.device_checker.repairable_device_checker", "_provision_repair_helpers"),
    ("test_repairable_device_group_checker", "health_checks.checker_apps.device_group_checker.repairable_device_group_checker", "_provision_repair_helpers"),
    ("test_repairable_network_protocol_checker", "health_checks.checker_apps.network_protocol_checker.repairable_network_protocol_checker", "_provision_repair_helpers"),
    ("test_fan_health_checker", "health_checks.checker_apps.fan_health_checker.fan_health_checker", "_provision_entities"),
    ("test_spa_health_checker", "health_checks.checker_apps.spa_health_checker.spa_health_checker", "_provision_entities"),
    ("test_shade_gateway_checker", "health_checks.checker_apps.shade_gateway_checker.shade_gateway_checker", "_provision_entities"),
    ("test_protect_health_checker", "health_checks.checker_apps.protect_health_checker.protect_health_checker", "_provision_entities"),
    ("test_school_lunch_app", "providers.ha_provisioner", "_provision_entities"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("helper_module, app_module, method", CASES)
async def test_url_env_pointer_reaches_each_migrated_provisioner(
    monkeypatch, helper_module, app_module, method,
):
    monkeypatch.setenv("HA_URL", "http://ha-env.test:8123")
    helpers = importlib.import_module(helper_module)
    app = helpers._make_app({"ha_url": None, "ha_url_env": "HA_URL"})
    app.initialize()
    provisioner = helpers._make_mock_provisioner()
    with patch(f"{app_module}.HAProvisioner", return_value=provisioner) as constructor:
        await getattr(app, method)()
    constructor.assert_called_once_with(ha_url="http://ha-env.test:8123", ha_token_env="TOKEN")
    assert provisioner.ensure_helper.await_count or provisioner.ensure_script.await_count


@pytest.mark.asyncio
async def test_controller_mute_persistence_also_uses_env_pointer(monkeypatch):
    monkeypatch.setenv("HA_URL", "http://ha-env.test:8123")
    helpers = importlib.import_module("test_health_check_controller")
    app = helpers._make_app({"ha_url": None, "ha_url_env": "HA_URL"})
    app.call_service = AsyncMock()
    provisioner = helpers._make_mock_provisioner()
    with patch("health_checks.controller.health_check_controller.HAProvisioner", return_value=provisioner) as constructor:
        await app._persist_mute("spa", True, None)
    constructor.assert_called_once_with(ha_url="http://ha-env.test:8123", ha_token_env="TOKEN")
    app.call_service.assert_awaited_once()


@pytest.mark.asyncio
async def test_protect_admin_discovery_uses_env_pointer(monkeypatch):
    monkeypatch.setenv("HA_URL", "http://ha-env.test:8123")
    helpers = importlib.import_module("test_protect_health_checker")
    app = helpers._make_app({"ha_url": None, "ha_url_env": "HA_URL"})
    app.get_state = AsyncMock(return_value=None)
    app.initialize()
    provisioner = helpers._make_mock_provisioner()
    admin = helpers._make_mock_admin()
    with patch(helpers.MOD_PATH + ".HAProvisioner", return_value=provisioner), patch(
        helpers.MOD_PATH + ".HaAdminClient", return_value=admin,
    ) as constructor:
        await app._async_startup()
    constructor.assert_called_once_with(ha_url="http://ha-env.test:8123", ha_token_env="TOKEN")


@pytest.mark.parametrize("filename", ["apps-prod.yaml", "apps-dev.yaml"])
def test_migrated_configs_load_without_secret_tag_and_use_url_pointers(filename):
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "apps" / filename).read_text())
    with_ha_url = [app for app in config.values() if isinstance(app, dict) and "ha_url_env" in app]
    assert with_ha_url
    assert all(app["ha_url_env"] == "HA_URL" for app in with_ha_url)
    assert not any(isinstance(app, dict) and "ha_url" in app for app in config.values())
