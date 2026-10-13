# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Opt-in runtime tests: a disposable FreeSWITCH, the installed Datadog Agent image and a local sink.

Needs ``FS_RTP_RUNTIME_TESTS=1`` and Docker. Nothing touches the lab; nothing leaves the machine.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Iterator

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'integration' / 'tests' / 'runtime'))

from rtp_agent_lib import AgentRig, series_points, sketch_points  # type: ignore[import-not-found]  # noqa: E402
from testbed import Testbed  # type: ignore[import-not-found]  # noqa: E402


def _docker_ready() -> bool:
    if shutil.which('docker') is None:
        return False
    try:
        return subprocess.run(['docker', 'info'], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get('FS_RTP_RUNTIME_TESTS') == '1' and _docker_ready():
        return
    skip = pytest.mark.skip(reason='needs FS_RTP_RUNTIME_TESTS=1 and Docker (disposable FreeSWITCH and Agent)')
    for item in items:
        if '/runtime/' in str(item.fspath):
            item.add_marker(skip)


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def wait_for(predicate: Callable[[], bool], timeout: float = 30.0, interval: float = 0.25) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


class Env:
    """The disposable stack shared by the runtime tests of one module."""

    def __init__(self, bed: Testbed, rig: AgentRig, statsd_port: int) -> None:
        self.bed, self.rig, self.statsd_port = bed, rig, statsd_port

    def call(
        self, dest: str = '5000', app: str = '&playback(tone_stream://L=400;%(1000,0,440))', variables: str = ''
    ) -> str:
        wait_for(lambda: re.search(r'^Status\s+UP', self.bed.raw('sofia status gateway loopreg'), re.M) is not None, 90)
        uid = str(uuid.uuid4())
        spec = 'origination_uuid={}'.format(uid) + (',' + variables if variables else '')
        reply = self.bed.raw('originate {{{}}}sofia/gateway/loopreg/{} {}'.format(spec, dest, app))
        assert reply.startswith('+OK') or 'USER_BUSY' in reply, reply[:60]
        return uid

    def kill(self, uids: list[str]) -> None:
        for uid in uids:
            self.bed.raw('uuid_kill ' + uid)

    def agent_sketch_count(self, metric: str) -> int:
        return sum(p['count'] for p in sketch_points(self.rig.payloads(), metric) if p['metric'] == metric)

    def agent_gauge_last(self, counter: str) -> float | None:
        points = [
            p
            for p in series_points(self.rig.payloads(), 'freeswitch.rtp.listener.total')
            if 'counter:' + counter in p['tags']
        ]
        return points[-1]['value'] if points else None


@pytest.fixture(scope='module')
def env() -> Iterator[Env]:
    net = 'fs-rtpl-' + uuid.uuid4().hex[:6]
    subprocess.run(['docker', 'network', 'create', net], check=True, capture_output=True)
    rig = None
    try:
        with Testbed(
            network=net,
            max_sessions=3000,
            sessions_per_second=1000,
            rtp_end=30000,
            esl_host_port=free_tcp_port(),
            codecs='PCMU,PCMA,speex@8000h@20i,speex@16000h@20i,L16',
        ) as bed:
            assert wait_for(lambda: bed.raw('show registrations count').strip() == '1 total.', 90)
            port = free_udp_port()
            rig = AgentRig(net, statsd_host_port=port)
            rig.start()
            yield Env(bed, rig, port)
    finally:
        if rig is not None:
            rig.stop()
        subprocess.run(['docker', 'network', 'rm', net], capture_output=True)
