# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import annotations

import socket
import threading
import time
from typing import Callable, Iterator

import pytest

SENTINEL = 'SENTINEL-listener-pw-5c1e-do-not-leak'
GREETING = b'Content-Type: auth/request\n\n'
EVENT_USER = 'esl_ev@test.local'


def frame(content_type: str, body: bytes = b'', **headers: str) -> bytes:
    out = 'Content-Type: {}\n'.format(content_type)
    for name, value in headers.items():
        out += '{}: {}\n'.format(name.replace('_', '-'), value)
    if body or content_type.startswith('text/event'):
        out += 'Content-Length: {}\n'.format(len(body))
    return out.encode() + b'\n' + body


def event_body(
    mos: str = '4.50',
    quality: str = '100.00',
    packets: str = '500',
    direction: str = 'outbound',
    codec: str = 'PCMU',
    uid: str = 'u-1',
) -> bytes:
    """SYNTHETIC event text of the real shape: the seven fields the listener reads plus filler headers."""
    lines = [
        'Event-Name: CHANNEL_HANGUP_COMPLETE',
        'Unique-ID: ' + uid,
        'Call-Direction: ' + direction,
        'Event-Date-Timestamp: 1791824000000000',
        'Caller-Caller-ID-Number: 5551110000',
        'variable_rtp_use_codec_name: ' + codec,
    ]
    if mos is not None:
        lines.append('variable_rtp_audio_in_mos: ' + mos)
    if quality is not None:
        lines.append('variable_rtp_audio_in_quality_percentage: ' + quality)
    if packets is not None:
        lines.append('variable_rtp_audio_in_packet_count: ' + packets)
    lines += ['variable_filler_{:03d}: x'.format(i) for i in range(200)]
    return ('\n'.join(lines) + '\n').encode()


class FakeEsl:
    """A scriptable ESL server for the unit tests (a SIMULATION, not FreeSWITCH)."""

    def __init__(
        self,
        allowed_events: str = 'CHANNEL_HANGUP_COMPLETE',
        allowed_api: str = 'none',
        users: dict[str, str] | None = None,
    ) -> None:
        self.allowed_events, self.allowed_api = allowed_events, allowed_api
        self.users = users or {EVENT_USER: SENTINEL}
        self.lines: list[str] = []
        self.connections = 0
        self.script: list[bytes] = []  # raw frames to send right after subscription, then keep the connection
        self.close_after_script = False
        self.refuse_subscription = False
        self.greeting: bytes | None = GREETING
        self._lock = threading.Lock()
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(('127.0.0.1', 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        self._conns: list[socket.socket] = []
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                sock, _ = self._listener.accept()
            except OSError:
                return
            with self._lock:
                self.connections += 1
                self._conns.append(sock)
            threading.Thread(target=self._run, args=(sock,), daemon=True).start()

    def _read(self, sock: socket.socket, buf: bytes) -> tuple[str | None, bytes]:
        while b'\n\n' not in buf:
            try:
                chunk = sock.recv(4096)
            except OSError:
                return None, buf
            if not chunk:
                return None, buf
            buf += chunk
        line, buf = buf.split(b'\n\n', 1)
        return line.decode(), buf

    def _run(self, sock: socket.socket) -> None:
        sock.settimeout(10)
        buf = b''
        try:
            if self.greeting is None:
                time.sleep(5)
                return
            sock.sendall(self.greeting)
            line, buf = self._read(sock, buf)
            if line is None:
                return
            self.lines.append(line.split(' ')[0])
            ok = False
            if line.startswith('userauth '):
                user, _, password = line[len('userauth ') :].partition(':')
                ok = self.users.get(user) == password
            if not ok:
                sock.sendall(b'Content-Type: command/reply\nReply-Text: -ERR invalid\n\n')
                return
            sock.sendall(
                ('Content-Type: command/reply\nReply-Text: +OK accepted\nAllowed-Events: {}\nAllowed-API: {}\n\n')
                .format(self.allowed_events, self.allowed_api)
                .encode()
            )
            line, buf = self._read(sock, buf)
            if line is None:
                return
            self.lines.append(line.split(' ')[0] + ' ' + ' '.join(line.split(' ')[1:3]))
            if self.refuse_subscription:
                sock.sendall(b'Content-Type: command/reply\nReply-Text: -ERR permission denied\n\n')
                return
            sock.sendall(b'Content-Type: command/reply\nReply-Text: +OK event listener enabled plain\n\n')
            for item in self.script:
                sock.sendall(item)
            if self.close_after_script:
                return
            while True:  # record anything else the client sends, until it goes away
                line, buf = self._read(sock, buf)
                if line is None:
                    return
                self.lines.append(line.split(' ')[0])
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def send_event(self, body: bytes) -> None:
        with self._lock:
            conns = list(self._conns)
        for sock in conns:
            try:
                sock.sendall(frame('text/event-plain', body))
            except OSError:
                pass

    def drop_all(self) -> None:
        with self._lock:
            conns, self._conns = self._conns, []
        for sock in conns:
            try:
                sock.shutdown(socket.SHUT_RDWR)
                sock.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop = True
        try:
            self._listener.close()
        except OSError:
            pass
        self.drop_all()


class UdpSink:
    """A UDP socket that records every datagram line (a SIMULATED Agent for unit tests)."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.lines: list[str] = []
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop:
            try:
                data, _ = self.sock.recvfrom(65536)
            except (TimeoutError, OSError):
                continue
            self.lines.extend(data.decode().split('\n'))

    def named(self, prefix: str) -> list[str]:
        return [line for line in self.lines if line.startswith(prefix)]

    def stop(self) -> None:
        self._stop = True
        self._thread.join(1)
        self.sock.close()


@pytest.fixture
def esl() -> Iterator[FakeEsl]:
    server = FakeEsl()
    yield server
    server.stop()


@pytest.fixture
def sink() -> Iterator[UdpSink]:
    server = UdpSink()
    yield server
    server.stop()


@pytest.fixture
def wait_for() -> Callable[..., bool]:
    def wait(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    return wait
