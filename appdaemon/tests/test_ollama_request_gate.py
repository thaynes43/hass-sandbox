"""Tests for the process-wide Ollama request gate (haynes-ops#3450).

AppDaemon must send at most one request at a time to an Ollama endpoint, across
every app and thread. assist02 runs qwen3.5 one request at a time
(haynes-ops#3452), so another client waits for at most the one camera request
already running, never a burst. Waiters queue (FIFO) rather than fail, and a
bounded wait skips the request with a warning instead of waiting forever.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.multimodal_text_provider import ExternalDataGenError
from providers.ai_providers.ollama._request_gate import (
    EndpointGate,
    OllamaQueueTimeout,
    gate_for,
)
from providers.ai_providers.ollama.ollama_multimodal_text_provider import (
    OllamaMultimodalConfig,
    OllamaMultimodalTextProvider,
)
from providers.ai_providers.ollama.ollama_simple_text_provider import (
    OllamaSimpleTextConfig,
    OllamaSimpleTextProvider,
)


def _unique_url() -> str:
    """A fresh endpoint per test so the module-level gate registry never leaks between tests."""
    return f"http://ollama-{uuid.uuid4().hex[:8]}.test:11434"


class _ConcurrencyProbe:
    """Records how many callers are inside a section at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.max_seen = 0
        self.calls = 0

    def __enter__(self):
        with self._lock:
            self.current += 1
            self.calls += 1
            self.max_seen = max(self.max_seen, self.current)
        return self

    def __exit__(self, *exc):
        with self._lock:
            self.current -= 1
        return False


