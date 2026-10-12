# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Additional protocol, configuration, security and multi-instance scenarios.

These complement ``test_unit.py`` and use only the in-process fake ESL server.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

import pytest

from datadog_checks.base.stubs.aggregator import AggregatorStub
from datadog_checks.freeswitch.esl import ERROR_TYPES

from .conftest import SENTINEL_PASSWORD
from .fake_esl import (
    FakeEslServer,
    api_response,
    command_reply,
    frame,
    make_handler,
)

MakeServer = Callable[..., FakeEslServer]
MakeCheck = Callable[..., Any]
Run = Callable[..., None]

SERVER_TEXT = 'SERVER-CONTROLLED-TEXT-9d1c'
ALL_METRICS = [
    'freeswitch.can_connect',
    'freeswitch.uptime',
    'freeswitch.channels.active',
    'freeswitch.collection.duration',
    'freeswitch.collection.errors',
    'freeswitch.sessions.limit',
    'freeswitch.sessions.created',
    'freeswitch.sessions.active',
    'freeswitch.sessions.peak',
    'freeswitch.sessions.rate.limit',
    'freeswitch.registrations',
    'freeswitch.calls.bridged',
]


def _all_tags(aggregator: AggregatorStub) -> set[str]:
    return {tag for name in ALL_METRICS for stub in aggregator.metrics(name) for tag in stub.tags}


def _error_tags(aggregator: AggregatorStub) -> set[str]:
    return {t for t in _all_tags(aggregator) if t.startswith('error_type:')}


# ---------------------------------------------------------------------------------------------------------
# Configuration validation fails safely, before any network activity, without echoing the password
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    'overrides',
    [
        {'port': 0},
        {'port': 65536},
        {'port': -1},
        {'port': 'eight-zero-two-one'},
        {'connect_timeout': 0},
        {'connect_timeout': -1},
        {'connect_timeout': 30.5},
        {'connect_timeout': 'soon'},
        {'read_timeout': 0.01},
        {'read_timeout': 31},
        {'read_timeout': 'soon'},
        {'password': ''},
        {'host': ''},
        {'host': '   '},
        {'host': '\t\n'},
    ],
    ids=[
        'port=0',
        'port=65536',
        'port=-1',
        'port=text',
        'connect_timeout=0',
        'connect_timeout=-1',
        'connect_timeout=30.5',
        'connect_timeout=text',
        'read_timeout=0.01',
        'read_timeout=31',
        'read_timeout=text',
        'password=empty',
        'host=empty',
        'host=spaces',
        'host=tab-newline',
    ],
)
def test_invalid_configuration_fails_before_connecting(
    overrides: dict, make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run
) -> None:
    server = make_server()
    check = make_check(server, **overrides)
    with pytest.raises(Exception) as exc:
        dd_run_check(check)
    assert SENTINEL_PASSWORD not in str(exc.value)
    assert server.connections == 0
    assert server.lines == []


@pytest.mark.parametrize('host', ['', '   ', '\t\n'], ids=['empty', 'spaces', 'tab-newline'])
def test_blank_host_is_rejected_with_a_host_error(
    host: str, make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run
) -> None:
    server = make_server()
    check = make_check(server, host=host)
    with pytest.raises(Exception) as exc:
        dd_run_check(check)
    assert 'host must not be empty' in str(exc.value)
    assert server.connections == 0


@pytest.mark.parametrize('missing', ['host', 'password'])
def test_missing_required_options_fail_before_connecting(
    missing: str, make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run
) -> None:
    server = make_server()
    check = make_check(server)
    check.instance.pop(missing)
    with pytest.raises(Exception) as exc:
        dd_run_check(check)
    assert SENTINEL_PASSWORD not in str(exc.value)
    assert server.connections == 0


