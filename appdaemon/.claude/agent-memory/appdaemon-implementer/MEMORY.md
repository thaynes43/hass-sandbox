# AppDaemon Implementer Memory

See topic files for detailed notes. Key links:
- `patterns.md` — app init, provisioning, test scaffolding patterns

## Quick Reference

### App init pattern (from dashboard_notify and vestaboard_configuration_app)
1. Parse config in `initialize()` using `resolve_arg_secret(cfg, key, default=...)`
2. Set up state, build dependencies (libraries, providers)
3. Call `self.run_in(self._async_startup_wrapper, 0)` at end of `initialize()`
4. `_async_startup_wrapper` calls `self.create_task(self._async_startup())`
5. `_async_startup` does: provision → register listeners → publish initial state

### Provisioner pattern
- Import inside method: `from providers.ha_provisioner import HAProvisioner`
- `prov = HAProvisioner(ha_url, ha_token_env)` — token_env is the env var *name*
- `await prov.ensure_script(script_id, config_dict)` → returns True if created
- `await prov.ensure_helper(helper_type, name, **kwargs)` → returns True if created
- Wrap each call in try/except, log errors at ERROR level

### Test scaffolding pattern
```python
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
```
- Mock all AppDaemon methods on the instance (get_state, set_state, log, fire_event, etc.)
- Use `_run(coro)` = `asyncio.get_event_loop().run_until_complete(coro)` for async tests
- Mock HAProvisioner: `patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov)`
- Use `tmp_path` fixture (pytest) for file-backed tests

### Security rules reminder
- No credentials in apps/ — only env var names via `_env` suffix keys
- `ha_token_env` holds the env var name, not the token itself
- All external HTTP calls go in providers/ only
- `frame_library_path` is a filesystem path, not a credential — can be plain string in config
