"""MQTT Broker Health Checker -- verifies AppDaemon <-> MQTT broker connectivity.

Publishes a ping message with a unique nonce to a test topic, then listens
for its own message to confirm the round-trip.  If the message is not
received within the configured timeout, the broker is reported as critical.

Communication with the controller is event-only (never ``get_app``).
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

# Add health_checks package root so we can import shared utilities
_health_checks_root = str(Path(__file__).resolve().parents[2])
if _health_checks_root not in sys.path:
    sys.path.insert(0, _health_checks_root)

import hassapi as hass


class MqttBrokerChecker(hass.Hass):
    """Checks MQTT broker health via publish/subscribe ping test."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        # Identity
        self._checker_id: str = args.get("checker_id", "mqtt_broker")
        self._checker_name: str = args.get("checker_name", "MQTT Broker")

        # Timing
        self._check_interval_s: int = int(args.get("check_interval_s", 120))
        self._ping_timeout_s: int = int(args.get("ping_timeout_s", 10))

        # MQTT
        self._mqtt_namespace: str = args.get("mqtt_namespace", "mqtt")
        self._ping_topic: str = args.get(
            "ping_topic", "appdaemon/health_check/ping"
        )

        # State
        self._current_nonce: str | None = None
        self._last_pong_nonce: str | None = None
        self._broker_status: str = "unknown"
        self._broker_detail: str = "initializing"
        self._startup_retries: int = 3  # retries before first successful check
        self._ever_succeeded: bool = False

        self.log(
            f"MqttBrokerChecker initialising: id={self._checker_id}, "
            f"interval={self._check_interval_s}s, timeout={self._ping_timeout_s}s",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    def _on_startup(self, kwargs: Any) -> None:
        """run_in callback -- launches the async startup coroutine."""
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        """Register with controller, set up listeners and timer."""
        # Register with controller
        self._register()

        # Subscribe to ping response topic via MQTT namespace
        try:
            self.listen_event(
                self._on_mqtt_message,
                "MQTT_MESSAGE",
                topic=self._ping_topic,
                namespace=self._mqtt_namespace,
            )
            self.log(
                f"Subscribed to MQTT topic: {self._ping_topic}", level="INFO"
            )
        except Exception as exc:
            self.log(
                f"Failed to subscribe to MQTT: {exc!r}", level="ERROR"
            )

        # Listen for controller events (HASS namespace)
        self.listen_event(
            self._on_controller_ready, "health_check_controller_ready"
        )
        self.listen_event(self._on_recheck, "health_check_recheck")

        # Schedule first check + periodic timer
        self.run_in(self._first_check, 5)

        self.log("MqttBrokerChecker started", level="INFO")

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        """Fire registration event to the controller."""
        self.fire_event(
            "health_check_command",
            command="register_checker",
            payload=json.dumps({
                "checker_id": self._checker_id,
                "checker_name": self._checker_name,
                "check_names": ["Broker Connectivity"],
            }),
        )
        self.log(
            f"Registered '{self._checker_name}' with checks: "
            "['Broker Connectivity']",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_controller_ready(
        self, event_name: str, data: dict, kwargs: Any
    ) -> None:
        """Re-register when controller (re)starts."""
        self.log(
            f"Controller ready -- re-registering '{self._checker_name}'",
            level="INFO",
        )
        self._register()
        self._run_ping()

    def _on_recheck(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Run check immediately on force-recheck request."""
        self.log(
            f"Force recheck requested for '{self._checker_name}'",
            level="INFO",
        )
        self._run_ping()

    def _first_check(self, kwargs: Any) -> None:
        """Run the first check, then start periodic timer."""
        self._run_ping()
        self.run_every(
            self._check_tick,
            f"now+{self._check_interval_s}",
            self._check_interval_s,
        )

    def _check_tick(self, kwargs: Any) -> None:
        """Periodic timer callback."""
        self._run_ping()

    # ------------------------------------------------------------------
    # Ping / pong
    # ------------------------------------------------------------------

    def _run_ping(self) -> None:
        """Send a ping to the MQTT broker and schedule timeout evaluation."""
        nonce = str(uuid.uuid4())[:8]
        self._current_nonce = nonce
        self._last_pong_nonce = None

        try:
            self.call_service(
                "mqtt/publish",
                topic=self._ping_topic,
                payload=json.dumps({"nonce": nonce, "ts": time.time()}),
                namespace=self._mqtt_namespace,
            )
            self.log(f"Published MQTT ping nonce={nonce}", level="DEBUG")
            # Schedule timeout evaluation
            self.run_in(self._evaluate_ping, self._ping_timeout_s, nonce=nonce)
        except Exception as exc:
            self.log(
                f"Failed to publish MQTT ping: {exc!r}", level="WARNING"
            )
            self._broker_status = "critical"
            self._broker_detail = f"publish failed: {exc}"
            self._report_status()

    def _on_mqtt_message(
        self, event_name: str, data: dict, kwargs: Any
    ) -> None:
        """Handle MQTT message on the ping topic."""
        try:
            payload = data.get("payload", "{}")
            if isinstance(payload, str):
                msg = json.loads(payload)
            else:
                msg = payload or {}

            nonce = msg.get("nonce")
            if not nonce:
                return

            self._last_pong_nonce = nonce
            self.log(f"Received MQTT pong nonce={nonce}", level="DEBUG")

            # If this matches our current outstanding ping, report immediately
            if nonce == self._current_nonce:
                latency_ms = (time.time() - msg.get("ts", time.time())) * 1000
                self._broker_status = "ok"
                self._broker_detail = f"round-trip {latency_ms:.0f}ms"
                self._ever_succeeded = True
                self._report_status()
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            self.log(f"Failed to parse MQTT pong: {exc!r}", level="DEBUG")

    def _evaluate_ping(self, kwargs: Any) -> None:
        """Check if the ping was responded to within timeout."""
        nonce = kwargs.get("nonce")
        if nonce != self._current_nonce:
            return  # Stale evaluation

        if self._last_pong_nonce == nonce:
            return  # Already handled in _on_mqtt_message

        # Timeout — during startup MQTT may still be reconnecting, so retry
        # a few times before declaring critical
        if not self._ever_succeeded and self._startup_retries > 0:
            self._startup_retries -= 1
            self.log(
                f"Ping timeout during startup — retrying "
                f"({self._startup_retries} retries left)",
                level="INFO",
            )
            self._run_ping()
            return

        # Timeout -- broker unreachable
        self._broker_status = "critical"
        self._broker_detail = f"ping timeout ({self._ping_timeout_s}s)"
        self._report_status()

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    def _report_status(self) -> None:
        """Report current broker status to the controller."""
        self.fire_event(
            "health_check_command",
            command="report_status",
            payload=json.dumps({
                "checker_id": self._checker_id,
                "results": [
                    {
                        "name": "Broker Connectivity",
                        "status": self._broker_status,
                        "detail": self._broker_detail,
                    }
                ],
            }),
        )
        self.log(
            f"Reported broker status: {self._broker_status} "
            f"({self._broker_detail})",
            level="INFO" if self._broker_status != "ok" else "DEBUG",
        )
