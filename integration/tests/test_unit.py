# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import annotations

import logging
import pathlib
import socket
import threading
import time
from typing import Any, Callable

import pytest

from datadog_checks.base.stubs.aggregator import AggregatorStub
from datadog_checks.dev.utils import get_metadata_metrics
from datadog_checks.freeswitch import FreeswitchCheck, __version__, esl
from datadog_checks.freeswitch.esl import EslClient

from .conftest import SENTINEL_PASSWORD
from .fake_esl import (
    DISCONNECT,
    FakeEslServer,
    api_response,
    command_reply,
    frame,
    make_handler,
)

BASE_TAGS = ['freeswitch_host:127.0.0.1', 'tier:test']
ALL_METRICS = [
    'freeswitch.can_connect',
    'freeswitch.uptime',
    'freeswitch.channels.active',
    'freeswitch.collection.duration',
    'freeswitch.collection.errors',
]

MakeServer = Callable[..., FakeEslServer]
MakeCheck = Callable[..., FreeswitchCheck]


def tags_for(server: FakeEslServer, *extra: str) -> list[str]:
    return ['freeswitch_host:127.0.0.1', 'freeswitch_port:{}'.format(server.port), 'tier:test', *extra]


def run(dd_run_check: Callable[..., None], check: FreeswitchCheck) -> None:
    dd_run_check(check)


def assert_failed(aggregator: AggregatorStub, server: FakeEslServer, error_type: str, runs: int = 1) -> None:
    aggregator.assert_metric('freeswitch.can_connect', 0, count=runs, tags=tags_for(server))
    aggregator.assert_metric(
        'freeswitch.collection.errors',
        1,
        count=runs,
        tags=tags_for(server, 'error_type:' + error_type),
        metric_type=aggregator.COUNT,
    )
    aggregator.assert_metric('freeswitch.collection.duration', count=runs)
    aggregator.assert_metric('freeswitch.uptime', count=0)
    aggregator.assert_metric('freeswitch.channels.active', count=0)
    aggregator.assert_all_metrics_covered()


def one_failure(
    server: FakeEslServer,
    make_check: MakeCheck,
    dd_run_check: Callable[..., None],
    aggregator: AggregatorStub,
    error_type: str,
    **overrides: Any,
) -> FreeswitchCheck:
    check = make_check(server, **overrides)
    dd_run_check(check)
    assert_failed(aggregator, server, error_type)
    return check


# ---------------------------------------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------------------------------------


def test_version() -> None:
    assert __version__