def _wait_until(predicate, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("condition not reached in time")


# --- EndpointGate ---


def test_gate_runs_concurrent_callers_one_at_a_time() -> None:
    gate = EndpointGate("http://gate-unit:11434")
    probe = _ConcurrencyProbe()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with gate.slot(max_wait_s=5.0, label="unit"):
                with probe:
                    time.sleep(0.02)
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert probe.calls == 8
    assert probe.max_seen == 1
    assert gate.in_flight == 0
    assert gate.queued == 0


def test_gate_serves_waiters_in_arrival_order() -> None:
    gate = EndpointGate("http://gate-fifo:11434")
    order: list[int] = []
    release_holder = threading.Event()
    holding = threading.Event()

    def holder() -> None:
        with gate.slot(max_wait_s=1.0):
            holding.set()
            release_holder.wait(timeout=5)

    def waiter(n: int) -> None:
        with gate.slot(max_wait_s=5.0):
            order.append(n)

    h = threading.Thread(target=holder)
    h.start()
    assert holding.wait(timeout=2)

    waiters = []
    for n in range(5):
        t = threading.Thread(target=waiter, args=(n,))
        t.start()
        waiters.append(t)
        _wait_until(lambda n=n: gate.queued == n + 1)

    release_holder.set()
    h.join(timeout=5)
    for t in waiters:
        t.join(timeout=5)

    assert order == [0, 1, 2, 3, 4]


def test_gate_wait_is_bounded_and_skips_with_queue_timeout() -> None:
    gate = EndpointGate("http://gate-timeout:11434")
    release_holder = threading.Event()
    holding = threading.Event()

    def holder() -> None:
        with gate.slot(max_wait_s=1.0):
            holding.set()
            release_holder.wait(timeout=5)

    h = threading.Thread(target=holder)
    h.start()
    assert holding.wait(timeout=2)

    started = time.monotonic()
    with pytest.raises(OllamaQueueTimeout) as exc_info:
        with gate.slot(max_wait_s=0.05, label="multimodal model=qwen3.5:9b"):
            pytest.fail("must not enter the slot while it is held")
    waited = time.monotonic() - started

    # Skips are ExternalDataGenError, so existing callers log a warning and move on.
    assert isinstance(exc_info.value, ExternalDataGenError)
    assert "skipped" in str(exc_info.value)
    assert "multimodal model=qwen3.5:9b" in str(exc_info.value)
    assert 0.04 <= waited < 1.0
    # The timed-out caller left the queue; the holder still owns the slot.
    assert gate.queued == 0
    assert gate.in_flight == 1

    release_holder.set()
    h.join(timeout=5)
    with gate.slot(max_wait_s=0.5) as waited_s:
        assert waited_s < 0.5


def test_gate_timed_out_head_does_not_block_the_next_waiter() -> None:
    gate = EndpointGate("http://gate-head:11434")
    release_holder = threading.Event()
    holding = threading.Event()
    got_slot = threading.Event()
    timed_out = threading.Event()

    def holder() -> None:
        with gate.slot(max_wait_s=1.0):
            holding.set()
            release_holder.wait(timeout=5)

    def impatient() -> None:
        try:
            with gate.slot(max_wait_s=0.05):
                pass
        except OllamaQueueTimeout:
            timed_out.set()

    def patient() -> None:
        with gate.slot(max_wait_s=5.0):
            got_slot.set()

    h = threading.Thread(target=holder)
    h.start()
    assert holding.wait(timeout=2)
    a = threading.Thread(target=impatient)
    a.start()
    _wait_until(lambda: gate.queued == 1)
    b = threading.Thread(target=patient)
    b.start()

    assert timed_out.wait(timeout=2)
    assert not got_slot.is_set()
    release_holder.set()
    assert got_slot.wait(timeout=2)
    for t in (h, a, b):
        t.join(timeout=5)
    assert gate.in_flight == 0
    assert gate.queued == 0


def test_gate_releases_the_slot_when_the_request_raises() -> None:
    gate = EndpointGate("http://gate-raise:11434")
    with pytest.raises(RuntimeError):
        with gate.slot(max_wait_s=0.5):
            raise RuntimeError("boom")
    assert gate.in_flight == 0
    with gate.slot(max_wait_s=0.1):
        assert gate.in_flight == 1


def test_gate_with_limit_two_wakes_a_second_blocked_waiter() -> None:
    """With limit > 1, taking the slot must also wake the next blocked waiter.

    Two holders leave at once and two blocked waiters must then run together
    (each waits for the other at a barrier). If only a release woke waiters,
    the second one could sleep through both releases. That depends on which
    waiter re-checks first, so the scenario repeats to make the race likely.
    """
    for attempt in range(100):
        gate = EndpointGate(f"http://gate-limit2-{attempt}:11434", limit=2)
        release_holders = threading.Event()
        holding = threading.Barrier(3)
        both_in = threading.Barrier(2, timeout=10.0)
        broken: list[int] = []

        def holder() -> None:
            with gate.slot(max_wait_s=1.0):
                holding.wait(timeout=5)
                release_holders.wait(timeout=5)

        def waiter() -> None:
            with gate.slot(max_wait_s=5.0):
                try:
                    both_in.wait()
                except threading.BrokenBarrierError:
                    broken.append(attempt)

        holders = [threading.Thread(target=holder) for _ in range(2)]
        for t in holders:
            t.start()
        holding.wait(timeout=5)  # both slots taken
        waiters = [threading.Thread(target=waiter) for _ in range(2)]
        for t in waiters:
            t.start()
        _wait_until(lambda: gate.queued == 2)  # both waiters are blocked in wait()
        release_holders.set()
        for t in holders + waiters:
            t.join(timeout=10)
        assert not broken, f"attempt {attempt}: the second waiter was not woken while a slot was free"
        assert gate.in_flight == 0


def test_gate_registry_survives_a_module_reload() -> None:
    import importlib

    from providers.ai_providers.ollama import _request_gate

    url = _unique_url()
    before = _request_gate.gate_for(url)
    saved = dict(vars(_request_gate))
    try:
        reloaded = importlib.reload(_request_gate)
        assert reloaded.gate_for(url) is before
    finally:
        # Put the original classes back so the other tests' imports still match.
        vars(_request_gate).update(saved)


def test_gate_rejects_a_zero_limit() -> None:
    with pytest.raises(ValueError):
        EndpointGate("http://gate-limit:11434", limit=0)


def test_gate_for_is_one_gate_per_endpoint() -> None:
    url = _unique_url()
    assert gate_for(url) is gate_for(url + "/")
    assert gate_for(url) is gate_for(url.upper())
    assert gate_for(url) is not gate_for(_unique_url())


# --- Providers share the gate ---


def _ok_response(content: str) -> MagicMock:
    body = json.dumps(
        {
            "model": "qwen3.5:9b",
            "message": {"role": "assistant", "content": content},
            "done": True,
            "done_reason": "stop",
            "load_duration": 0,
        }
    ).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _slow_urlopen(probe: _ConcurrencyProbe, hold_s: float = 0.03):
    timeouts: list[float] = []

    def fake_urlopen(req, timeout=None):
        timeouts.append(timeout)
        with probe:
            time.sleep(hold_s)
        return _ok_response('{"score": 0.5}')

    return fake_urlopen, timeouts


def test_vision_and_text_requests_to_one_endpoint_run_one_at_a_time(tmp_path: Path) -> None:
    """Burst of camera-scoring calls plus a narrative call: never two in flight at once."""
    url = _unique_url()
    img = tmp_path / "frame_000.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"\x00" * 16)
    vision = OllamaMultimodalTextProvider(OllamaMultimodalConfig(base_url=url, timeout_s=300.0))
    text = OllamaSimpleTextProvider(OllamaSimpleTextConfig(base_url=url, timeout_s=300.0))
    probe = _ConcurrencyProbe()
    fake_urlopen, timeouts = _slow_urlopen(probe)
    results: list[dict] = []
    errors: list[BaseException] = []

    def score() -> None:
        try:
            results.append(
                vision.generate_from_image(input_image_path=str(img), instructions="score", expected_keys=["score"])
            )
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)

    def narrate() -> None:
        try:
            results.append(text.generate_from_text(input_text="facts", instructions="narrate", expected_keys=["score"]))
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        threads = [threading.Thread(target=score) for _ in range(5)]
        threads += [threading.Thread(target=narrate) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

    assert not errors
    assert probe.calls == 7
    assert probe.max_seen == 1
    assert len(results) == 7
    # Each request keeps its own HTTP timeout; the queue wait is reported separately.
    assert timeouts == [300.0] * 7
    waits = [r["_meta"]["queue_wait_s"] for r in results]
    assert all(w >= 0 for w in waits)
    assert max(waits) > 0.03  # somebody queued behind the others


def test_queued_request_past_its_bound_is_skipped_without_being_sent(tmp_path: Path) -> None:
    url = _unique_url()
    img = tmp_path / "frame_000.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"\x00" * 16)
    vision = OllamaMultimodalTextProvider(OllamaMultimodalConfig(base_url=url, queue_wait_s=0.05))
    text = OllamaSimpleTextProvider(OllamaSimpleTextConfig(base_url=url, queue_wait_s=0.05))

    with patch("urllib.request.urlopen") as urlopen:
        with gate_for(url).slot(max_wait_s=1.0):  # another request is in flight
            with pytest.raises(OllamaQueueTimeout):
                vision.generate_from_image(input_image_path=str(img), instructions="score")
            with pytest.raises(OllamaQueueTimeout):
                text.generate_from_text(input_text="facts", instructions="narrate")
        urlopen.assert_not_called()

        # Once the slot is free the next request goes straight through.
        urlopen.return_value = _ok_response('{"score": 0.9}')
        out = vision.generate_from_image(input_image_path=str(img), instructions="score")
        assert out["score"] == 0.9


def test_different_endpoints_do_not_block_each_other(tmp_path: Path) -> None:
    img = tmp_path / "frame_000.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"\x00" * 16)
    busy_url, free_url = _unique_url(), _unique_url()
    other = OllamaMultimodalTextProvider(OllamaMultimodalConfig(base_url=free_url, queue_wait_s=0.05))

    with patch("urllib.request.urlopen", return_value=_ok_response('{"score": 1}')):
        with gate_for(busy_url).slot(max_wait_s=1.0):
            out = other.generate_from_image(input_image_path=str(img), instructions="score")
    assert out["score"] == 1


def test_queue_wait_defaults_to_the_request_timeout(tmp_path: Path) -> None:
    """A bundle that shortens timeout_s shortens the queue bound with it."""
    from providers.ai_providers.ollama._request_gate import effective_queue_wait_s

    assert effective_queue_wait_s(None, 300.0) == 300.0
    assert effective_queue_wait_s(None, 45.0) == 45.0
    assert effective_queue_wait_s(5.0, 300.0) == 5.0

    url = _unique_url()
    img = tmp_path / "frame_000.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"\x00" * 16)
    vision = OllamaMultimodalTextProvider(OllamaMultimodalConfig(base_url=url, timeout_s=0.05))
    with patch("urllib.request.urlopen") as urlopen:
        with gate_for(url).slot(max_wait_s=1.0):
            started = time.monotonic()
            with pytest.raises(OllamaQueueTimeout):
                vision.generate_from_image(input_image_path=str(img), instructions="score")
            assert time.monotonic() - started < 1.0
        urlopen.assert_not_called()
