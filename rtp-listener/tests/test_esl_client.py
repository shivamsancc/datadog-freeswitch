# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import annotations

import socket

import pytest

from fs_rtp_listener.esl import (
    EslAuthError,
    EslClosed,
    EslConnectError,
    EslError,
    EslPermissionError,
    EslProtocolError,
    EventClient,
)

from .conftest import EVENT_USER, SENTINEL, FakeEsl, event_body, frame


def client(server: FakeEsl, **kw: object) -> EventClient:
    args: dict[str, object] = {
        'host': '127.0.0.1',
        'port': server.port,
        'password': SENTINEL,
        'username': EVENT_USER,
        'connect_timeout': 2.0,
        'read_timeout': 0.2,
    }
    args.update(kw)
    return EventClient(**args)  # type: ignore[arg-type]


def test_connect_subscribes_only_and_never_sends_an_api_line(esl: FakeEsl, wait_for: object) -> None:
    c = client(esl)
    c.connect()
    esl.send_event(event_body(uid='a'))
    esl.send_event(event_body(uid='b'))
    bodies = []
    for _ in range(20):
        bodies += list(c.events())
        if len(bodies) >= 2:
            break
    assert len(bodies) == 2 and 'Unique-ID: a' in bodies[0]
    c.close()
    assert len(esl.lines) == 2 and esl.lines[0] == 'userauth'
    assert esl.lines[1].startswith('event plain CHANNEL_HANGUP_COMPLETE')
    assert not any(line.startswith(('api', 'bgapi', 'sendmsg', 'filter')) for line in esl.lines)


def test_wrong_password_is_an_auth_error_without_the_secret(esl: FakeEsl) -> None:
    with pytest.raises(EslAuthError) as error:
        client(esl, password='wrong-' + SENTINEL).connect()
    assert SENTINEL not in str(error.value) and SENTINEL not in repr(client(esl))


@pytest.mark.parametrize(
    'events, api',
    [
        ('all', 'none'),
        ('CHANNEL_HANGUP_COMPLETE', 'all'),
        ('CHANNEL_HANGUP_COMPLETE', 'status'),
        ('CHANNEL_CREATE', 'none'),
    ],
)
def test_broad_accounts_are_refused_in_strict_mode(events: str, api: str) -> None:
    server = FakeEsl(allowed_events=events, allowed_api=api)
    try:
        with pytest.raises(EslPermissionError):
            client(server).connect()
        assert server.lines == ['userauth']  # nothing more was sent: no subscription on a broad account
    finally:
        server.stop()


def test_strict_mode_can_be_relaxed_explicitly() -> None:
    server = FakeEsl(allowed_events='all', allowed_api='all')
    try:
        c = client(server, strict_permissions=False)
        c.connect()
        c.close()
    finally:
        server.stop()


def test_subscription_refused(esl: FakeEsl) -> None:
    esl.refuse_subscription = True
    with pytest.raises(EslPermissionError):
        client(esl).connect()


def test_connection_refused_and_bad_greeting() -> None:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    with pytest.raises(EslConnectError):
        EventClient('127.0.0.1', port, 'x', 'a@b', connect_timeout=0.5).connect()
    server = FakeEsl()
    server.greeting = b'Content-Type: something/else\n\n'
    try:
        with pytest.raises(EslProtocolError):
            client(server).connect()
    finally:
        server.stop()


def test_greeting_that_never_arrives_times_out() -> None:
    server = FakeEsl()
    server.greeting = None
    try:
        with pytest.raises(EslError):
            client(server, connect_timeout=0.4).connect()
    finally:
        server.stop()


def test_disconnect_notice_and_peer_close(esl: FakeEsl) -> None:
    c = client(esl)
    c.connect()
    esl.send_event(event_body())
    assert len(list(c.events())) <= 1
    for conn in list(esl._conns):
        conn.sendall(frame('text/disconnect-notice', b'bye\n'))
    with pytest.raises(EslClosed):
        for _ in range(50):
            list(c.events())
    c.close()


@pytest.mark.parametrize(
    'chunk',
    [
        b'Content-Type: text/event-plain\nContent-Length: abc\n\n',
        b'Content-Type: text/event-plain\nContent-Length: 99999999\n\n',
        b'garbage without separator\n\n',
        b'X' * 20000 + b'\n\n',
    ],
)
def test_malformed_frames_are_protocol_errors(chunk: bytes) -> None:
    server = FakeEsl()
    server.script = [chunk]
    try:
        c = client(server, max_event_bytes=1_000_000)
        c.connect()
        with pytest.raises(EslProtocolError):
            for _ in range(50):
                list(c.events())
        c.close()
    finally:
        server.stop()


def test_unknown_message_types_are_ignored(esl: FakeEsl) -> None:
    esl.script = [
        frame('log/data', b'hello'),
        frame('command/reply', b''),
        frame('text/event-plain', event_body(uid='k')),
    ]
    c = client(esl)
    c.connect()
    got = []
    for _ in range(30):
        got += list(c.events())
        if got:
            break
    assert len(got) == 1 and 'Unique-ID: k' in got[0]
    c.close()
