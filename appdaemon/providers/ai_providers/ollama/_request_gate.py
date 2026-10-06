"""Process-wide request gate: at most one AppDaemon request in flight per Ollama endpoint.

Why this exists (haynes-ops#3450, Tom's ruling 2026-10-06): the house Ollama
endpoint (``ollama-assist02``) is shared. Besides AppDaemon's camera pipeline
it serves Home Assistant's Ollama integration (an AI Task entity), and #3450
measured it as a local voice model. HA's Assist pipelines themselves ran on
llama-server (``llama_cpp``), not on this endpoint, when this was written
(2026-10-06). Ollama runs ``qwen3.5`` one request at a time and queues the rest
in arrival order. It ignores ``OLLAMA_NUM_PARALLEL`` for that architecture, so
a second slot is not an option (haynes-ops#3452). So any other client's request
that lands behind a burst of camera-scoring requests waits for the whole burst.
On 2026-10-06 bursts of AppDaemon ``/api/chat`` calls took 27 s to over 2 min
each, and an Assist-sized probe took 37 s behind a burst of 5 versus 6-8 s
behind serialized calls.

Every AppDaemon request to Ollama is camera-pipeline work
(``detection_summary_app``: per-frame vision scoring and the run narrative).
Both Ollama providers take this gate's slot before they send, so the whole
AppDaemon process has at most one request at Ollama per endpoint. Any other
client then waits for at most the one camera request already running (about
10 s), not a burst.

Behaviour:

- Callers queue in FIFO order rather than fail.
- A caller waits at most ``max_wait_s`` for its turn. Past that it raises
  :class:`OllamaQueueTimeout` (an ``ExternalDataGenError``), so the caller logs
  a warning and skips that request. The queue never waits silently forever.
- The HTTP timeout starts only once a caller holds the slot, so each request
  keeps its full timeout.

The registry is module level, so every app and thread in the AppDaemon process
shares the same gates. In the container ``providers/`` is copied to
``/conf/apps/providers`` (``docker/entrypoint.sh``), inside AppDaemon's watched
app directory. The image is immutable, so nothing triggers a reload. The
registry also survives ``importlib.reload`` (``globals().get`` below), but not
a fresh import after the module is dropped from ``sys.modules``. That would
start an empty registry, and two requests could be in flight with no log line.
So the invariant is: restart AppDaemon after editing any file inside a running
pod.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Dict, Iterator

from ..multimodal_text_provider import ExternalDataGenError

logger = logging.getLogger(__name__)

# Longest a request waits for the endpoint's slot before it is skipped. It
# matches the per-request HTTP timeout: before this gate the queue lived inside
# Ollama, where that same timeout was the only bound on the wait. Sizing: a
# 1920x1080 vision call takes about 9-10.6 s on assist02, and a camera run is up
# to 10 scoring calls plus one narrative, so about 110 s. 300 s is roughly 30
# calls, or about three cameras' full runs, ahead. Other clients do not depend on
# this bound, because AppDaemon never has more than one request at Ollama. The bound
# only decides when a pile-up (many cameras at once, or a slow or hung Ollama)
# starts dropping frames instead of delivering them late.
OLLAMA_DEFAULT_QUEUE_WAIT_S = 300.0


class OllamaQueueTimeout(ExternalDataGenError):
    """A request waited longer than its bound for the endpoint's slot and was skipped."""


class EndpointGate:
    """FIFO gate allowing at most ``limit`` requests in flight at once."""

    def __init__(self, endpoint: str, *, limit: int = 1) -> None:
        if int(limit) < 1:
            raise ValueError(f"limit must be >= 1, got {limit!r}")
        self.endpoint = endpoint
        self._limit = int(limit)
        self._cond = threading.Condition()
        self._in_flight = 0
        self._queue: deque[object] = deque()

    @property
    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

    @property
    def queued(self) -> int:
        with self._cond:
            return len(self._queue)

    @contextmanager
    def slot(self, *, max_wait_s: float, label: str = "") -> Iterator[float]:
        """Hold the endpoint's slot for the body of the ``with``; yields seconds spent queued.

        Raises :class:`OllamaQueueTimeout` if the slot is not free within ``max_wait_s``.
        """
        waited_s = self._acquire(max_wait_s=float(max_wait_s), label=label)
        try:
            yield waited_s
        finally:
            self._release()

    def _acquire(self, *, max_wait_s: float, label: str) -> float:
        ticket = object()
        started = time.monotonic()
        deadline = started + max(0.0, max_wait_s)
        with self._cond:
            ahead_at_entry = len(self._queue) + self._in_flight
            self._queue.append(ticket)
            try:
                while not (self._queue[0] is ticket and self._in_flight < self._limit):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        waited_s = time.monotonic() - started
                        logger.warning(
                            "ollama queue wait exceeded: endpoint=%s label=%s waited_s=%.1f "
                            "max_wait_s=%.1f ahead_at_entry=%d; skipping request",
                            self.endpoint,
                            label,
                            waited_s,
                            max_wait_s,
                            ahead_at_entry,
                        )
                        raise OllamaQueueTimeout(
                            f"ollama queue wait exceeded {max_wait_s:.0f}s for {self.endpoint} "
                            f"({label or 'request'}; {ahead_at_entry} ahead at entry); skipped. "
                            "AppDaemon sends one request at a time to this endpoint so other clients never queue behind a burst."
                        )
                    self._cond.wait(remaining)
            except BaseException:
                # Leave the queue (we may have been at its head) and let the next waiter re-check.
                try:
                    self._queue.remove(ticket)
                except ValueError:
                    pass
                self._cond.notify_all()
                raise
            self._queue.popleft()
            self._in_flight += 1
            if self._queue and self._in_flight < self._limit:
                # With limit > 1 the new head may be able to go now as well.
                self._cond.notify_all()
        return time.monotonic() - started

    def _release(self) -> None:
        with self._cond:
            self._in_flight -= 1
            self._cond.notify_all()


# globals().get keeps the existing registry if this module is re-executed by
# importlib.reload (see the module docstring).
_GATES: Dict[str, EndpointGate] = globals().get("_GATES", {})
_GATES_LOCK = globals().get("_GATES_LOCK") or threading.Lock()


def _endpoint_key(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/").lower()


def gate_for(base_url: str) -> EndpointGate:
    """Return the process-wide gate for an Ollama base URL (one request in flight at a time)."""
    key = _endpoint_key(base_url)
    with _GATES_LOCK:
        gate = _GATES.get(key)
        if gate is None:
            gate = EndpointGate(key, limit=1)
            _GATES[key] = gate
        return gate