def test_success_metrics_and_wire(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    server = make_server()
    check = make_check(server)
    dd_run_check(check)

    tags = tags_for(server)
    aggregator.assert_metric('freeswitch.can_connect', 1, tags=tags, count=1, metric_type=aggregator.GAUGE)
    aggregator.assert_metric('freeswitch.uptime', 3401, tags=tags, count=1, metric_type=aggregator.GAUGE)
    aggregator.assert_metric('freeswitch.channels.active', 4, tags=tags, count=1, metric_type=aggregator.GAUGE)
    aggregator.assert_metric('freeswitch.collection.errors', 0, tags=tags, count=1, metric_type=aggregator.COUNT)
    aggregator.assert_metric('freeswitch.collection.duration', tags=tags, count=1, metric_type=aggregator.GAUGE)
    aggregator.assert_all_metrics_covered()
    aggregator.assert_metrics_using_metadata(get_metadata_metrics())

    duration = aggregator.metrics('freeswitch.collection.duration')[0].value
    assert 0 <= duration < 5
    assert server.lines == ['auth ' + SENTINEL_PASSWORD, 'api uptime s', 'api show channels count']


def test_connection_reuse_across_runs(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    server = make_server()
    check = make_check(server)
    for _ in range(5):
        dd_run_check(check)

    assert server.connections == 1
    assert server.api_lines == ['api uptime s', 'api show channels count'] * 5
    aggregator.assert_metric('freeswitch.can_connect', 1, count=5)
    aggregator.assert_metric('freeswitch.collection.errors', 0, count=5)


@pytest.mark.parametrize(
    'body, expected',
    [
        (b'\n0 total.\n', 0),
        (b'\n4 total.\n', 4),
        (b'40 total.\n', 40),
        (b'  \n\n  12 total.  \n\n', 12),
        (b'\r\n7 total.\r\n', 7),
    ],
)
def test_channel_count_parsing(
    body: bytes,
    expected: int,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Callable[..., None],
    aggregator: AggregatorStub,
) -> None:
    server = make_server()
    server.responses['show channels count'] = body
    dd_run_check(make_check(server))
    aggregator.assert_metric('freeswitch.channels.active', expected, count=1)
    aggregator.assert_metric('freeswitch.can_connect', 1, count=1)


@pytest.mark.parametrize('body, expected', [(b'0\n', 0), (b'3401', 3401), (b' 86400 \n', 86400)])
def test_uptime_parsing(
    body: bytes,
    expected: int,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Callable[..., None],
    aggregator: AggregatorStub,
) -> None:
    server = make_server()
    server.responses['uptime s'] = body
    dd_run_check(make_check(server))
    aggregator.assert_metric('freeswitch.uptime', expected, count=1)


@pytest.mark.parametrize(
    'command, body',
    [
        ('uptime s', b'up 1 hour\n'),
        ('uptime s', b'-5\n'),
        ('uptime s', b'12.5\n'),
        ('uptime s', b'\xff\xfe\n'),
        ('uptime s', b'\n'),
        ('show channels count', b'4\n'),
        ('show channels count', b'many total.\n'),
        ('show channels count', b'4 total.\n5 total.\n'),
        ('show channels count', b'-1 total.\n'),
        ('show channels count', b'\xff total.\n'),
        ('show channels count', b'{"row_count":0}'),
    ],
)
def test_unparseable_responses(
    command: str,
    body: bytes,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Callable[..., None],
    aggregator: AggregatorStub,
) -> None:
    server = make_server()
    server.responses[command] = body
    one_failure(server, make_check, dd_run_check, aggregator, 'parse')


# ---------------------------------------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------------------------------------


def test_invalid_password_is_not_retried(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    server = make_server(password='some-other-password')
    one_failure(server, make_check, dd_run_check, aggregator, 'auth')
    assert server.connections == 1
    assert server.api_lines == []


def test_userauth_success_uses_userauth_line(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    server = make_server(users={'monitor@example.org': SENTINEL_PASSWORD})
    dd_run_check(make_check(server, username='monitor@example.org'))
    aggregator.assert_metric('freeswitch.can_connect', 1, count=1)
    assert server.lines[0] == 'userauth monitor@example.org:' + SENTINEL_PASSWORD


def test_userauth_rejected_does_not_fall_back_to_shared_password(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    # The shared password is valid on this server; the directory user is not.
    server = make_server(users={})
    one_failure(server, make_check, dd_run_check, aggregator, 'auth', username='monitor@example.org')
    assert server.connections == 1
    assert [line.split(' ')[0] for line in server.lines] == ['userauth']


def test_permission_denied_is_auth_error(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    def on_api(conn: Any, server: FakeEslServer, cmd: str) -> bool:
        conn.send(command_reply('-ERR permission denied'))
        return True

    server = make_server(make_handler(on_api=on_api))
    one_failure(server, make_check, dd_run_check, aggregator, 'auth')
    assert server.connections == 1


@pytest.mark.parametrize('username', ['monitor', '@example.org', 'monitor@', 'a:b@example.org', 'a b@example.org'])
def test_invalid_username_is_a_configuration_error(
    username: str, make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None]
) -> None:
    server = make_server()
    check = make_check(server, username=username)
    with pytest.raises(Exception) as exc:
        dd_run_check(check)
    assert SENTINEL_PASSWORD not in str(exc.value)
    assert server.connections == 0


@pytest.mark.parametrize('password', ['line1\nline2', 'with\rcr', 'tab\tchar'])
def test_password_with_control_characters_is_rejected(
    password: str, make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None]
) -> None:
    server = make_server()
    check = make_check(server, password=password)
    with pytest.raises(Exception) as exc:
        dd_run_check(check)
    assert 'line1' not in str(exc.value)
    assert server.connections == 0


# ---------------------------------------------------------------------------------------------------------
# Restricted-account policy (fail closed)
# ---------------------------------------------------------------------------------------------------------


def _userauth_server(make_server: MakeServer, **attrs: Any) -> FakeEslServer:
    server = make_server(users={'monitor@example.org': SENTINEL_PASSWORD})
    for name, value in attrs.items():
        setattr(server, name, value)
    return server


@pytest.mark.parametrize(
    'advertised', ['uptime,show', 'show,uptime', 'uptime show', 'show uptime', 'uptime,uptime,show']
)
def test_userauth_accepts_exactly_the_required_permissions(
    advertised: str,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Callable[..., None],
    aggregator: AggregatorStub,
) -> None:
    server = _userauth_server(make_server, allowed_api=advertised)
    dd_run_check(make_check(server, username='monitor@example.org'))
    aggregator.assert_metric('freeswitch.can_connect', 1, count=1)


@pytest.mark.parametrize(
    'advertised',
    [
        'all',
        'ALL',
        None,  # header absent: the server did not say what the account may do
        '',
        'uptime',  # cannot do the channel query; refuse early
        'show',
        'uptime,show,status',
        'uptime,show,system',
        'uptime, show',  # server would split on the space and see "uptime,"
        'uptime,,show',
        'uptime,show,',
        ',uptime,show',
        'Uptime,show',
    ],
)
def test_userauth_account_with_other_permissions_is_refused_before_any_command(
    advertised: str | None,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Callable[..., None],
    aggregator: AggregatorStub,
) -> None:
    server = _userauth_server(make_server, allowed_api=advertised)
    one_failure(server, make_check, dd_run_check, aggregator, 'auth', username='monitor@example.org')
    assert server.api_lines == []
    assert [line.split(' ')[0] for line in server.lines] == ['userauth']  # and no fallback to `auth`
    assert server.connections == 1


@pytest.mark.parametrize(
    'reply',
    [
        # trailing padding could hide entries the server truncated from the advertised line
        b'Content-Type: command/reply\nReply-Text: +OK accepted\nAllowed-API: uptime,show   \n\n',
        b'Content-Type: command/reply\nReply-Text: +OK accepted\nAllowed-API:  uptime,show\n\n',
        b'Content-Type: command/reply\nReply-Text: +OK accepted\nAllowed-API:uptime,show\n\n',
        # a duplicated header is ambiguous
        b'Content-Type: command/reply\nReply-Text: +OK accepted\nAllowed-API: uptime,show\nAllowed-API: all\n\n',
        b'Content-Type: command/reply\nReply-Text: +OK accepted\nAllowed-API: all\nAllowed-API: uptime,show\n\n',
    ],
)
def test_userauth_ambiguous_advertisement_is_refused(
    reply: bytes,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Callable[..., None],
    aggregator: AggregatorStub,
) -> None:
    server = _userauth_server(make_server, userauth_reply=reply)
    one_failure(server, make_check, dd_run_check, aggregator, 'auth', username='monitor@example.org')
    assert server.api_lines == []


def test_policy_does_not_apply_to_shared_password_mode(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    # The shared password cannot be restricted and the server advertises nothing; the README documents the risk.
    server = make_server()
    dd_run_check(make_check(server))
    aggregator.assert_metric('freeswitch.can_connect', 1, count=1)
    assert server.lines[0].startswith('auth ')


def test_refusal_reason_is_logged_once_without_secrets(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    server = _userauth_server(make_server, allowed_api='all')
    check = make_check(server, username='monitor@example.org')
    for _ in range(3):
        dd_run_check(check)
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert 'error_type=auth' in warnings[0] and 'not restricted' in warnings[0]
    assert SENTINEL_PASSWORD not in caplog.text
    assert 'monitor@example.org' not in caplog.text


def test_policy_still_leaves_the_client_allowlist_in_force(make_server: MakeServer) -> None:
    server = _userauth_server(make_server)
    client = EslClient('127.0.0.1', server.port, SENTINEL_PASSWORD, 'monitor@example.org', 0.5, 0.3)
    client.connect(time.monotonic() + 2)
    with pytest.raises(esl.EslCommandNotAllowed):
        client.api('version', time.monotonic() + 2)
    client.close()
    assert esl.REQUIRED_API_PERMISSIONS == {'uptime', 'show'}


# ---------------------------------------------------------------------------------------------------------
# Connection failures and recovery
# ---------------------------------------------------------------------------------------------------------


def _closed_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_connection_refused(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    server = make_server()
    check = make_check(server, port=_closed_port())
    dd_run_check(check)
    aggregator.assert_metric('freeswitch.can_connect', 0, count=1)
    aggregator.assert_metric_has_tag('freeswitch.collection.errors', 'error_type:connect')
    assert server.connections == 0


def test_unresolvable_host_is_a_connect_error(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    check = make_check(make_server(), host='no-such-host.invalid')
    dd_run_check(check)
    aggregator.assert_metric('freeswitch.can_connect', 0, count=1)
    aggregator.assert_metric_has_tag('freeswitch.collection.errors', 'error_type:connect')


def test_disconnect_during_authentication(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    server = make_server(make_handler(on_auth=lambda conn, srv, line: False))
    one_failure(server, make_check, dd_run_check, aggregator, 'connect')
    assert server.connections == 1


def test_disconnect_during_command(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    server = make_server(make_handler(on_api=lambda conn, srv, cmd: False))
    one_failure(server, make_check, dd_run_check, aggregator, 'connect')
    assert server.connections == 1  # a fresh connection that fails is not retried


def test_disconnect_notice_instead_of_greeting(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    server = make_server(make_handler(greeting=DISCONNECT))
    one_failure(server, make_check, dd_run_check, aggregator, 'connect')


def test_disconnect_notice_during_command(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
        conn.send(DISCONNECT)
        return False

    server = make_server(make_handler(on_api=on_api))
    one_failure(server, make_check, dd_run_check, aggregator, 'connect')


def test_restart_between_runs_reconnects_once_and_succeeds(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    server = make_server()
    check = make_check(server)
    dd_run_check(check)
    server.kill_connections()  # simulates FreeSWITCH restarting
    time.sleep(0.1)
    dd_run_check(check)

    assert server.connections == 2
    aggregator.assert_metric('freeswitch.can_connect', 1, count=2)
    aggregator.assert_metric('freeswitch.collection.errors', 0, count=2)
    aggregator.assert_metric('freeswitch.can_connect', 0, count=0)


def test_connection_that_dies_mid_exchange_is_replaced_in_the_same_run(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    state = {'calls': 0}

    def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
        state['calls'] += 1
        # First connection: answer the first run, then drop on the next command without a prior FIN.
        if state['calls'] == 3 and srv.connections == 1:
            return False
        conn.send(api_response(srv.responses[cmd]))
        return True

    server = make_server(make_handler(on_api=on_api))
    check = make_check(server)
    dd_run_check(check)
    dd_run_check(check)

    assert server.connections == 2
    aggregator.assert_metric('freeswitch.can_connect', 1, count=2)
    aggregator.assert_metric('freeswitch.collection.errors', 0, count=2)


def test_recovery_after_failure(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    def on_auth(conn: Any, srv: FakeEslServer, line: str) -> bool:
        if srv.mode == 'reject':
            conn.send(command_reply('-ERR invalid') + DISCONNECT)
            return False
        conn.send(command_reply('+OK accepted'))
        return True

    server = make_server(make_handler(on_auth=on_auth))
    server.mode = 'reject'
    check = make_check(server)
    dd_run_check(check)
    aggregator.assert_metric('freeswitch.can_connect', 0, count=1)
    server.mode = 'good'
    dd_run_check(check)
    dd_run_check(check)
    aggregator.assert_metric('freeswitch.can_connect', 1, count=2)
    assert server.connections == 2


def test_failures_never_cause_a_retry_loop(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
        if srv.mode == 'drop':
            return False
        conn.send(api_response(srv.responses[cmd]))
        return True

    def handler(conn: Any, srv: FakeEslServer, index: int) -> None:
        if srv.mode == 'drop' and index > 1:
            return  # new connections are closed immediately
        make_handler(on_api=on_api)(conn, srv, index)

    server = make_server(handler)
    check = make_check(server)
    dd_run_check(check)
    assert server.connections == 1
    server.mode = 'drop'
    dd_run_check(check)  # reused connection fails, one reconnect also fails
    assert server.connections == 2
    dd_run_check(check)  # nothing is connected: exactly one new attempt
    assert server.connections == 3
    aggregator.assert_metric('freeswitch.can_connect', 0, count=2)


# ---------------------------------------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------------------------------------


def test_no_greeting_times_out(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    server = make_server(make_handler(greeting=None))
    start = time.monotonic()
    one_failure(server, make_check, dd_run_check, aggregator, 'timeout')
    assert time.monotonic() - start < 2


def test_stall_during_authentication_times_out(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    def on_auth(conn: Any, srv: FakeEslServer, line: str) -> bool:
        conn.wait_closed()
        return False

    server = make_server(make_handler(on_auth=on_auth))
    one_failure(server, make_check, dd_run_check, aggregator, 'timeout')


def test_stall_during_command_times_out(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
        conn.wait_closed()
        return False

    server = make_server(make_handler(on_api=on_api))
    one_failure(server, make_check, dd_run_check, aggregator, 'timeout')
    assert server.connections == 1


def test_slow_drip_response_is_bounded_by_one_read_timeout(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
        conn.drip(api_response(b'3401\n'), 0.05)  # arrives, but far too slowly
        return False

    server = make_server(make_handler(on_api=on_api))
    start = time.monotonic()
    one_failure(server, make_check, dd_run_check, aggregator, 'timeout', read_timeout=0.3)
    assert time.monotonic() - start < 1.5


def test_run_deadline_is_below_the_collection_interval(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    server = make_server(make_handler(greeting=None))
    check = make_check(server, connect_timeout=5, read_timeout=5, min_collection_interval=1)
    start = time.monotonic()
    dd_run_check(check)
    assert time.monotonic() - start < 1.5
    aggregator.assert_metric_has_tag('freeswitch.collection.errors', 'error_type:timeout')


# ---------------------------------------------------------------------------------------------------------
# Name resolution, deadlines and logging
# ---------------------------------------------------------------------------------------------------------


def test_ip_literal_is_connected_without_name_resolution(
    monkeypatch: pytest.MonkeyPatch,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Callable[..., None],
    aggregator: AggregatorStub,
) -> None:
    def no_dns(*args: Any, **kwargs: Any) -> None:
        raise AssertionError('name resolution attempted for an IP literal')

    monkeypatch.setattr(socket, 'getaddrinfo', no_dns)
    server = make_server()
    dd_run_check(make_check(server, host='127.0.0.1'))
    aggregator.assert_metric('freeswitch.can_connect', 1, count=1)


def test_ipv6_literal_failure_is_a_connect_error_without_resolution(
    monkeypatch: pytest.MonkeyPatch,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Callable[..., None],
    aggregator: AggregatorStub,
) -> None:
    def no_dns(*args: Any, **kwargs: Any) -> None:
        raise AssertionError('name resolution attempted for an IP literal')

    monkeypatch.setattr(socket, 'getaddrinfo', no_dns)
    dd_run_check(make_check(make_server(), host='::1', port=_closed_port()))
    aggregator.assert_metric('freeswitch.can_connect', 0, count=1)
    aggregator.assert_metric_has_tag('freeswitch.collection.errors', 'error_type:connect')


def test_slow_name_resolution_is_not_followed_by_a_late_connection(
    monkeypatch: pytest.MonkeyPatch,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Callable[..., None],
    aggregator: AggregatorStub,
) -> None:
    real = socket.getaddrinfo

    def slow(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.6)
        return real(*args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', slow)
    server = make_server()
    check = make_check(server, host='localhost', min_collection_interval=0.5)
    dd_run_check(check)

    aggregator.assert_metric('freeswitch.can_connect', 0, count=1)
    aggregator.assert_metric_has_tag('freeswitch.collection.errors', 'error_type:timeout')
    assert server.connections == 0
    # No lingering thread or retry: the next run resolves again exactly once.
    dd_run_check(check)
    assert server.connections == 0


def test_hostname_resolution_failure_is_a_connect_error(
    monkeypatch: pytest.MonkeyPatch,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Callable[..., None],
    aggregator: AggregatorStub,
) -> None:
    def failing(*args: Any, **kwargs: Any) -> None:
        raise socket.gaierror('temporary failure in name resolution of secret-host-detail')

    monkeypatch.setattr(socket, 'getaddrinfo', failing)
    dd_run_check(make_check(make_server(), host='fs.example.org'))
    aggregator.assert_metric('freeswitch.can_connect', 0, count=1)
    aggregator.assert_metric_has_tag('freeswitch.collection.errors', 'error_type:connect')


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.name.endswith('freeswitch')]


def test_persistent_failure_warns_once_not_every_run(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    server = make_server(password='some-other-password')
    check = make_check(server)
    for _ in range(6):
        dd_run_check(check)

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert 'error_type=auth' in warnings[0]
    assert '127.0.0.1:{}'.format(server.port) in warnings[0]
    assert SENTINEL_PASSWORD not in caplog.text
    assert sum('still failing' in r.getMessage() for r in caplog.records) == 5


def test_error_type_change_and_recovery_are_logged(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    def on_auth(conn: Any, srv: FakeEslServer, line: str) -> bool:
        if srv.mode == 'reject':
            conn.send(command_reply('-ERR invalid') + DISCONNECT)
            return False
        if srv.mode == 'drop':
            return False
        conn.send(command_reply('+OK accepted'))
        return True

    server = make_server(make_handler(on_auth=on_auth))
    check = make_check(server)
    server.mode = 'reject'
    dd_run_check(check)
    dd_run_check(check)
    server.mode = 'drop'
    dd_run_check(check)
    server.mode = 'good'
    dd_run_check(check)
    dd_run_check(check)

    warnings = _warnings(caplog)
    assert len(warnings) == 2
    assert 'error_type=auth' in warnings[0] and 'error_type=connect' in warnings[1]
    recovered = [r.getMessage() for r in caplog.records if 'recovered' in r.getMessage()]
    assert len(recovered) == 1 and 'after 3 failed' in recovered[0]
    assert SENTINEL_PASSWORD not in caplog.text


def test_reminder_warning_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from datadog_checks.freeswitch import check as check_module

    monkeypatch.setattr(check_module, 'LOG_REMINDER_SECONDS', 0.3)
    caplog.set_level(logging.DEBUG)
    check = make_check(make_server(password='some-other-password'))
    dd_run_check(check)
    dd_run_check(check)
    assert len(_warnings(caplog)) == 1
    time.sleep(0.35)
    dd_run_check(check)
    assert len(_warnings(caplog)) == 2


# ---------------------------------------------------------------------------------------------------------
# Protocol failures
# ---------------------------------------------------------------------------------------------------------


def _api_reply_handler(payload: bytes, then_close: bool = False) -> Any:
    def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
        conn.send(payload)
        return not then_close

    return make_handler(on_api=on_api)


@pytest.mark.parametrize(
    'name, payload, then_close, error_type',
    [
        ('missing_length', b'Content-Type: api/response\n\n3401\n', False, 'protocol'),
        ('negative_length', b'Content-Type: api/response\nContent-Length: -5\n\n3401\n', False, 'protocol'),
        ('non_numeric_length', b'Content-Type: api/response\nContent-Length: abc\n\n3401\n', False, 'protocol'),
        ('signed_length', b'Content-Type: api/response\nContent-Length: +5\n\n3401\n', False, 'protocol'),
        ('huge_length', b'Content-Type: api/response\nContent-Length: 99999999999\n\n', False, 'protocol'),
        ('oversized_length', b'Content-Type: api/response\nContent-Length: 65537\n\n', False, 'protocol'),
        (
            'duplicate_length',
            b'Content-Type: api/response\nContent-Length: 5\nContent-Length: 5\n\n3401\n',
            False,
            'protocol',
        ),
        ('truncated_body', b'Content-Type: api/response\nContent-Length: 10\n\n340', True, 'connect'),
        ('truncated_then_stall', b'Content-Type: api/response\nContent-Length: 10\n\n340', False, 'timeout'),
        ('wrong_content_type', frame('text/event-plain', b'3401\n'), False, 'protocol'),
        ('missing_content_type', b'Content-Length: 5\n\n3401\n', False, 'protocol'),
        ('malformed_header', b'this is not a header\n\n', False, 'protocol'),
        ('invalid_utf8_header', b'Content-Type: api/response\nX: \xff\xfe\nContent-Length: 0\n\n', False, 'protocol'),
        ('unknown_command', api_response(b'-ERR uptime Command not found!\n'), False, 'protocol'),
        ('unexpected_reply', command_reply('+OK something'), False, 'protocol'),
        ('extra_frame', api_response(b'3401\n') + api_response(b'3401\n'), False, 'protocol'),
        ('oversized_header_no_terminator', b'X-Pad: ' + b'a' * 9000, False, 'protocol'),
        ('oversized_header_terminated', b'X-Pad: ' + b'a' * 9000 + b'\n\n', False, 'protocol'),
        ('oversized_body', frame('api/response', b'0' * 70000), False, 'protocol'),
    ],
    ids=lambda v: v if isinstance(v, str) else ('' if isinstance(v, bytes) else None),
)
def test_protocol_failures(
    name: str,
    payload: bytes,
    then_close: bool,
    error_type: str,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Callable[..., None],
    aggregator: AggregatorStub,
) -> None:
    server = make_server(_api_reply_handler(payload, then_close))
    one_failure(server, make_check, dd_run_check, aggregator, error_type)
    assert server.connections == 1


def test_unexpected_greeting(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    server = make_server(make_handler(greeting=frame('command/reply', b'hello')))
    one_failure(server, make_check, dd_run_check, aggregator, 'protocol')
    assert server.lines == []  # no credentials are sent to something that did not ask for them


def test_unexpected_authentication_reply(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    def on_auth(conn: Any, srv: FakeEslServer, line: str) -> bool:
        conn.send(frame('api/response', b'+OK'))
        return False

    server = make_server(make_handler(on_auth=on_auth))
    one_failure(server, make_check, dd_run_check, aggregator, 'protocol')


def test_failure_closes_the_connection_so_a_late_reply_cannot_desynchronize(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
        if srv.mode == 'late' and cmd == 'uptime s':
            time.sleep(0.5)  # longer than the read timeout; the reply arrives too late
            conn.send(api_response(b'111\n'))
            return True
        conn.send(api_response(srv.responses[cmd]))
        return True

    server = make_server(make_handler(on_api=on_api))
    check = make_check(server)
    dd_run_check(check)
    server.mode = 'late'
    dd_run_check(check)  # times out on the reused connection, reconnect also times out
    server.mode = 'good'
    dd_run_check(check)
    last_uptime = aggregator.metrics('freeswitch.uptime')[-1].value
    assert last_uptime == 3401  # never the late value from the abandoned stream


# ---------------------------------------------------------------------------------------------------------
# Command safety
# ---------------------------------------------------------------------------------------------------------

DISALLOWED = [
    'originate user/1000 &echo',
    'bridge',
    'hupall',
    'uuid_kill abc',
    'system id',
    'bgapi uptime s',
    'fsctl shutdown',
    'shutdown',
    'unload mod_event_socket',
    'eval ${hostname}',
    'sched_api +1 none hupall',
    'status',
    'uptime',
    'uptime s ',
    'UPTIME S',
    'show channels',
    'show channels count as json',
    'uptime s\nhupall',
    'uptime s\r\n\r\napi hupall',
    'show channels count\n\nexit',
    '',
]


@pytest.mark.parametrize('command', DISALLOWED)
def test_client_rejects_commands_outside_the_allowlist(command: str, make_server: MakeServer) -> None:
    server = make_server()
    client = EslClient('127.0.0.1', server.port, SENTINEL_PASSWORD, connect_timeout=0.5, read_timeout=0.3)
    client.connect(time.monotonic() + 2)
    with pytest.raises(esl.EslCommandNotAllowed) as exc:
        client.api(command, time.monotonic() + 2)
    assert command.strip() not in str(exc.value) or not command.strip()
    time.sleep(0.1)
    assert server.api_lines == []
    client.close()


def test_allowlist_is_exactly_two_commands() -> None:
    assert esl.ALLOWED_COMMANDS == {'uptime s', 'show channels count'}


def test_only_expected_lines_reach_the_server_over_many_runs(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None]
) -> None:
    server = make_server()
    check = make_check(server)
    for _ in range(10):
        dd_run_check(check)
    assert set(server.lines) == {'auth ' + SENTINEL_PASSWORD, 'api uptime s', 'api show channels count'}


# ---------------------------------------------------------------------------------------------------------
# Secrets and process safety
# ---------------------------------------------------------------------------------------------------------


def _assert_no_sentinel(aggregator: AggregatorStub, caplog: pytest.LogCaptureFixture) -> None:
    assert SENTINEL_PASSWORD not in caplog.text
    for stubs in aggregator._metrics.values():
        for stub in stubs:
            assert SENTINEL_PASSWORD not in ' '.join(stub.tags)
            assert SENTINEL_PASSWORD not in stub.name
    assert not aggregator.service_check_names


def _leaky_handler(mode: str) -> Any:
    """A hostile server that echoes the password back in every way it can."""
    echo = SENTINEL_PASSWORD.encode()

    def on_auth(conn: Any, srv: FakeEslServer, line: str) -> bool:
        conn.send(b'Content-Type: command/reply\nReply-Text: -ERR bad ' + echo + b'\n\n' + DISCONNECT)
        return False

    def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
        if mode == 'err_body':
            conn.send(api_response(b'-ERR ' + echo + b'\n'))
        elif mode == 'garbage_body':
            conn.send(api_response(echo + b' not a number\n'))
        elif mode == 'bad_header':
            conn.send(b'Content-Type: api/response\n' + echo + b'\n\n')
        return mode != 'bad_header'

    return make_handler(on_auth=on_auth if mode == 'auth' else None, on_api=on_api if mode != 'auth' else None)


@pytest.mark.parametrize('mode', ['auth', 'err_body', 'garbage_body', 'bad_header'])
def test_password_never_leaks_even_if_the_server_echoes_it(
    mode: str,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Callable[..., None],
    aggregator: AggregatorStub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    server = make_server(_leaky_handler(mode), password='unrelated')
    check = make_check(server)
    dd_run_check(check)
    _assert_no_sentinel(aggregator, caplog)
    assert SENTINEL_PASSWORD not in repr(check)
    assert SENTINEL_PASSWORD not in repr(check._client)


def test_exceptions_do_not_contain_credentials(make_server: MakeServer) -> None:
    def reject(conn: Any, srv: FakeEslServer, line: str) -> bool:
        conn.send(command_reply('-ERR invalid') + DISCONNECT)
        return False

    def junk(conn: Any, srv: FakeEslServer, line: str) -> bool:
        conn.send(b'Content-Type: x\n\n')
        return False

    for handler in (make_handler(on_auth=reject), make_handler(greeting=DISCONNECT), make_handler(on_auth=junk)):
        server = make_server(handler)
        client = EslClient('127.0.0.1', server.port, SENTINEL_PASSWORD, 'u@d', 0.5, 0.3)
        with pytest.raises(esl.EslError) as exc:
            client.connect(time.monotonic() + 2)
            client.api('uptime s', time.monotonic() + 2)
        text = str(exc.value) + repr(exc.value) + ''.join(repr(a) for a in exc.value.args)
        assert SENTINEL_PASSWORD not in text
        assert exc.value.__cause__ is None
        assert exc.value.__context__ is None
        assert SENTINEL_PASSWORD not in repr(client)
        client.close()


def test_check_spawns_no_processes_and_starts_no_threads(
    monkeypatch: pytest.MonkeyPatch,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Callable[..., None],
) -> None:
    import os
    import subprocess

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError('process execution attempted')

    for target, name in [
        (subprocess, 'Popen'),
        (subprocess, 'run'),
        (os, 'system'),
        (os, 'popen'),
        (os, 'execv'),
        (os, 'posix_spawn'),
    ]:
        monkeypatch.setattr(target, name, boom)

    server = make_server()
    check = make_check(server)
    dd_run_check(check)
    before = set(threading.enumerate())  # includes the fake server's own threads
    for _ in range(3):
        dd_run_check(check)
    assert set(threading.enumerate()) == before


def test_source_has_no_process_thread_or_environment_access() -> None:
    package = pathlib.Path(esl.__file__).parent
    for path in (package / 'esl.py', package / 'check.py'):
        text = path.read_text()
        for needle in ('subprocess', 'import os', 'os.environ', 'getenv', 'threading', 'import shlex', 'os.system'):
            assert needle not in text, (path.name, needle)


# ---------------------------------------------------------------------------------------------------------
# Multiple instances and bounded resources
# ---------------------------------------------------------------------------------------------------------


def test_instances_are_independent(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    pw_a, pw_b = 'password-for-A-xyz', 'password-for-B-xyz'
    server_a = make_server(password=pw_a)
    server_b = make_server(password=pw_b)
    server_b.responses['show channels count'] = b'\n8 total.\n'
    check_a = make_check(server_a, password=pw_a, tags=['instance:a'])
    check_b = make_check(server_b, password='wrong-password', tags=['instance:b'])

    dd_run_check(check_a)
    dd_run_check(check_b)
    dd_run_check(check_a)

    tags_a = ['freeswitch_host:127.0.0.1', 'freeswitch_port:{}'.format(server_a.port), 'instance:a']
    tags_b = ['freeswitch_host:127.0.0.1', 'freeswitch_port:{}'.format(server_b.port), 'instance:b']
    aggregator.assert_metric('freeswitch.can_connect', 1, tags=tags_a, count=2)
    aggregator.assert_metric('freeswitch.can_connect', 0, tags=tags_b, count=1)
    aggregator.assert_metric('freeswitch.channels.active', 4, tags=tags_a, count=2)
    aggregator.assert_metric('freeswitch.channels.active', count=0, tags=tags_b)
    aggregator.assert_metric_has_tag('freeswitch.collection.errors', 'error_type:auth', count=1)
    assert server_a.connections == 1
    assert server_b.connections == 1
    assert all(pw_b not in line for line in server_a.lines)
    assert all(pw_a not in line for line in server_b.lines)


def test_repeated_runs_use_bounded_resources(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Callable[..., None], aggregator: AggregatorStub
) -> None:
    server = make_server()
    check = make_check(server)
    dd_run_check(check)
    threads_before = set(threading.enumerate())
    for _ in range(199):
        dd_run_check(check)

    assert server.connections == 1
    assert len(server.api_lines) == 400
    assert set(threading.enumerate()) == threads_before
    assert check._client is not None and check._client._buf == b''
    tag_sets = {tuple(sorted(s.tags)) for name in ALL_METRICS for s in aggregator.metrics(name)}
    assert len(tag_sets) == 1  # no tag growth


def test_closed_client_does_not_leak_sockets(make_server: MakeServer) -> None:
    server = make_server()
    client = EslClient('127.0.0.1', server.port, SENTINEL_PASSWORD, connect_timeout=0.5, read_timeout=0.3)
    for _ in range(20):
        client.connect(time.monotonic() + 2)
        assert client.connected
        client.close()
        assert not client.connected
    assert client._sock is None
