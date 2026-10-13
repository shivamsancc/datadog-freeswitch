# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Orchestration tests with a SIMULATED ESL server and a SIMULATED Agent (UDP sink).

Not the real FreeSWITCH or Agent."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import pytest

from fs_rtp_listener.config import Config
from fs_rtp_listener.listener import Listener
from fs_rtp_listener.statsd import StatsdClient

from .conftest import EVENT_USER, SENTINEL, FakeEsl, UdpSink, event_body

Wait = Callable[..., bool]


def config(server: FakeEsl, sink: UdpSink, **kw: Any) -> Config:
    base: dict[str, Any] = {
        'host': '127.0.0.1',
        'port': server.port,
        'username': EVENT_USER,
        'password': SENTINEL,
        'statsd_port': sink.port,
        'health_interval': 0.2,
        'backoff_min': 0.05,
        'backoff_max': 0.3,
        'connect_timeout': 1.0,
        'read_timeout': 0.1,
    }
    base.update(kw)
    return Config(**base)


def run(server: FakeEsl, sink: UdpSink, **kw: Any) -> Listener:
    listener = Listener(config(server, sink, **kw))
    listener.start()
    return listener


def counter(listener: Listener, name: str) -> int:
    return listener.snapshot()['counters'][name]  # type: ignore[index,no-any-return]


def test_valid_event_becomes_two_distributions_with_bounded_tags(esl: FakeEsl, sink: UdpSink, wait_for: Wait) -> None:
    listener = run(esl, sink)
    try:
        assert wait_for(lambda: listener.connected)
        esl.send_event(event_body(mos='4.20', quality='87.00', codec='PCMA', direction='inbound'))
        assert wait_for(lambda: len(sink.named('freeswitch.rtp.mos:')) == 1)
        assert sink.named('freeswitch.rtp.mos:') == ['freeswitch.rtp.mos:4.2|d|#direction:inbound,codec:pcma']
        assert wait_for(
            lambda: (
                sink.named('freeswitch.rtp.quality_percentage:')
                == ['freeswitch.rtp.quality_percentage:87.0|d|#direction:inbound,codec:pcma']
            )
        )
        text = '\n'.join(sink.lines)
        assert 'u-1' not in text and '5551110000' not in text and SENTINEL not in text  # no ids, numbers, secrets
    finally:
        assert listener.stop()


@pytest.mark.parametrize(
    'kw, outcome',
    [
        ({'packets': '0', 'mos': 'nan', 'quality': 'nan'}, 'no_media'),
        ({'packets': '12'}, 'short'),
        ({'mos': None}, 'missing_fields'),
        ({'mos': 'nan'}, 'non_finite'),
        ({'mos': 'inf'}, 'non_finite'),
        ({'quality': '101'}, 'out_of_range'),
        ({'mos': 'abc'}, 'invalid_value'),
    ],
)
def test_rejected_samples_emit_no_quality_and_are_counted(
    esl: FakeEsl, sink: UdpSink, wait_for: Wait, kw: dict[str, str | None], outcome: str
) -> None:
    listener = run(esl, sink)
    try:
        assert wait_for(lambda: listener.connected)
        esl.send_event(event_body(**kw))  # type: ignore[arg-type]
        assert wait_for(lambda: listener.snapshot()['outcomes'][outcome] == 1)  # type: ignore[index]
        time.sleep(0.5)
        assert sink.named('freeswitch.rtp.mos:') == [] and sink.named('freeswitch.rtp.quality_percentage:') == []
        assert wait_for(
            lambda: any(
                line.startswith('freeswitch.rtp.listener.samples:1|c|#outcome:' + outcome) for line in sink.lines
            )
        )
    finally:
        listener.stop()


