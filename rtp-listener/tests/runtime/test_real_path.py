# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Real FreeSWITCH hangup events -> the listener -> DogStatsD -> the installed Datadog Agent 7.80.3 -> a local sink.

What this proves: the path works end to end locally and that what the Agent flushed matches what the listener submitted.
What it does not prove: anything at a Datadog backend (percentiles, merging across hosts, billing).
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Any

import pytest

from fs_rtp_listener.config import Config
from fs_rtp_listener.listener import Listener

from ..conftest import EVENT_USER, UdpSink
from .conftest import Env, wait_for


def make_config(env: Env, **kw: Any) -> Config:
    base: dict[str, Any] = {
        'host': '127.0.0.1',
        'port': env.bed.port,
        'username': EVENT_USER,
        'password': env.bed.restricted_password,
        'statsd_port': env.statsd_port,
        'health_interval': 5.0,
        'backoff_min': 1.0,
        'backoff_max': 4.0,
    }
    base.update(kw)
    return Config(**base)


@pytest.fixture(scope='module')
def listener(env: Env) -> Any:
    instance = Listener(make_config(env))
    instance.start()
    assert wait_for(lambda: instance.connected, 30)
    yield instance
    instance.stop()


def outcomes(listener: Listener) -> dict[str, int]:
    return dict(listener.snapshot()['outcomes'])  # type: ignore[call-overload]


def flush_wait() -> None:
    time.sleep(28)  # two Agent flush intervals plus the listener's health interval


