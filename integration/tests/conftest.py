# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import annotations

from typing import Any, Callable, Iterator

import pytest

from datadog_checks.base.types import InstanceType
from datadog_checks.freeswitch import FreeswitchCheck

from .fake_esl import FakeEslServer, Handler

# A distinctive value that must never show up in logs, exceptions, tags or object representations.
SENTINEL_PASSWORD = 'SENTINEL-pw-7f3a9c1e-do-not-leak'
DEFAULT_TIMEOUTS = {'connect_timeout': 3.0, 'read_timeout': 3.0}
# For tests whose subject is a timeout: short, so that the stall they provoke ends quickly.
FAST_TIMEOUTS = {'connect_timeout': 0.5, 'read_timeout': 0.3}


@pytest.fixture
def instance() -> InstanceType:
    return {'host': 'localhost', 'password': 'not-a-real-password'}


@pytest.fixture
def make_server() -> Iterator[Callable[..., FakeEslServer]]:
    servers: list[FakeEslServer] = []

    def factory(handler: Handler | None = None, **kwargs: Any) -> FakeEslServer:
        kwargs.setdefault('password', SENTINEL_PASSWORD)
        server = FakeEslServer(handler, **kwargs)
        servers.append(server)
        return server

    yield factory
    for server in servers:
        server.stop()


@pytest.fixture
def make_check() -> Iterator[Callable[..., FreeswitchCheck]]:
    checks: list[FreeswitchCheck] = []

    def factory(server: FakeEslServer, **overrides: Any) -> FreeswitchCheck:
        inst: dict[str, Any] = {
            'host': '127.0.0.1',
            'port': server.port,
            'password': SENTINEL_PASSWORD,
            # Generous on purpose: a test that does not exercise a timeout must not depend on how quickly the
            # fake server's thread is scheduled. Tests that expect a timeout pass FAST_TIMEOUTS.
            **DEFAULT_TIMEOUTS,
            'tags': ['tier:test'],
        }
        inst.update(overrides)
        check = FreeswitchCheck('freeswitch', {}, [inst])
        checks.append(check)
        return check

    yield factory
    for check in checks:
        client = getattr(check, '_client', None)
        if client is not None:
            client.close()
