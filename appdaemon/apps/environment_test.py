"""
Environment Test AppDaemon app.

Verifies the AppDaemon dev environment:
- Logs on initialize() to confirm the app loads
- Registers a run_daily callback as a sanity check
- Demonstrates the basic app lifecycle (initialize, callbacks, terminate)

Reference: https://appdaemon.readthedocs.io/en/latest/APPGUIDE.html
"""

import hassapi as hass


class EnvironmentTest(hass.Hass):
    """Environment test app for AppDaemon."""

    def initialize(self):
        """Called on startup and reload. Register callbacks here."""
        self.log("Environment test: AppDaemon dev environment loaded")
        # Run every 1 second for dev environment testing (change to run_daily for prod)
        self.run_every(self._tick_callback, "now", 1)

    def _tick_callback(self, cb_args):
        """Called every second for dev testing."""
        self.log("Environment test: tick callback fired")

    def terminate(self):
        """Called before reload. Optional cleanup."""
        self.log("Environment test: terminating")
