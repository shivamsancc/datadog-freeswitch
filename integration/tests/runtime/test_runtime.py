# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Runtime verification against a disposable FreeSWITCH 1.10.12 (see ``testbed.py``).

Run with ``FS_RUNTIME_TESTS=1 pytest tests/runtime``. Everything here talks to a container created for the run, over
the container's own loopback (real SIP and RTP between two sofia profiles); nothing touches the lab or any carrier.
Ground truth is always read with a separate, full-control connection to the same disposable instance.
"""

from __future__ import annotations

import re
import socket
import subprocess
import time
from typing import Any, Callable

import pytest

from datadog_checks.base.stubs.aggregator import AggregatorStub
from datadog_checks.dev.utils import get_metadata_metrics
from datadog_checks.freeswitch import FreeswitchCheck

from .conftest import wait_for
from .testbed import DOMAIN, RESTRICTED_USER, UNRESTRICTED_USER, Testbed

Run = Callable[..., None]


def make_check(bed: Testbed, **overrides: Any) -> FreeswitchCheck:
    instance: dict[str, Any] = {
        'host': '127.0.0.1',
        'port': bed.port,
        'username': '{}@{}'.format(RESTRICTED_USER, DOMAIN),
        'password': bed.restricted_password,
        'tags': ['tier:runtime-test'],
        'collect_bridged_calls': True,  # opt-in metric; the default (off) is checked in the relay test below
    }
    instance.update(overrides)
    return FreeswitchCheck('freeswitch', {}, [instance])


def value(aggregator: AggregatorStub, name: str) -> float:
    samples = aggregator.metrics(name)
    assert len(samples) == 1, '{}: expected one sample, got {}'.format(name, len(samples))
    return samples[0].value


def collect(bed: Testbed, dd_run_check: Run, aggregator: AggregatorStub, **overrides: Any) -> dict[str, float]:
    aggregator.reset()
    dd_run_check(make_check(bed, **overrides))
    return {name: aggregator.metrics(name)[0].value for name in aggregator.metric_names}


def count_total(text: str) -> int:
    match = re.search(r'([0-9]+) total\.\s*$', text.strip())
    assert match, text[:200]
    return int(match.group(1))


def status_numbers(bed: Testbed) -> dict[str, int]:
    text = bed.raw('status')
    return {
        'created': int(re.search(r'^(\d+) session\(s\) since startup', text, re.M).group(1)),  # type: ignore[union-attr]
        'current': int(re.search(r'^(\d+) session\(s\) - peak', text, re.M).group(1)),  # type: ignore[union-attr]
        'peak': int(re.search(r'session\(s\) - peak (\d+),', text).group(1)),  # type: ignore[union-attr]
        'limit': int(re.search(r'^(\d+) session\(s\) max', text, re.M).group(1)),  # type: ignore[union-attr]
        'rate_limit': int(re.search(r'out of max (\d+),', text).group(1)),  # type: ignore[union-attr]
    }


def settle(bed: Testbed, channels: int, bridged: int | None = None) -> None:
    """The ``show`` tables are written asynchronously (about a second behind call setup); wait for them."""

    def ready() -> bool:
        if count_total(bed.raw('show channels count')) != channels:
            return False
        return bridged is None or count_total(bed.raw('show bridged_calls count')) == bridged

    assert wait_for(ready, timeout=20), 'show tables did not settle'


def call(bed: Testbed, application: str, destination: str = '5000') -> str:
    assert wait_for(
        lambda: re.search(r'^Status\s+UP', bed.raw('sofia status gateway loopreg'), re.M) is not None, timeout=90
    ), 'test gateway is not up'
    reply = bed.raw('originate sofia/gateway/loopreg/{} {}'.format(destination, application))
    assert reply.startswith('+OK '), reply[:80]
    return reply.split()[1]


@pytest.fixture(autouse=True)
def _no_calls(testbed: Testbed) -> Any:
    testbed.raw('hupall')
    assert wait_for(lambda: count_total(testbed.raw('show channels count')) == 0)
    yield
    testbed.raw('hupall')


# ---------------------------------------------------------------------------------------------------------


def test_idle_metrics_match_ground_truth_through_the_restricted_account(
    testbed: Testbed, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    got = collect(testbed, dd_run_check, aggregator)
    truth = status_numbers(testbed)
    assert got['freeswitch.can_connect'] == 1
    assert got['freeswitch.collection.errors'] == 0
    assert got['freeswitch.uptime'] > 0
    assert got['freeswitch.channels.active'] == 0
    assert got['freeswitch.sessions.active'] == 0
    assert got['freeswitch.sessions.limit'] == truth['limit'] == testbed.max_sessions
    assert got['freeswitch.sessions.rate.limit'] == truth['rate_limit'] == testbed.sessions_per_second
    assert got['freeswitch.sessions.created'] == truth['created']
    assert got['freeswitch.sessions.peak'] == truth['peak']
    # the gateway `loopreg` registered with the `edge` profile of the same instance
    assert got['freeswitch.registrations'] == count_total(testbed.raw('show registrations count')) == 1
    assert got['freeswitch.calls.bridged'] == 0
    aggregator.assert_metrics_using_metadata(get_metadata_metrics())
    for name, samples in aggregator._metrics.items():
        for sample in samples:
            assert set(sample.tags) == {
                'freeswitch_host:127.0.0.1',
                'freeswitch_port:{}'.format(testbed.port),
                'tier:runtime-test',
            }, name


def test_metric_types_as_emitted_by_a_real_run(testbed: Testbed, dd_run_check: Run, aggregator: AggregatorStub) -> None:
    collect(testbed, dd_run_check, aggregator)
    assert aggregator.metrics('freeswitch.sessions.created')[0].type == aggregator.MONOTONIC_COUNT
    for name in (
        'freeswitch.sessions.active',
        'freeswitch.sessions.peak',
        'freeswitch.sessions.limit',
        'freeswitch.sessions.rate.limit',
        'freeswitch.registrations',
        'freeswitch.calls.bridged',
        'freeswitch.channels.active',
    ):
        assert aggregator.metrics(name)[0].type == aggregator.GAUGE, name


def test_loaded_state_matches_ground_truth(testbed: Testbed, dd_run_check: Run, aggregator: AggregatorStub) -> None:
    before = status_numbers(testbed)
    call(testbed, '&echo')  # one SIP call: two legs (outbound on `internal`, inbound on `edge`)
    call(testbed, '&bridge(sofia/gateway/loopreg/5000)', '5001')  # a bridge between two outbound SIP legs
    settle(testbed, 6, 1)

    got = collect(testbed, dd_run_check, aggregator)
    channels = count_total(testbed.raw('show channels count'))
    assert channels == 6
    assert got['freeswitch.channels.active'] == channels
    assert got['freeswitch.sessions.active'] == channels  # one session per channel in this FreeSWITCH
    assert got['freeswitch.calls.bridged'] == count_total(testbed.raw('show bridged_calls count')) == 1
    assert got['freeswitch.sessions.peak'] >= channels
    assert got['freeswitch.sessions.created'] - before['created'] == channels
    # `show calls count` is NOT a logical-call count: it is channels minus bridged pairs. Recorded as evidence.
    assert count_total(testbed.raw('show calls count')) == channels - 1


def test_bridged_calls_scales_with_the_number_of_bridges(
    testbed: Testbed, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    for _ in range(3):
        call(testbed, '&bridge(sofia/gateway/loopreg/5000)', '5001')
    settle(testbed, 12, 3)
    got = collect(testbed, dd_run_check, aggregator)
    assert got['freeswitch.calls.bridged'] == 3
    assert got['freeswitch.channels.active'] == 12
    # an unbridged (parked / echo) call is not a bridged call
    call(testbed, '&echo')
    settle(testbed, 14, 3)
    assert collect(testbed, dd_run_check, aggregator)['freeswitch.calls.bridged'] == 3


def test_peak_is_a_high_water_mark_that_survives_hangup(
    testbed: Testbed, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    for _ in range(2):
        call(testbed, '&echo')
    settle(testbed, 4)
    during = collect(testbed, dd_run_check, aggregator)
    testbed.raw('hupall')
    assert wait_for(lambda: count_total(testbed.raw('show channels count')) == 0)
    after = collect(testbed, dd_run_check, aggregator)
    assert after['freeswitch.sessions.active'] == 0
    assert after['freeswitch.sessions.peak'] >= during['freeswitch.sessions.active'] >= 4
    # the cumulative counter never goes down while the process runs
    assert after['freeswitch.sessions.created'] >= during['freeswitch.sessions.created']


def test_registration_count_follows_the_gateway(
    testbed: Testbed, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    assert collect(testbed, dd_run_check, aggregator)['freeswitch.registrations'] == 1
    testbed.raw('sofia profile internal killgw loopreg')
    try:
        assert wait_for(lambda: count_total(testbed.raw('show registrations count')) == 0)
        assert collect(testbed, dd_run_check, aggregator)['freeswitch.registrations'] == 0
    finally:
        testbed.raw('sofia profile internal rescan')
        assert wait_for(lambda: count_total(testbed.raw('show registrations count')) == 1, timeout=60)
    assert collect(testbed, dd_run_check, aggregator)['freeswitch.registrations'] == 1


def test_shared_password_mode_collects_the_same_metrics(
    testbed: Testbed, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    got = collect(testbed, dd_run_check, aggregator, username=None, password=testbed.password)
    assert got['freeswitch.can_connect'] == 1
    assert got['freeswitch.sessions.limit'] == testbed.max_sessions


# -- security and failure --------------------------------------------------------------------------------------


def test_account_without_the_api_restriction_is_refused_before_any_command(
    testbed: Testbed, dd_run_check: Run, aggregator: AggregatorStub, caplog: pytest.LogCaptureFixture
) -> None:
    got = collect(
        testbed,
        dd_run_check,
        aggregator,
        username='{}@{}'.format(UNRESTRICTED_USER, DOMAIN),
    )
    assert got['freeswitch.can_connect'] == 0
    assert 'freeswitch.uptime' not in got
    assert [t for m in aggregator.metrics('freeswitch.collection.errors') for t in m.tags if 'error_type' in t] == [
        'error_type:auth'
    ]
    assert testbed.restricted_password not in caplog.text


def test_wrong_password_is_an_auth_error_without_leaking_it(
    testbed: Testbed, dd_run_check: Run, aggregator: AggregatorStub, caplog: pytest.LogCaptureFixture
) -> None:
    got = collect(testbed, dd_run_check, aggregator, password='definitely-not-the-password')
    assert got['freeswitch.can_connect'] == 0
    assert 'error_type:auth' in [t for m in aggregator.metrics('freeswitch.collection.errors') for t in m.tags]
    assert 'definitely-not-the-password' not in caplog.text


def test_server_denies_commands_outside_the_restricted_account(testbed: Testbed) -> None:
    """The server side of the allowlist, independent of the client: first-word permission matching."""
    sock = socket.create_connection(('127.0.0.1', testbed.port), timeout=10)
    sock.settimeout(10)
    buf = b''

    def message() -> tuple[str, bytes]:
        nonlocal buf
        while b'\n\n' not in buf:
            buf += sock.recv(65536)
        head, buf = buf.split(b'\n\n', 1)
        length = re.search(rb'Content-Length: (\d+)', head)
        n = int(length.group(1)) if length else 0
        while len(buf) < n:
            buf += sock.recv(65536)
        body, buf = buf[:n], buf[n:]
        return head.decode(), body

    with sock:
        message()
        sock.sendall('userauth {}@{}:{}\n\n'.format(RESTRICTED_USER, DOMAIN, testbed.restricted_password).encode())
        head, _ = message()
        assert 'Allowed-API: uptime,show' in head
        denied = {}
        for command in (
            'status',
            'sofia status',
            'sofia status gateway',
            'version short',
            'hupall',
            'originate x &park',
        ):
            sock.sendall('api {}\n\n'.format(command).encode())
            head, _ = message()
            denied[command] = 'permission denied' in head
        sock.sendall(b'api show channels count\n\n')
        head, body = message()
    assert all(denied.values()), denied
    assert 'api/response' in head and body.strip().endswith(b'total.')


def test_paused_freeswitch_is_a_bounded_timeout_and_recovers(
    testbed: Testbed, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    subprocess.run(['docker', 'pause', testbed.name], check=True, capture_output=True)
    try:
        start = time.monotonic()
        got = collect(testbed, dd_run_check, aggregator, connect_timeout=1.0, read_timeout=1.0)
        elapsed = time.monotonic() - start
    finally:
        subprocess.run(['docker', 'unpause', testbed.name], check=True, capture_output=True)
    assert got['freeswitch.can_connect'] == 0
    assert elapsed < 6
    kinds = [t for m in aggregator.metrics('freeswitch.collection.errors') for t in m.tags if 'error_type' in t]
    assert kinds and kinds[0] in ('error_type:timeout', 'error_type:connect')
    assert wait_for(lambda: collect(testbed, dd_run_check, aggregator)['freeswitch.can_connect'] == 1, timeout=20)


def test_connection_refused_when_nothing_listens(dd_run_check: Run, aggregator: AggregatorStub) -> None:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    check = FreeswitchCheck('freeswitch', {}, [{'host': '127.0.0.1', 'port': port, 'password': 'x'}])
    dd_run_check(check)
    aggregator.assert_metric('freeswitch.can_connect', 0, count=1)
    assert 'error_type:connect' in [t for m in aggregator.metrics('freeswitch.collection.errors') for t in m.tags]


class _RecordingProxy:
    """A loopback TCP relay that records every client-to-server line. Sits between the check and the real FS."""

    def __init__(self, target_port: int) -> None:
        import threading

        self.lines: list[str] = []
        self._target = target_port
        self._listener = socket.socket()
        self._listener.bind(('127.0.0.1', 0))
        self._listener.listen(4)
        self.port = self._listener.getsockname()[1]
        self._threads = [threading.Thread(target=self._accept, daemon=True)]
        self._threads[0].start()

    def _accept(self) -> None:
        import threading

        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            upstream = socket.create_connection(('127.0.0.1', self._target), timeout=10)
            for src, dst, record in ((client, upstream, True), (upstream, client, False)):
                thread = threading.Thread(target=self._pump, args=(src, dst, record), daemon=True)
                thread.start()

    def _pump(self, src: socket.socket, dst: socket.socket, record: bool) -> None:
        buf = b''
        try:
            while True:
                chunk = src.recv(65536)
                if not chunk:
                    break
                if record:
                    buf += chunk
                    while b'\n\n' in buf:
                        line, buf = buf.split(b'\n\n', 1)
                        self.lines.append(line.decode('utf-8', 'replace'))
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            for sock in (src, dst):
                try:
                    sock.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._listener.close()


def _wire_lines(testbed: Testbed, dd_run_check: Run, **overrides: Any) -> list[str]:
    proxy = _RecordingProxy(testbed.port)
    try:
        instance: dict[str, Any] = {
            'host': '127.0.0.1',
            'port': proxy.port,
            'username': '{}@{}'.format(RESTRICTED_USER, DOMAIN),
            'password': testbed.restricted_password,
            **overrides,
        }
        check = FreeswitchCheck('freeswitch', {}, [instance])
        for _ in range(3):
            dd_run_check(check)
        return list(proxy.lines)
    finally:
        proxy.close()


def test_check_sends_only_allowlisted_commands_to_a_real_server(
    testbed: Testbed, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    """Every line the check puts on the wire toward a real FreeSWITCH: one userauth, then allowlisted api lines."""
    from datadog_checks.freeswitch import esl

    lines = _wire_lines(testbed, dd_run_check, collect_bridged_calls=True)
    aggregator.assert_metric('freeswitch.can_connect', 1, count=3)
    assert lines[0].startswith('userauth ') and len([x for x in lines if not x.startswith('api ')]) == 1
    commands = {x[len('api ') :] for x in lines if x.startswith('api ')}
    assert commands == esl.ALLOWED_COMMANDS  # all five were sent, and nothing else
    assert len(lines) == 1 + 3 * 5  # one connection reused for three runs
    assert not any('hupall' in x or 'originate' in x or 'sofia' in x or 'event' in x for x in lines)


def test_default_configuration_never_requests_the_bridged_listing_from_a_real_server(
    testbed: Testbed, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    call(testbed, '&bridge(sofia/gateway/loopreg/5000)', '5001')
    settle(testbed, 4, 1)
    lines = _wire_lines(testbed, dd_run_check)
    commands = {x[len('api ') :] for x in lines if x.startswith('api ')}
    assert commands == {'uptime s', 'show channels count', 'show status', 'show registrations count'}
    assert len(lines) == 1 + 3 * 4
    assert aggregator.metrics('freeswitch.calls.bridged') == []


def test_sessions_created_across_a_real_restart_submits_the_baseline_reset(
    testbed: Testbed, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    """Keep this test last: it restarts the disposable FreeSWITCH (a new process; the generated config is reused)."""
    call(testbed, '&echo')
    call(testbed, '&echo')
    check_before = make_check(testbed)
    dd_run_check(check_before)
    before = status_numbers(testbed)['created']
    assert before >= 4

    testbed.restart()
    assert wait_for(lambda: count_total(testbed.raw('show channels count')) == 0)
    # the same check object (as the Agent keeps it) now sees a restarted server on the same address
    check_before._client._port = testbed.port  # type: ignore[union-attr]
    check_before.instance['port'] = testbed.port
    aggregator.reset()
    dd_run_check(check_before)
    samples = [m.value for m in aggregator.metrics('freeswitch.sessions.created')]
    assert samples == [0, 0]  # the reset sample, then the raw value of the new process
    assert status_numbers(testbed)['created'] == 0
