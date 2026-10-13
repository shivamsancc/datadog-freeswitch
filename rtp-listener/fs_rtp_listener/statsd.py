# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""A minimal DogStatsD UDP client: distribution, gauge and count. UDP gives no acknowledgement.

``send_errors`` counts only what the operating system reports (for example a refused connection after the Agent went
away). A datagram that is accepted by the kernel and then lost is invisible here; see the cumulative totals emitted by
the listener for the mitigation.
"""

from __future__ import annotations

import socket
import threading
from typing import Callable, Sequence

MAX_DATAGRAM = 1400


class StatsdClient:
    def __init__(self, host: str, port: int, socket_factory: Callable[[], socket.socket] | None = None) -> None:
        self._host, self._port = host, port
        self._factory = socket_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self.send_errors = 0
        self.datagrams = 0

    @staticmethod
    def line(name: str, value: float | int, kind: str, tags: Sequence[str] = ()) -> str:
        text = '{}:{}|{}'.format(name, repr(float(value)) if isinstance(value, float) else value, kind)
        if tags:
            text += '|#' + ','.join(tags)
        return text

    def _send(self, payload: str) -> bool:
        """Send one datagram. After an operating-system error the socket is replaced and the send retried once.

        A connected UDP socket reports an earlier ICMP "port unreachable" (the Agent was down) on the next send; without
        the retry that first datagram after the Agent is back would be refused although the Agent is already listening.
        ``send_errors`` counts datagrams that could not be handed to the operating system even after the retry.
        """
        data = payload.encode()
        with self._lock:
            for _ in range(2):
                try:
                    if self._sock is None:
                        self._sock = self._factory()
                        self._sock.connect((self._host, self._port))
                    self._sock.send(data)
                    self.datagrams += 1
                    return True
                except OSError:
                    if self._sock is not None:
                        try:
                            self._sock.close()
                        except OSError:
                            pass
                        self._sock = None  # a new socket is created for the retry and the next datagram
            self.send_errors += 1
            return False

    def send_lines(self, lines: Sequence[str]) -> int:
        """Send lines, packing as many as fit into one datagram. Returns the number of lines accepted by the OS."""
        sent, size = 0, 0
        batch: list[str] = []
        for line in lines:
            if batch and size + len(line) + 1 > MAX_DATAGRAM:
                sent += len(batch) if self._send('\n'.join(batch)) else 0
                batch, size = [], 0
            batch.append(line)
            size += len(line) + 1
        if batch:
            sent += len(batch) if self._send('\n'.join(batch)) else 0
        return sent

    def distribution(self, name: str, value: float, tags: Sequence[str] = ()) -> bool:
        return self._send(self.line(name, value, 'd', tags))

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