def test_duplicate_event_ids_are_processed_once(esl: FakeEsl, sink: UdpSink, wait_for: Wait) -> None:
    listener = run(esl, sink)
    try:
        assert wait_for(lambda: listener.connected)
        for _ in range(3):
            esl.send_event(event_body(uid='same'))
        esl.send_event(event_body(uid='other'))
        assert wait_for(lambda: counter(listener, 'events') == 4)
        assert wait_for(lambda: listener.snapshot()['outcomes']['valid'] == 2)  # type: ignore[index]
        assert counter(listener, 'duplicates') == 2
    finally:
        listener.stop()


def test_malformed_event_text_does_not_stop_the_listener(esl: FakeEsl, sink: UdpSink, wait_for: Wait) -> None:
    listener = run(esl, sink)
    try:
        assert wait_for(lambda: listener.connected)
        esl.send_event(b'\xff\xfe not utf8 \x00')
        esl.send_event(b'')
        esl.send_event(event_body(uid='good'))
        assert wait_for(lambda: listener.snapshot()['outcomes']['valid'] == 1)  # type: ignore[index]
    finally:
        listener.stop()


def test_disconnect_reconnects_with_bounded_backoff_and_counts_the_gap(
    esl: FakeEsl, sink: UdpSink, wait_for: Wait
) -> None:
    listener = run(esl, sink, backoff_min=0.05, backoff_max=0.2)
    try:
        assert wait_for(lambda: listener.connected)
        assert wait_for(lambda: counter(listener, 'reconnects') == 1)
        esl.drop_all()
        assert wait_for(lambda: counter(listener, 'reconnects') == 2, timeout=5)
        assert esl.connections >= 2
        esl.send_event(event_body(uid='after'))
        assert wait_for(lambda: listener.snapshot()['outcomes']['valid'] == 1)  # type: ignore[index]
        assert listener.snapshot()['gap_seconds_total'] > 0  # type: ignore[operator]
        assert wait_for(
            lambda: any(line.startswith('freeswitch.rtp.listener.total:2|g|#counter:reconnects') for line in sink.lines)
        )
    finally:
        listener.stop()


def test_events_sent_while_disconnected_are_not_replayed(esl: FakeEsl, sink: UdpSink, wait_for: Wait) -> None:
    listener = run(esl, sink, backoff_min=0.4, backoff_max=0.4)
    try:
        assert wait_for(lambda: listener.connected)
        esl.drop_all()
        esl.send_event(event_body(uid='lost'))  # nobody is subscribed: FreeSWITCH would not queue it
        assert wait_for(lambda: counter(listener, 'reconnects') == 2, timeout=5)
        time.sleep(0.5)
        assert counter(listener, 'events') == 0
        assert listener.snapshot()['gap_seconds_total'] >= 0.15  # type: ignore[operator]
    finally:
        listener.stop()


def test_gap_is_reported_while_the_listener_is_still_disconnected(sink: UdpSink, wait_for: Wait) -> None:
    server = FakeEsl()
    server.refuse_subscription = True  # it can never subscribe: the gap keeps growing and is reported while open
    listener = run(server, sink, backoff_min=0.05, backoff_max=0.1)
    try:
        assert wait_for(
            lambda: len([x for x in sink.lines if x.startswith('freeswitch.rtp.listener.gap_seconds:')]) >= 3, timeout=5
        )
        assert not listener.connected
        deltas = [float(x.split(':')[1].split('|')[0]) for x in sink.named('freeswitch.rtp.listener.gap_seconds:')]
        assert sum(deltas) > 0.3
        assert any(x == 'freeswitch.rtp.listener.connected:0|g' for x in sink.lines)
        assert listener.snapshot()['connect_reasons'] == {'permissions': counter(listener, 'connect_errors')}
    finally:
        listener.stop()
        server.stop()