@pytest.mark.parametrize(
    'username',
    ['monitor@example.org\napi status', 'monitor@example.org\r\n\r\napi reloadxml', 'a@b\x00c', 'm@x\tapi status'],
)
def test_username_cannot_inject_commands(
    username: str, make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run
) -> None:
    server = make_server(users={'monitor@example.org': SENTINEL_PASSWORD})
    check = make_check(server, username=username)
    with pytest.raises(Exception):
        dd_run_check(check)
    assert server.connections == 0
    assert server.lines == []


# ---------------------------------------------------------------------------------------------------------
# Framing: legitimate-but-awkward traffic must succeed
# ---------------------------------------------------------------------------------------------------------


def _fragmenting_handler(chunk: int) -> Any:
    """A well-formed server that writes every message in tiny pieces so each frame spans many reads."""

    def send(conn: Any, data: bytes) -> None:
        for i in range(0, len(data), chunk):
            conn.send(data[i : i + chunk])

    def handler(conn: Any, server: FakeEslServer, index: int) -> None:
        send(conn, b'Content-Type: auth/request\n\n')
        while True:
            line = conn.read_line()
            if line is None:
                return
            server.record(line)
            if line.startswith('auth '):
                send(conn, command_reply('+OK accepted'))
            elif line.startswith('api '):
                send(conn, api_response(server.responses[line[4:]]))
            else:
                return

    return handler


