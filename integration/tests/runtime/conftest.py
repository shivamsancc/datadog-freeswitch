# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import Iterator

import pytest

from .testbed import Testbed


def _docker_ready() -> bool:
    if shutil.which('docker') is None:
        return False
    try:
        return subprocess.run(['docker', 'info'], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get('FS_RUNTIME_TESTS') == '1' and _docker_ready():
        return
    skip = pytest.mark.skip(reason='runtime tests need FS_RUNTIME_TESTS=1 and a working Docker (disposable FreeSWITCH)')
    for item in items:
        if '/runtime/' in str(item.fspath):
            item.add_marker(skip)


def wait_for(predicate, timeout: float = 40.0, interval: float = 0.5) -> bool:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture(scope='module')
def testbed() -> Iterator[Testbed]:
    with Testbed() as bed:
        assert wait_for(lambda: bed.raw('show registrations count').strip() == '1 total.', timeout=90), (
            'test gateway never registered'
        )
        assert wait_for(
            lambda: re.search(r'^Status\s+UP', bed.raw('sofia status gateway loopreg'), re.M) is not None, timeout=60
        ), 'test gateway never came up'
        yield bed
