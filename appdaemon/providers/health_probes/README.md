# Health Probes

Asynchronous HTTP probes for AppDaemon health checkers. The transport lives in the provider layer; `health_checks/shared/check_utils.py` reexports the functions so existing checker imports keep working.

- `http_check()` requires a successful HTTP response (2xx).
- `http_reachable_check()` accepts responses below 500, including authentication challenges; server errors remain critical.

Both return the existing `status` and `detail` result dictionary and retain their timeout behavior. They use `aiohttp` and require no AppDaemon runtime. The health-check apps consume these probes through the shared compatibility exports.