def test_concurrent_calls_codecs_no_media_and_short_calls(
    env: Env, listener: Listener, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    start = outcomes(listener)
    mos_before, quality_before = (
        env.agent_sketch_count('freeswitch.rtp.mos'),
        env.agent_sketch_count('freeswitch.rtp.quality_percentage'),
    )
    submitted_before = listener.snapshot()['counters']['submitted_mos']  # type: ignore[index]
    media: list[str] = []
    variants = ['', 'absolute_codec_string=PCMA', 'absolute_codec_string=speex@16000h@20i', 'rtp_secure_media=true']
    for i in range(12):  # 12 concurrent media calls = 24 legs, across codecs and SRTP
        media.append(env.call(variables=variants[i % 4]))
    parked = [env.call(app='&park') for _ in range(3)]
    short = [env.call(app='&park') for _ in range(3)]
    env.kill(short)  # hung up right after answer: far less than one second of media
    for _ in range(2):
        env.call(dest='5002', app='&park')  # unanswered: events without media variables
    time.sleep(10)
    env.kill(media + parked)
    expected = {'valid': 24, 'no_media': 12, 'missing_fields': 4}
    assert wait_for(lambda: sum(outcomes(listener).values()) - sum(start.values()) >= 40, 40), outcomes(listener)
    got = {k: outcomes(listener)[k] - start[k] for k in outcomes(listener)}
    assert got['valid'] == expected['valid'], got
    assert got['no_media'] == expected['no_media'] and got['missing_fields'] == expected['missing_fields'], got
    assert got['invalid_value'] == got['non_finite'] == got['out_of_range'] == got['short'] == 0, got
    counters = listener.snapshot()['counters']
    assert counters['dropped'] == 0 and counters['duplicates'] == 0  # type: ignore[index]
    flush_wait()
    mos_after = env.agent_sketch_count('freeswitch.rtp.mos')
    quality_after = env.agent_sketch_count('freeswitch.rtp.quality_percentage')
    submitted = counters['submitted_mos'] - submitted_before  # type: ignore[index]
    assert submitted == 24
    assert mos_after - mos_before == 24 and quality_after - quality_before == 24  # Agent flushed what was submitted
    # tags and values as the Agent saw them
    from rtp_agent_lib import sketch_points  # type: ignore[import-not-found]

    points = [p for p in sketch_points(env.rig.payloads(), 'freeswitch.rtp.') if p['metric'] == 'freeswitch.rtp.mos']
    tag_sets = {tuple(p['tags']) for p in points}
    assert all(len(t) == 2 for t in tag_sets)
    assert {tag.split(':')[1] for t in tag_sets for tag in t if tag.startswith('codec:')} <= {'pcmu', 'pcma', 'speex'}
    assert {tag.split(':')[1] for t in tag_sets for tag in t if tag.startswith('direction:')} == {'inbound', 'outbound'}
    assert all(p['min'] >= 4.0 and p['max'] <= 4.5 for p in points)  # clean loopback media
    # nothing sensitive was logged
    assert env.bed.restricted_password not in caplog.text
    assert 'origination' not in caplog.text and 'sofia/' not in caplog.text


def test_reconnect_gap_and_missed_events_are_visible_and_not_replayed(env: Env, listener: Listener) -> None:
    before = listener.snapshot()
    reconnects = before['counters']['reconnects']  # type: ignore[index]
    uid = env.call()  # one call, two legs, will hang up while the listener is disconnected
    time.sleep(3)
    listener.force_disconnect()
    assert wait_for(lambda: not listener.connected, 5)
    env.kill([uid])  # hung up inside the gap: FreeSWITCH delivers it to nobody
    assert wait_for(lambda: listener.connected, 20)
    time.sleep(2)
    after = listener.snapshot()
    assert after['counters']['reconnects'] == reconnects + 1  # type: ignore[index]
    assert after['gap_seconds_total'] > before['gap_seconds_total']  # type: ignore[operator]
    assert after['counters']['events'] == before['counters']['events']  # type: ignore[index]  # the two legs were lost
    assert outcomes(listener) == before['outcomes']
    flush_wait()
    assert env.agent_gauge_last('reconnects') == after['counters']['reconnects']  # type: ignore[index]
    assert float(env.agent_gauge_last('gap_seconds') or 0) >= after['gap_seconds_total'] - 6  # type: ignore[operator]


def test_freeswitch_restart_is_survived(env: Env, listener: Listener) -> None:
    reconnects = listener.snapshot()['counters']['reconnects']  # type: ignore[index]
    valid = outcomes(listener)['valid']
    env.bed.restart()
    assert wait_for(lambda: listener.snapshot()['counters']['reconnects'] > reconnects, 60)  # type: ignore[index]
    assert wait_for(lambda: listener.connected, 30)
    uid = env.call()
    time.sleep(8)
    env.kill([uid])
    assert wait_for(lambda: outcomes(listener)['valid'] >= valid + 2, 20)


def test_agent_restart_blind_spot_is_quantifiable_from_the_cumulative_gauge(env: Env, listener: Listener) -> None:
    flush_wait()
    submitted_before = listener.snapshot()['counters']['submitted_mos']  # type: ignore[index]
    uids = [env.call() for _ in range(4)]
    time.sleep(6)
    subprocess.run(['docker', 'restart', '-t', '1', env.rig.agent], capture_output=True)
    env.kill(uids)  # their hangup events arrive while the Agent's DogStatsD is not listening
    assert wait_for(lambda: listener.snapshot()['counters']['submitted_mos'] >= submitted_before + 8, 30)  # type: ignore[index]
    submitted = listener.snapshot()['counters']['submitted_mos']  # type: ignore[index]
    assert wait_for(lambda: 'DogStatsD' in env.rig.status(), 120)
    time.sleep(1)
    again = [env.call() for _ in range(2)]  # after the Agent is back, delivery resumes
    time.sleep(6)
    env.kill(again)
    assert wait_for(lambda: listener.snapshot()['counters']['submitted_mos'] >= submitted + 4, 30)  # type: ignore[index]
    time.sleep(35)
    total_submitted = listener.snapshot()['counters']['submitted_mos']  # type: ignore[index]
    observed = env.agent_sketch_count('freeswitch.rtp.mos')
    gauge = env.agent_gauge_last('submitted_mos')
    assert gauge == total_submitted  # the cumulative gauge survived because a later datagram carried it
    lost = total_submitted - observed
    assert lost >= 0
    # The listener cannot see this loss itself: UDP gave no error, which is the documented blind spot.
    print(
        'submitted',
        total_submitted,
        'agent_observed',
        observed,
        'lost_between_listener_and_agent',
        lost,
        'send_errors',
        listener.snapshot()['counters']['send_errors'],  # type: ignore[index]
    )


def test_queue_overflow_with_real_events_is_counted_and_conserved(env: Env) -> None:
    sink = UdpSink()  # SIMULATED Agent: the overflow needs a private statsd target, real FreeSWITCH events
    small = Listener(make_config(env, queue_size=3, statsd_port=sink.port, health_interval=0.5))
    small._emit_hook = lambda: time.sleep(0.2)
    small.start()
    try:
        assert wait_for(lambda: small.connected, 30)
        uids = [env.call() for _ in range(10)]
        time.sleep(5)
        env.kill(uids)
        assert wait_for(lambda: small.snapshot()['counters']['events'] >= 20, 30)  # type: ignore[index]
        assert wait_for(lambda: small.snapshot()['queue_depth'] == 0, 30)
        time.sleep(1)
        snap = small.snapshot()
        events, dropped = snap['counters']['events'], snap['counters']['dropped']  # type: ignore[index]
        processed = sum(snap['outcomes'].values())  # type: ignore[attr-defined]
        assert dropped > 0
        assert events == processed + dropped  # nothing vanished silently
        assert snap['max_queue_depth'] == 3
    finally:
        small.stop()
        sink.stop()