def test_authentication_failure_keeps_retrying_and_never_logs_the_secret(
    sink: UdpSink, wait_for: Wait, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeEsl(users={EVENT_USER: 'a-different-secret'})
    caplog.set_level(logging.DEBUG)
    listener = run(server, sink, backoff_min=0.05, backoff_max=0.1)
    try:
        assert wait_for(lambda: counter(listener, 'connect_errors') >= 3, timeout=5)
        reasons: dict[str, int] = listener.snapshot()['connect_reasons']  # type: ignore[assignment]
        assert reasons.get('auth', 0) >= 3
        assert SENTINEL not in caplog.text and 'a-different-secret' not in caplog.text
        assert caplog.text.count('connection failed') <= 2  # rate limited, not one line per attempt
    finally:
        listener.stop()
        server.stop()


def test_backoff_is_bounded_and_jittered(esl: FakeEsl, sink: UdpSink) -> None:
    listener = Listener(config(esl, sink, backoff_min=1.0, backoff_max=8.0), rng=lambda: 0.0)
    waits: list[float] = []

    class FakeStop:
        def wait(self, delay: float) -> bool:
            waits.append(delay)
            return False

    listener._stop = FakeStop()  # type: ignore[assignment]
    for attempt in range(1, 8):
        listener._sleep_backoff(attempt)
    assert waits == [0.5, 1.0, 2.0, 4.0, 4.0, 4.0, 4.0]  # half of min(8, 2^(n-1)), jitter factor 0.5 at rng=0
    waits.clear()
    listener._rng = lambda: 1.0
    for attempt in (1, 4, 20):
        listener._sleep_backoff(attempt)
    assert waits == [1.0, 8.0, 8.0]


def test_queue_overflow_drops_the_newest_and_counts_it(esl: FakeEsl, sink: UdpSink, wait_for: Wait) -> None:
    gate = threading.Event()
    listener = Listener(config(esl, sink, queue_size=5))
    listener._emit_hook = lambda: gate.wait(10)  # the emitter is stuck on its first sample
    listener.start()
    try:
        assert wait_for(lambda: listener.connected)
        for i in range(30):
            esl.send_event(event_body(uid='q%d' % i))
        assert wait_for(lambda: counter(listener, 'events') == 30)
        assert counter(listener, 'dropped') == 30 - 5 - 1  # queue of 5, one held by the emitter
        assert listener.max_queue_depth == 5
        gate.set()
        assert wait_for(lambda: listener.snapshot()['outcomes']['valid'] == 6)  # type: ignore[index]
        assert wait_for(lambda: any(x.startswith('freeswitch.rtp.listener.dropped:24|c') for x in sink.lines))
    finally:
        gate.set()
        listener.stop()


def test_clean_shutdown_ends_threads_and_closes_the_connection(esl: FakeEsl, sink: UdpSink, wait_for: Wait) -> None:
    listener = run(esl, sink)
    assert wait_for(lambda: listener.connected)
    esl.send_event(event_body(uid='last'))
    assert listener.stop(timeout=5)
    assert not [t for t in threading.enumerate() if t.name.startswith('fs-rtp-')]
    assert not listener.connected
    # queued data was processed and a final health report was sent before stopping
    assert any(x.startswith('freeswitch.rtp.listener.total:') for x in sink.lines)
    assert not any(line.startswith(('api', 'bgapi')) for line in esl.lines)


def test_send_errors_are_counted_not_raised(esl: FakeEsl, wait_for: Wait) -> None:
    bad = StatsdClient('127.0.0.1', 9)
    bad.send_errors = 0

    def explode() -> Any:
        raise OSError('network is down')

    bad._factory = explode
    listener = Listener(
        Config(
            host='127.0.0.1',
            port=esl.port,
            username=EVENT_USER,
            password=SENTINEL,
            health_interval=0.2,
            connect_timeout=1.0,
            read_timeout=0.1,
        ),
        statsd=bad,
    )
    listener.start()
    try:
        assert wait_for(lambda: listener.connected)
        esl.send_event(event_body())
        assert wait_for(lambda: listener.snapshot()['outcomes']['valid'] == 1)  # type: ignore[index]
        assert wait_for(lambda: counter(listener, 'send_errors') >= 1)
        assert counter(listener, 'submitted_mos') == 0  # nothing the OS refused is counted as submitted
    finally:
        listener.stop()
