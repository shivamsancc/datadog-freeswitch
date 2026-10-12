# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""A minimal, read-only FreeSWITCH Event Socket (ESL) inbound client.

Deliberately narrow: it can authenticate and run the fixed commands in ``ALLOWED_COMMANDS``, nothing else.
It never subscribes to events and never exposes generic command execution.

Every raised error carries a fixed message. Passwords, authentication lines, commands and server text are
never placed in exceptions.
"""

from __future__ import annotations

import ipaddress
import re
import select
import socket
import time
from typing import NamedTuple

# The only commands this client will ever send, matched exactly.
CMD_UPTIME = 'uptime s'
CMD_CHANNELS = 'show channels count'
# Session capacity. Tested on FreeSWITCH 1.10.12 only: a restricted account limited to ``uptime,show`` may run it
# (first word ``show``); the separate ``status`` command is not permitted for that account and is not used.
CMD_STATUS = 'show status'
# Count of registered endpoints (``N total.``). Observed on 1.10.12 with real SIP registrations; count-only reply.
CMD_REGISTRATIONS = 'show registrations count'
# Bridged calls. FreeSWITCH ignores ``count`` for this table and returns the full listing followed by ``N total.``,
# so only the trailing line is interpreted (see ``parse_bridged_calls``) and a larger body limit applies.
CMD_BRIDGED = 'show bridged_calls count'
ALLOWED_COMMANDS = frozenset({CMD_UPTIME, CMD_CHANNELS, CMD_STATUS, CMD_REGISTRATIONS, CMD_BRIDGED})

MAX_HEADER_BYTES = 8 * 1024
MAX_BODY_BYTES = 64 * 1024
# The bridged-calls listing grows with the number of calls (about 600 bytes per row).
MAX_LISTING_BYTES = 2 * 1024 * 1024
_RECV_SIZE = 4096
_MAX_ADDRESSES = 4

ERROR_CONNECT = 'connect'
ERROR_TIMEOUT = 'timeout'
ERROR_AUTH = 'auth'
ERROR_PROTOCOL = 'protocol'
ERROR_PARSE = 'parse'
ERROR_TYPES = (ERROR_CONNECT, ERROR_TIMEOUT, ERROR_AUTH, ERROR_PROTOCOL, ERROR_PARSE)

_DIGITS = re.compile(r'[0-9]+')
_CHANNELS = re.compile(r'([0-9]+) total\.')
_TOTAL = re.compile(r'([0-9]+) total\.')
_STATUS_CREATED = re.compile(r'([0-9]+) session\(s\) since startup')
_STATUS_LIMIT = re.compile(r'([0-9]+) session\(s\) max')
_STATUS_CURRENT = re.compile(r'([0-9]+) session\(s\) - peak ([0-9]+), last 5min ([0-9]+)')
_STATUS_RATE = re.compile(r'([0-9]+) session\(s\) per Sec out of max ([0-9]+), peak ([0-9]+), last 5min ([0-9]+)')
_STATUS_MAX_DIGITS = 15
_USERNAME = re.compile(r'[^\s@:]+@[^\s@:]+')

# First words of the two commands above. FreeSWITCH's ``esl-allowed-api`` matches only the first word, so
# this is the narrowest permission that can be expressed; it also admits every other ``show`` subcommand.
REQUIRED_API_PERMISSIONS = frozenset({'uptime', 'show'})


class EslError(Exception):
    """Base class of operational errors. ``error_type`` is one of ``ERROR_TYPES``.

    ``connection_usable`` is True only when the failure was an application-level answer of the server (the
    message was received completely and the stream is in sync), so a later query on the same connection is safe.
    """

    error_type = ERROR_PROTOCOL
    connection_usable = False


class EslConnectError(EslError):
    error_type = ERROR_CONNECT


class EslTimeoutError(EslError):
    error_type = ERROR_TIMEOUT


class EslAuthError(EslError):
    error_type = ERROR_AUTH


class EslProtocolError(EslError):
    error_type = ERROR_PROTOCOL


class EslParseError(EslError):
    error_type = ERROR_PARSE
    # Raised after a complete, in-sync response was received.
    connection_usable = True


class EslCommandRefused(EslAuthError):
    """The server refused one command (permission denied) after a complete, in-sync reply."""

    connection_usable = True


class EslCommandFailed(EslProtocolError):
    """The server answered one command with ``-ERR`` after a complete, in-sync reply."""

    connection_usable = True


class EslCommandNotAllowed(ValueError):
    """Raised before anything is sent. This is a programming error, not an operational one."""


def _has_control_chars(value: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)


def validate_credentials(username: str | None, password: str) -> None:
    """Raise ValueError (fixed message) when the credentials cannot be sent safely on one protocol line."""
    if not password or _has_control_chars(password):
        raise ValueError('password must be non-empty and must not contain control characters')
    if username is not None:
        if not _USERNAME.fullmatch(username) or _has_control_chars(username):
            raise ValueError('username must have the form user@domain')


def parse_uptime(body: bytes) -> int:
    try:
        text = body.decode('utf-8').strip()
    except UnicodeDecodeError:
        raise EslParseError('uptime response is not valid UTF-8') from None
    if not _DIGITS.fullmatch(text) or not text.isascii():
        raise EslParseError('uptime response has an unexpected format')
    return int(text)


def _decode_text(body: bytes, what: str) -> str:
    try:
        text = body.decode('utf-8')
    except UnicodeDecodeError:
        raise EslParseError('{} response is not valid UTF-8'.format(what)) from None
    if not text.isascii():
        raise EslParseError('{} response has an unexpected format'.format(what))
    return text


def parse_channels(body: bytes) -> int:
    match = _CHANNELS.fullmatch(_decode_text(body, 'channel count').strip())
    if not match:
        raise EslParseError('channel count response has an unexpected format')
    return int(match.group(1))


def parse_registrations(body: bytes) -> int:
    """The number from ``show registrations count`` (``N total.``). No other content is accepted."""
    match = _TOTAL.fullmatch(_decode_text(body, 'registration count').strip())
    if not match or len(match.group(1)) > _STATUS_MAX_DIGITS:
        raise EslParseError('registration count response has an unexpected format')
    return int(match.group(1))


def parse_bridged_calls(body: bytes) -> int:
    """The number of bridged calls from ``show bridged_calls count``.

    FreeSWITCH answers with the full listing (a CSV header and one row per bridged call) followed by ``N total.``
    (``0 total.`` alone when there are none). Only the final line is read as the value; a non-empty listing must
    begin with the expected CSV header. Rows are never interpreted, stored or logged.
    """
    text = _decode_text(body, 'bridged call')
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    if not lines:
        raise EslParseError('bridged call response has an unexpected format')
    match = _TOTAL.fullmatch(lines[-1])
    if not match or len(match.group(1)) > _STATUS_MAX_DIGITS:
        raise EslParseError('bridged call response has an unexpected format')
    value = int(match.group(1))
    if value > 0 and not lines[0].startswith('uuid,'):
        raise EslParseError('bridged call response has an unexpected format')
    return value


def _status_field(
    lines: list[str], marker: str, pattern: re.Pattern[str], required: bool = True
) -> tuple[int, ...] | None:
    """The groups of the one line containing ``marker``. At most one candidate may exist and it must be valid."""
    candidates = [line for line in lines if marker in line]
    if not candidates and not required:
        return None
    if len(candidates) != 1:
        raise EslParseError('status response has a missing or repeated session field')
    match = pattern.fullmatch(candidates[0])
    if match is None or any(len(g) > _STATUS_MAX_DIGITS for g in match.groups()) or not candidates[0].isascii():
        raise EslParseError('status response has an unexpected session field format')
    return tuple(int(g) for g in match.groups())


class StatusFields(NamedTuple):
    """Values of ``show status``. The ``None`` fields are absent on FreeSWITCH versions that do not print the line."""

    created: int
    limit: int
    active: int | None
    peak: int | None
    peak_5min: int | None
    rate_limit: int | None
    rate_peak: int | None


def parse_status(body: bytes) -> StatusFields:
    """Parse the session lines of the body of ``show status``.

    ``<N> session(s) since startup`` and ``<N> session(s) max`` must each occur exactly once. The current/peak line
    (``<N> session(s) - peak P, last 5min Q``) and the rate line (``<N> session(s) per Sec out of max M, peak P,
    last 5min Q``) may be absent, but if present they must be valid. No other field is interpreted.
    """
    text = _decode_text(body, 'status')
    lines = [line.strip() for line in text.split('\n')]
    created = _status_field(lines, ' session(s) since startup', _STATUS_CREATED)
    limit = _status_field(lines, ' session(s) max', _STATUS_LIMIT)
    current = _status_field(lines, ' session(s) - peak ', _STATUS_CURRENT, required=False)
    rate = _status_field(lines, ' session(s) per Sec out of max ', _STATUS_RATE, required=False)
    assert created is not None and limit is not None
    return StatusFields(
        created=created[0],
        limit=limit[0],
        active=current[0] if current else None,
        peak=current[1] if current else None,
        peak_5min=current[2] if current else None,
        rate_limit=rate[1] if rate else None,
        rate_peak=rate[2] if rate else None,
    )


class EslClient:
    """One ESL connection. Not thread-safe; any error leaves the connection closed."""

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        username: str | None = None,
        connect_timeout: float = 3.0,
        read_timeout: float = 3.0,
    ) -> None:
        validate_credentials(username, password)
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._sock: socket.socket | None = None
        self._buf = b''
        self._raw_headers: dict[str, str] = {}
        self._duplicate_headers: set[str] = set()

    def __repr__(self) -> str:
        return 'EslClient(host={!r}, port={!r}, connected={})'.format(self._host, self._port, self.connected)

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def is_stale(self) -> bool:
        """True if the peer closed the connection or sent unsolicited data since the last exchange."""
        if self._sock is None:
            return True
        try:
            readable, _, _ = select.select([self._sock], [], [], 0)
        except (OSError, ValueError):
            return True
        return bool(readable)

    def close(self) -> None:
        sock, self._sock = self._sock, None
        self._buf = b''
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # -- connection -------------------------------------------------------------------------------------------

    def connect(self, deadline: float) -> None:
        """Connect and authenticate. ``deadline`` is a ``time.monotonic()`` value. Closes on any failure."""
        self.close()
        try:
            self._connect_and_auth(deadline)
        except BaseException:
            self.close()
            raise

    def _connect_and_auth(self, deadline: float) -> None:
        self._sock = self._open_socket(deadline)

        headers, _ = self._read_message(deadline)
        if headers.get('content-type') != 'auth/request':
            raise EslProtocolError('unexpected greeting from server')

        if self._username is not None:
            line = 'userauth {}:{}'.format(self._username, self._password)
        else:
            line = 'auth {}'.format(self._password)
        self._send(line, deadline)

        headers, _ = self._read_message(deadline)
        if headers.get('content-type') != 'command/reply':
            raise EslProtocolError('unexpected authentication response type')
        reply = headers.get('reply-text', '')
        if reply.startswith('+OK'):
            self._ensure_drained()
            if self._username is not None:
                self._require_restricted_account(headers)
            return
        if reply.startswith('-ERR'):
            raise EslAuthError('authentication was rejected')
        raise EslProtocolError('unexpected authentication reply')

    def _require_restricted_account(self, headers: dict[str, str]) -> None:
        """Fail closed unless the ``userauth`` account is advertised as limited to ``uptime`` and ``show``.

        FreeSWITCH 1.10.12 answers a successful ``userauth`` with ``Allowed-API: <esl-allowed-api>`` (the value
        used to build the permission list), or ``Allowed-API: all`` when the account has no restriction. The value
        is only trusted when it is exactly the two expected first words, written exactly as the server formats it
        (one space after the colon, no padding, no duplicates), because the server truncates the advertised line
        to 512 bytes and padding could hide further entries.
        """
        value = headers.get('allowed-api')
        raw = self._raw_headers.get('allowed-api')
        if value is None or not value or raw != ' ' + value or 'allowed-api' in self._duplicate_headers:
            raise EslAuthError('account is not restricted to the API commands this check uses')
        # The server splits on a space if the value contains one, otherwise on a comma, and matches entries exactly.
        entries = value.split(' ' if ' ' in value else ',')
        if '' in entries or set(entries) != REQUIRED_API_PERMISSIONS:
            raise EslAuthError('account is not restricted to the API commands this check uses')

    def _open_socket(self, deadline: float) -> socket.socket:
        """Open the TCP connection within ``connect_timeout`` and the run deadline.

        An IP literal is connected to directly, so no name resolution can block. A host name goes through the
        operating system resolver, which the standard library cannot interrupt (no threads or signals are used
        here by design); the deadline is re-checked after resolution and no connection is attempted late.
        """
        try:
            literal = ipaddress.ip_address(self._host.strip('[]'))
        except ValueError:
            literal = None

        if literal is not None:
            family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
            targets: list[tuple[int, tuple]] = [(family, (str(literal), self._port))]
        else:
            if deadline - time.monotonic() <= 0:
                raise EslTimeoutError('deadline exceeded before connecting')
            try:
                infos = socket.getaddrinfo(self._host, self._port, type=socket.SOCK_STREAM)
            except OSError:
                raise EslConnectError('could not resolve the FreeSWITCH host') from None
            targets = [(info[0], info[4]) for info in infos[:_MAX_ADDRESSES]]

        last: EslError = EslConnectError('could not connect to FreeSWITCH')
        for target_family, address in targets:
            timeout = min(self._connect_timeout, deadline - time.monotonic())
            if timeout <= 0:
                raise EslTimeoutError('deadline exceeded before connecting')
            sock = socket.socket(target_family, socket.SOCK_STREAM)
            try:
                sock.settimeout(timeout)
                sock.connect(address)
                return sock
            except TimeoutError:
                sock.close()
                last = EslTimeoutError('timed out connecting to FreeSWITCH')
            except OSError:
                sock.close()
                last = EslConnectError('could not connect to FreeSWITCH')
        raise last

    # -- commands ---------------------------------------------------------------------------------------------

    def api(self, command: str, deadline: float, max_body: int = MAX_BODY_BYTES) -> bytes:
        """Run one allowlisted API command and return the raw response body.

        Closes the connection on any failure, except when the server answered completely and in sync with a
        refusal (``permission denied``) or a command error (``-ERR``): those leave the connection usable.
        """
        if command not in ALLOWED_COMMANDS or '\r' in command or '\n' in command:
            raise EslCommandNotAllowed('command is not allowed')
        if self._sock is None:
            raise EslConnectError('not connected')
        try:
            self._send('api ' + command, deadline)
            headers, body = self._read_message(deadline, max_body)
            content_type = headers.get('content-type')
            if content_type == 'command/reply':
                if headers.get('reply-text', '').startswith('-ERR permission denied'):
                    self._ensure_drained()
                    raise EslCommandRefused('command was refused by the server')
                raise EslProtocolError('unexpected command reply')
            if content_type != 'api/response':
                raise EslProtocolError('unexpected response type')
            if 'content-length' not in headers:
                raise EslProtocolError('response has no content length')
            self._ensure_drained()
            if body.startswith(b'-ERR'):
                raise EslCommandFailed('server reported a command error')
            return body
        except EslError as e:
            if not e.connection_usable:
                self.close()
            raise
        except BaseException:
            self.close()
            raise

    # -- framing ----------------------------------------------------------------------------------------------

    def _send(self, line: str, deadline: float) -> None:
        sock = self._sock
        assert sock is not None
        timeout = min(self._read_timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise EslTimeoutError('deadline exceeded while sending')
        try:
            sock.settimeout(timeout)
            sock.sendall(line.encode('utf-8') + b'\n\n')
        except TimeoutError:
            raise EslTimeoutError('timed out sending to FreeSWITCH') from None
        except OSError:
            raise EslConnectError('connection to FreeSWITCH was lost') from None

    def _fill(self, message_deadline: float) -> None:
        sock = self._sock
        assert sock is not None
        timeout = message_deadline - time.monotonic()
        if timeout <= 0:
            raise EslTimeoutError('timed out waiting for FreeSWITCH')
        try:
            sock.settimeout(timeout)
            chunk = sock.recv(_RECV_SIZE)
        except TimeoutError:
            raise EslTimeoutError('timed out waiting for FreeSWITCH') from None
        except OSError:
            raise EslConnectError('connection to FreeSWITCH was lost') from None
        if not chunk:
            raise EslConnectError('connection closed by FreeSWITCH')
        self._buf += chunk

    def _read_message(self, run_deadline: float, max_body: int = MAX_BODY_BYTES) -> tuple[dict[str, str], bytes]:
        """Read exactly one ESL message. The whole message must arrive within one read timeout."""
        message_deadline = min(time.monotonic() + self._read_timeout, run_deadline)

        while True:
            end = self._buf.find(b'\n\n')
            if end != -1:
                break
            if len(self._buf) > MAX_HEADER_BYTES:
                raise EslProtocolError('response header is too large')
            self._fill(message_deadline)
        if end > MAX_HEADER_BYTES:
            raise EslProtocolError('response header is too large')

        raw, rest = self._buf[:end], self._buf[end + 2 :]
        self._buf = rest
        headers = self._parse_headers(raw)

        if headers.get('content-type') == 'text/disconnect-notice':
            raise EslConnectError('FreeSWITCH closed the connection')

        length = 0
        if 'content-length' in headers:
            value = headers['content-length']
            if not (value.isascii() and value.isdigit()):
                raise EslProtocolError('invalid content length')
            length = int(value)
            if length > max_body:
                raise EslProtocolError('response body is too large')
        while len(self._buf) < length:
            self._fill(message_deadline)
        body, self._buf = self._buf[:length], self._buf[length:]
        return headers, body

    def _ensure_drained(self) -> None:
        # One request is outstanding at a time, so extra bytes mean the stream is no longer in sync.
        if self._buf:
            raise EslProtocolError('unexpected data after response')

    def _parse_headers(self, raw: bytes) -> dict[str, str]:
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise EslProtocolError('response header is not valid UTF-8') from None
        headers: dict[str, str] = {}
        self._raw_headers = {}
        self._duplicate_headers = set()
        for line in text.split('\n'):
            name, sep, value = line.partition(':')
            name = name.strip().lower()
            if not sep or not name:
                raise EslProtocolError('malformed response header')
            if name in headers:
                if name in ('content-type', 'content-length'):
                    raise EslProtocolError('duplicate response header')
                self._duplicate_headers.add(name)
            headers[name] = value.strip()
            self._raw_headers[name] = value
        return headers
