# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""A receive-only FreeSWITCH Event Socket client for one event type.

It authenticates (``userauth`` or, only if explicitly allowed, ``auth``), subscribes with
``event plain CHANNEL_HANGUP_COMPLETE`` and then only reads. It never sends an ``api`` or ``bgapi`` line.
Every raised error carries a fixed message and a bounded ``reason``; passwords, server text and event content are
never placed in exceptions.
"""

from __future__ import annotations

import socket
import time
from typing import Iterator

EVENT = 'CHANNEL_HANGUP_COMPLETE'
MAX_HEADER_BYTES = 8192
RECV_SIZE = 65536

REASONS = ('connect', 'timeout', 'auth', 'permissions', 'protocol', 'closed')


class EslError(Exception):
    reason = 'protocol'


class EslConnectError(EslError):
    reason = 'connect'


class EslAuthError(EslError):
    reason = 'auth'


class EslPermissionError(EslError):
    reason = 'permissions'


class EslProtocolError(EslError):
    reason = 'protocol'


class EslClosed(EslError):
    reason = 'closed'


class EventClient:
    """One connection. Not thread-safe except for :meth:`close`, which may be called from another thread."""

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        username: str | None,
        strict_permissions: bool = True,
        connect_timeout: float = 5.0,
        read_timeout: float = 1.0,
        max_event_bytes: int = 262144,
    ) -> None:
        self._host, self._port = host, port
        self._password, self._username = password, username
        self._strict = strict_permissions
        self._connect_timeout, self._read_timeout = connect_timeout, read_timeout
        self._max_event = max_event_bytes
        self._sock: socket.socket | None = None
        self._buf = b''
        self.sent_lines: list[str] = []  # command words only, for tests: 'userauth', 'auth', 'event'

    def __repr__(self) -> str:
        return 'EventClient(host={!r}, port={!r})'.format(self._host, self._port)

    # -- lifecycle ----------------------------------------------------------------------------------------------

    def connect(self) -> None:
        self.close()
        try:
            self._sock = socket.create_connection((self._host, self._port), timeout=self._connect_timeout)
            self._sock.settimeout(self._read_timeout)
        except TimeoutError:
            raise EslConnectError('timed out connecting to FreeSWITCH') from None
        except OSError:
            raise EslConnectError('could not connect to FreeSWITCH') from None
        try:
            headers, _ = self._message(deadline=time.monotonic() + self._connect_timeout)
            if headers.get('content-type') != 'auth/request':
                raise EslProtocolError('unexpected greeting')
            if self._username is not None:
                line = 'userauth {}:{}'.format(self._username, self._password)
                self.sent_lines.append('userauth')
            else:
                line = 'auth {}'.format(self._password)
                self.sent_lines.append('auth')
            self._send(line)
            headers, _ = self._message(deadline=time.monotonic() + self._connect_timeout)
            reply = headers.get('reply-text', '')
            if headers.get('content-type') != 'command/reply':
                raise EslProtocolError('unexpected authentication response')
            if not reply.startswith('+OK'):
                raise EslAuthError('authentication was rejected')
            if self._strict:
                self._check_permissions(headers)
            self._send('event plain ' + EVENT)
            self.sent_lines.append('event')
            headers, _ = self._message(deadline=time.monotonic() + self._connect_timeout)
            if not headers.get('reply-text', '').startswith('+OK'):
                raise EslPermissionError('event subscription was refused')
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check_permissions(headers: dict[str, str]) -> None:
        """Fail closed unless the account may only subscribe to the one event and run no API command."""
        if headers.get('allowed-events') != EVENT:
            raise EslPermissionError('account is not limited to the hangup event')
        if headers.get('allowed-api', '').lower() not in ('none', ''):
            raise EslPermissionError('account may run API commands')

    def close(self) -> None:
        sock, self._sock = self._sock, None
        self._buf = b''
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # -- reading ------------------------------------------------------------------------------------------------

    def events(self) -> Iterator[str]:
        """Yield the body of each hangup event. Returns when the read times out with nothing pending (call again).

        Raises :class:`EslClosed` or another :class:`EslError` when the connection ends or misbehaves.
        """
        while True:
            try:
                headers, body = self._message(deadline=None)
            except TimeoutError:
                return
            content_type = headers.get('content-type')
            if content_type == 'text/disconnect-notice':
                raise EslClosed('FreeSWITCH closed the connection')
            if content_type == 'text/event-plain':
                yield body.decode('utf-8', 'replace')
            # anything else (command replies, log lines) is ignored

    def _send(self, line: str) -> None:
        if self._sock is None:
            raise EslClosed('not connected')
        try:
            self._sock.sendall(line.encode() + b'\n\n')
        except OSError:
            raise EslClosed('connection to FreeSWITCH was lost') from None

    def _fill(self) -> None:
        if self._sock is None:
            raise EslClosed('not connected')
        try:
            chunk = self._sock.recv(RECV_SIZE)
        except TimeoutError:
            raise
        except OSError:
            raise EslClosed('connection to FreeSWITCH was lost') from None
        if not chunk:
            raise EslClosed('connection closed by FreeSWITCH')
        self._buf += chunk

    def _message(self, deadline: float | None) -> tuple[dict[str, str], bytes]:
        """One framed message.

        With a deadline, a timeout while waiting is an :class:`EslError`; without one, a timeout with an empty buffer
        propagates as :class:`TimeoutError` (idle connection) while a partial message keeps waiting.
        """
        while b'\n\n' not in self._buf:
            if len(self._buf) > MAX_HEADER_BYTES:
                raise EslProtocolError('header too large')
            try:
                self._fill()
            except TimeoutError:
                if deadline is None and not self._buf:
                    raise
                if deadline is not None and time.monotonic() > deadline:
                    raise EslError('timed out waiting for FreeSWITCH') from None
        raw, self._buf = self._buf.split(b'\n\n', 1)
        if len(raw) > MAX_HEADER_BYTES:
            raise EslProtocolError('header too large')
        headers: dict[str, str] = {}
        for line in raw.decode('utf-8', 'replace').split('\n'):
            name, sep, value = line.partition(':')
            if not sep or not name.strip():
                raise EslProtocolError('malformed header')
            headers[name.strip().lower()] = value.strip()
        length = 0
        if 'content-length' in headers:
            value = headers['content-length']
            if not (value.isascii() and value.isdigit()):
                raise EslProtocolError('invalid content length')
            length = int(value)
            if length > self._max_event:
                raise EslProtocolError('event too large')
        started = time.monotonic()
        while len(self._buf) < length:
            try:
                self._fill()
            except TimeoutError:
                if time.monotonic() - started > max(self._connect_timeout, 5.0):
                    raise EslError('timed out reading an event') from None
        body, self._buf = self._buf[:length], self._buf[length:]
        return headers, body
