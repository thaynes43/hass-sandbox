"""Board I/O — sleep window logic and write-success tracking."""

from __future__ import annotations

from datetime import datetime as dt, time as dtime
from typing import Callable


class BoardIO:
    """Manages sleep-window checks and board-write status tracking.

    Pure Python — no AppDaemon dependency.  The actual ``VestaboardClient``
    calls remain on the controller app (for test-patch compatibility).
    """

    def __init__(
        self,
        sleep_enabled: bool,
        sleep_start: str,
        sleep_end: str,
        log_fn: Callable[[str], None],
    ) -> None:
        self._sleep_enabled = sleep_enabled
        self._sleep_start = sleep_start
        self._sleep_end = sleep_end
        self._log = log_fn
        self.last_write_ok: bool | None = None

    # ------------------------------------------------------------------
    # Sleep window
    # ------------------------------------------------------------------

    @staticmethod
    def parse_time(time_str: str) -> tuple[int, int, int]:
        """Parse 'HH:MM:SS' or 'HH:MM' into (hour, minute, second)."""
        parts = str(time_str).split(":")
        h = int(parts[0]) if len(parts) > 0 else 0
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
        return h, m, s

    def is_sleeping(self) -> bool:
        """Return True if the current time is within the sleep window."""
        if not self._sleep_enabled:
            return False
        now = dt.now().time()
        sh, sm, ss = self.parse_time(self._sleep_start)
        eh, em, es = self.parse_time(self._sleep_end)
        start = dtime(sh, sm, ss)
        end = dtime(eh, em, es)
        if start < end:
            return start <= now < end
        else:
            return now >= start or now < end

    @property
    def sleep_end(self) -> str | None:
        """Return the sleep end time string, or None if sleep is disabled."""
        return self._sleep_end if self._sleep_enabled else None