@pytest.mark.parametrize('chunk', [1, 2, 7])
def test_frames_split_across_many_reads_are_reassembled(
    chunk: int, make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    server = make_server(_fragmenting_handler(chunk))
    server.responses['show channels count'] = b'\n12 total.\n'
    dd_run_check(make_check(server))
    aggregator.assert_metric('freeswitch.can_connect', 1, count=1)
    aggregator.assert_metric('freeswitch.uptime', 3401, count=1)
    aggregator.assert_metric('freeswitch.channels.active', 12, count=1)
    aggregator.assert_metric('freeswitch.collection.errors', 0, count=1)


def test_body_larger_than_one_receive_buffer_is_read_completely(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    server = make_server()
    server.responses['uptime s'] = b'86400' + b' ' * 20000  # well inside the 64 KiB bound, > 4 KiB receive size
    dd_run_check(make_check(server))
    aggregator.assert_metric('freeswitch.uptime', 86400, count=1)
    aggregator.assert_metric('freeswitch.can_connect', 1, count=1)


def test_header_names_are_case_insensitive(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    def on_auth(conn: Any, srv: FakeEslServer, line: str) -> bool:
        conn.send(b'content-type: command/reply\nreply-text: +OK accepted\n\n')
        return True

    def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
        body = srv.responses[cmd]
        conn.send(b'CONTENT-TYPE: api/response\nCONTENT-LENGTH: ' + str(len(body)).encode() + b'\n\n' + body)
        return True

    server = make_server(make_handler(greeting=b'CONTENT-TYPE: auth/request\n\n', on_auth=on_auth, on_api=on_api))
    dd_run_check(make_check(server))
    aggregator.assert_metric('freeswitch.can_connect', 1, count=1)
    aggregator.assert_metric('freeswitch.channels.active', 4, count=1)


# ---------------------------------------------------------------------------------------------------------
# Framing: hostile traffic must fail closed with a bounded error type
# ---------------------------------------------------------------------------------------------------------


def test_duplicate_content_type_in_greeting_is_a_protocol_error_and_sends_no_credentials(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    server = make_server(make_handler(greeting=b'Content-Type: auth/request\nContent-Type: auth/request\n\n'))
    dd_run_check(make_check(server))
    assert _error_tags(aggregator) == {'error_type:protocol'}
    assert server.lines == []


def test_duplicate_content_type_in_api_response_is_a_protocol_error(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
        conn.send(b'Content-Type: api/response\nContent-Type: api/response\nContent-Length: 5\n\n3401\n')
        return True

    server = make_server(make_handler(on_api=on_api))
    dd_run_check(make_check(server))
    assert _error_tags(aggregator) == {'error_type:protocol'}
    aggregator.assert_metric('freeswitch.uptime', count=0)


def test_data_trailing_the_auth_reply_is_a_protocol_error_and_no_command_is_sent(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    def on_auth(conn: Any, srv: FakeEslServer, line: str) -> bool:
        conn.send(command_reply('+OK accepted') + api_response(b'injected\n'))
        return True

    server = make_server(make_handler(on_auth=on_auth))
    dd_run_check(make_check(server))
    assert _error_tags(aggregator) == {'error_type:protocol'}
    assert server.api_lines == []


def test_unsolicited_event_frame_between_runs_forces_a_clean_reconnect(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    server = make_server()
    check = make_check(server)
    dd_run_check(check)
    assert server.connections == 1
    # Unsolicited bytes arrive on the idle connection (the check never subscribes to events).
    server_conn_socks = list(server._socks)
    for sock in server_conn_socks:
        sock.sendall(frame('text/event-plain', b'Event-Name: HEARTBEAT\n'))
    dd_run_check(check)
    assert server.connections == 2  # the stale stream was discarded, not parsed as a reply
    aggregator.assert_metric('freeswitch.can_connect', 1, count=2)
    aggregator.assert_metric('freeswitch.collection.errors', 0, count=2)


# ---------------------------------------------------------------------------------------------------------
# Server-controlled text never becomes a tag, a log line or an exception message
# ---------------------------------------------------------------------------------------------------------


def _hostile_servers() -> dict[str, Any]:
    def greeting_text() -> Any:
        return make_handler(greeting=frame('{}/x'.format(SERVER_TEXT), b'', X_Note=SERVER_TEXT))

    def auth_rejected() -> Any:
        def on_auth(conn: Any, srv: FakeEslServer, line: str) -> bool:
            conn.send(command_reply('-ERR {}'.format(SERVER_TEXT)))
            return False

        return make_handler(on_auth=on_auth)

    def api_error_body() -> Any:
        def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
            conn.send(api_response('-ERR {} {}\n'.format(SERVER_TEXT, cmd).encode()))
            return True

        return make_handler(on_api=on_api)

    def permission_denied() -> Any:
        def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
            conn.send(command_reply('-ERR permission denied {}'.format(SERVER_TEXT)))
            return True

        return make_handler(on_api=on_api)

    def garbage_numbers() -> Any:
        def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
            conn.send(api_response('{} total.\n'.format(SERVER_TEXT).encode()))
            return True

        return make_handler(on_api=on_api)

    def odd_header_name() -> Any:
        def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
            conn.send(
                'Content-Type: api/response\n{}: {}\nContent-Length: 0\n\n'.format(SERVER_TEXT, SERVER_TEXT).encode()
            )
            return True

        return make_handler(on_api=on_api)

    return {
        'greeting_text': greeting_text,
        'auth_rejected': auth_rejected,
        'api_error_body': api_error_body,
        'permission_denied': permission_denied,
        'garbage_numbers': garbage_numbers,
        'odd_header_name': odd_header_name,
    }


@pytest.mark.parametrize('scenario', sorted(_hostile_servers()))
def test_server_text_is_not_used_in_tags_logs_or_errors(
    scenario: str,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Run,
    aggregator: AggregatorStub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    server = make_server(_hostile_servers()[scenario]())
    dd_run_check(make_check(server))

    assert SERVER_TEXT not in ' '.join(_all_tags(aggregator))
    assert SERVER_TEXT not in caplog.text
    assert SENTINEL_PASSWORD not in caplog.text
    errors = _error_tags(aggregator)
    assert len(errors) == 1
    assert {e.split(':', 1)[1] for e in errors} <= set(ERROR_TYPES)


# ---------------------------------------------------------------------------------------------------------
# Sockets are closed on every failure mode, and the server sees the close
# ---------------------------------------------------------------------------------------------------------


def _closing_observer(payload: bytes, closed: threading.Event) -> Any:
    def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
        conn.send(payload)
        if conn.read_line() is None:  # None means EOF or timeout: the client closed (or went silent)
            closed.set()
        return False

    return make_handler(on_api=on_api)


@pytest.mark.parametrize(
    'payload',
    [
        b'Content-Type: api/response\nContent-Length: abc\n\n',  # protocol
        command_reply('-ERR permission denied'),  # auth
        b'Content-Type: api/response\nContent-Length: 65537\n\n',  # bounded size
    ],
    ids=['bad_length', 'permission_denied', 'oversized'],
)
def test_client_socket_is_closed_after_each_failure(
    payload: bytes, make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    closed = threading.Event()
    server = make_server(_closing_observer(payload, closed))
    check = make_check(server)
    dd_run_check(check)

    assert check._client is not None and not check._client.connected
    assert closed.wait(3), 'the client did not close its socket after the failure'
    aggregator.assert_metric('freeswitch.can_connect', 0, count=1)


def test_parse_failure_keeps_the_synchronized_connection_and_recovers_on_the_next_run(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    # A well-formed reply with unusable content leaves the stream in sync, so the connection is reused.
    server = make_server()
    server.responses['uptime s'] = b'not a number\n'
    check = make_check(server)
    dd_run_check(check)
    assert _error_tags(aggregator) == {'error_type:parse'}

    server.responses['uptime s'] = b'77\n'
    dd_run_check(check)
    assert server.connections == 1
    assert [s.value for s in aggregator.metrics('freeswitch.can_connect')] == [0, 1]
    aggregator.assert_metric('freeswitch.uptime', 77, count=1)


# ---------------------------------------------------------------------------------------------------------
# Several instances: values, tags and failures stay separate
# ---------------------------------------------------------------------------------------------------------


def test_three_instances_with_different_ports_and_outcomes_do_not_interfere(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    idle = make_server()
    idle.responses['show channels count'] = b'\n0 total.\n'
    idle.responses['uptime s'] = b'10\n'
    busy = make_server()
    busy.responses['show channels count'] = b'\n12 total.\n'
    busy.responses['uptime s'] = b'999999\n'
    broken = make_server(make_handler(greeting=None))  # accepts and then says nothing: a timeout

    checks = {
        'idle': (idle, make_check(idle, tags=['pbx:idle'])),
        'busy': (busy, make_check(busy, tags=['pbx:busy'])),
        'broken': (broken, make_check(broken, tags=['pbx:broken'], connect_timeout=0.3, read_timeout=0.2)),
    }
    for _ in range(2):
        for _, check in checks.values():
            dd_run_check(check)

    def tags(name: str) -> list[str]:
        srv = checks[name][0]
        return ['freeswitch_host:127.0.0.1', 'freeswitch_port:{}'.format(srv.port), 'pbx:' + name]

    aggregator.assert_metric('freeswitch.channels.active', 0, tags=tags('idle'), count=2, metric_type=aggregator.GAUGE)
    aggregator.assert_metric('freeswitch.uptime', 10, tags=tags('idle'), count=2)
    aggregator.assert_metric('freeswitch.channels.active', 12, tags=tags('busy'), count=2)
    aggregator.assert_metric('freeswitch.uptime', 999999, tags=tags('busy'), count=2)

    def values(name: str, wanted: list[str]) -> list[float]:
        return [s.value for s in aggregator.metrics(name) if sorted(s.tags) == sorted(wanted)]

    for ok in ('idle', 'busy'):
        aggregator.assert_metric('freeswitch.can_connect', 1, tags=tags(ok), count=2)
        assert values('freeswitch.collection.errors', tags(ok)) == [0, 0]

    aggregator.assert_metric('freeswitch.can_connect', 0, tags=tags('broken'), count=2)
    assert values('freeswitch.collection.errors', tags('broken') + ['error_type:timeout']) == [1, 1]
    assert {s.type for s in aggregator.metrics('freeswitch.collection.errors')} == {aggregator.COUNT}
    aggregator.assert_metric('freeswitch.uptime', count=0, tags=tags('broken'))
    aggregator.assert_metric('freeswitch.channels.active', count=0, tags=tags('broken'))
    assert set(aggregator.metric_names) <= set(ALL_METRICS)
