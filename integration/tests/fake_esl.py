# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""A deterministic, scriptable fake FreeSWITCH Event Socket server for unit tests."""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable

GREETING = b'Content-Type: auth/request\n\n'
# Body of ``show status`` as captured from the FreeSWITCH 1.10.12 lab (Session C, restricted account, idle, one earlier
# call). It contains no identifiers; only the uptime line varies between captures. Real capture, not synthetic.
STATUS_1_10_12 = (
    b'UP 0 years, 0 days, 7 hours, 49 minutes, 43 seconds, 402 milliseconds, 400 microseconds\n'
    b'FreeSWITCH (Version 1.10.12 -release 64bit) is ready\n'
    b'4 session(s) since startup\n'
    b'0 session(s) - peak 4, last 5min 0 \n'
    b'0 session(s) per Sec out of max 30, peak 4, last 5min 0\n'
    b'200 session(s) max\n'
    b'min idle cpu 0.00/99.43\n'
    b'Current Stack Size/Max 240K/8192K\n'
)

DEFAULT_RESPONSES = {
    'uptime s': b'3401\n',
    'show channels count': b'\n4 total.\n',
    'show status': STATUS_1_10_12,
    'show registrations count': b'\n1 total.\n',
    'show bridged_calls count': b'\n0 total.\n',
}


def frame(content_type: str, body: bytes | None = b'', **headers: str) -> bytes:
    out = 'Content-Type: {}\n'.format(content_type)
    for name, value in headers.items():
        out += '{}: {}\n'.format(name.replace('_', '-'), value)
    if body is not None:
        out += 'Content-Length: {}\n'.format(len(body))
    return out.encode() + b'\n' + (body or b'')


def command_reply(text: str) -> bytes:
    return 'Content-Type: command/reply\nReply-Text: {}\n\n'.format(text).encode()


def api_response(body: bytes) -> bytes:
    return frame('api/response', body)


DISCONNECT = frame('text/disconnect-notice', b'Disconnected, goodbye.\n', Content_Disposition='disconnect')


class Conn:
    """Server side of one accepted connection."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.sock.settimeout(5)
        self._buf = b''

    def read_line(self) -> str | None:
        """Read one ``\\n\\n``-terminated client line, or None on EOF/timeout."""
        while b'\n\n' not in self._buf:
            try:
                chunk = self.sock.recv(4096)
            except OSError:
                return None
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b'\n\n', 1)
        return line.decode('utf-8', 'replace')

    def send(self, data: bytes) -> None:
        try:
            self.sock.sendall(data)
        except OSError:
            pass

    def drip(self, data: bytes, delay: float) -> None:
        for i in range(len(data)):
            self.send(data[i : i + 1])
            time.sleep(delay)

    def wait_closed(self, timeout: float = 3.0) -> None:
        """Stall (send nothing) until the client goes away or the timeout passes."""
        end = time.monotonic() + timeout
        self.sock.settimeout(0.1)
        while time.monotonic() < end:
            try:
                if not self.sock.recv(4096):
                    return
            except TimeoutError:
                continue
            except OSError:
                return

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


Handler = Callable[[Conn, 'FakeEslServer', int], None]


class FakeEslServer:
    def __init__(self, handler: Handler | None = None, password: str = 'secret', users: dict | None = None) -> None:
        self.handler = handler or make_handler()
        self.password = password
        self.users = users or {}
        self.lines: list[str] = []
        self.connections = 0
        self.mode = 'good'
        # What a userauth success advertises. None omits the header; USERAUTH_REPLY overrides the whole reply.
        self.allowed_api: str | None = 'uptime,show'
        self.userauth_reply: bytes | None = None
        self.responses = dict(DEFAULT_RESPONSES)
        self._lock = threading.Lock()
        self._socks: list[socket.socket] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(('127.0.0.1', 0))
        self._listener.listen(16)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                sock, _ = self._listener.accept()
            except OSError:
                return
            with self._lock:
                self.connections += 1
                index = self.connections
                self._socks.append(sock)
            threading.Thread(target=self._run, args=(sock, index), daemon=True).start()

    def _run(self, sock: socket.socket, index: int) -> None:
        conn = Conn(sock)
        try:
            self.handler(conn, self, index)
        finally:
            conn.close()

    def record(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)

    def kill_connections(self) -> None:
        with self._lock:
            socks, self._socks = self._socks, []
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        self.kill_connections()

    @property
    def api_lines(self) -> list[str]:
        return [line for line in self.lines if line.startswith('api ')]


def make_handler(
    greeting: bytes | None = GREETING,
    on_auth: Callable[[Conn, FakeEslServer, str], bool] | None = None,
    on_api: Callable[[Conn, FakeEslServer, str], bool] | None = None,
) -> Handler:
    """Build a connection handler.

    ``on_auth`` / ``on_api`` return True to continue the session and False to end it, and are responsible for
    sending whatever they want to send. With ``greeting=None`` nothing is sent and the server just waits.
    """

    def handler(conn: Conn, server: FakeEslServer, index: int) -> None:
        if greeting is None:
            conn.wait_closed()
            return
        conn.send(greeting)
        authed = False
        while True:
            line = conn.read_line()
            if line is None:
                return
            server.record(line)
            if line == 'exit':
                conn.send(command_reply('+OK bye'))
                return
            if not authed:
                if on_auth is not None:
                    if not on_auth(conn, server, line):
                        return
                    authed = True
                    continue
                if line == 'auth ' + server.password:
                    authed = True
                    conn.send(command_reply('+OK accepted'))
                elif line.startswith('userauth '):
                    user, _, password = line[len('userauth ') :].partition(':')
                    if user in server.users and server.users[user] == password:
                        authed = True
                        if server.userauth_reply is not None:
                            conn.send(server.userauth_reply)
                        else:
                            api = (
                                b''
                                if server.allowed_api is None
                                else b'Allowed-API: ' + server.allowed_api.encode() + b'\n'
                            )
                            conn.send(
                                b'Content-Type: command/reply\nReply-Text: +OK accepted\n'
                                b'Allowed-Events: all\n' + api + b'\n'
                            )
                    else:
                        conn.send(command_reply('-ERR invalid') + DISCONNECT)
                        return
                else:
                    conn.send(command_reply('-ERR invalid') + DISCONNECT)
                    return
                continue
            if line.startswith('api '):
                cmd = line[4:]
                if on_api is not None:
                    if not on_api(conn, server, cmd):
                        return
                    continue
                if cmd in server.responses:
                    conn.send(api_response(server.responses[cmd]))
                else:
                    conn.send(api_response('-ERR {} Command not found!\n'.format(cmd).encode()))
            else:
                conn.send(command_reply('-ERR command not found'))

    return handler
