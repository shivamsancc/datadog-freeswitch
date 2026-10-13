# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Research experiment (not a test): a very slow consumer, and a hard kill of FreeSWITCH with calls in flight.

Usage: python -m tests.runtime.rtp_stress_extra <out.json>
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Any

from .rtp_load_experiment import Observer, container_stats, created_total, ready, start_calls
from .testbed import Testbed


def main(out: str) -> None:
    results: dict[str, Any] = {}
    with Testbed(max_sessions=3000, sessions_per_second=1000, rtp_end=30000) as bed:
        ready(bed)
        observer = Observer(bed)
        expected = 0
        t0 = time.time()
        for _ in range(16):  # 16 x 75 calls = 2,400 legs hang up while the consumer reads nothing (about 23 MB)
            started = start_calls(bed, 75, 'sc', app='&park')
            expected += 2 * len(started)
            bed.raw('hupall NORMAL_CLEARING')
        results['slow_consumer'] = {
            'legs_expected': expected,
            'generate_seconds': round(time.time() - t0, 1),
            'container': container_stats(bed),
        }
        got = observer.read(30)
        results['slow_consumer'].update({
            'events_received_after_resume': len(got),
            'unique': len({e.get('Unique-ID') for e in got}),
            'disconnected_by_server': observer.disconnected,
            'megabytes': round(sum(int(e['_bytes']) for e in got) / 1e6, 1),
        })  # fmt: skip
        print(results['slow_consumer'], flush=True)
        observer.close()
        # hard kill with calls in flight
        observer = Observer(bed)
        started = start_calls(bed, 10, 'kill')
        time.sleep(2)
        before = created_total(bed)
        subprocess.run(['docker', 'kill', '--signal', 'KILL', bed.name], capture_output=True)
        time.sleep(1)
        got = observer.read(3)
        results['hard_kill'] = {
            'calls_in_flight': len(started),
            'sessions_created_before': before,
            'events_received': len(got),
            'subscription_disconnected': observer.disconnected,
        }
        print(results['hard_kill'], flush=True)
        observer.close()
        subprocess.run(['docker', 'start', bed.name], capture_output=True)
        bed._wait_ready(60)
        results['after_hard_kill_uptime_restarted'] = bed.raw('uptime s').strip()
    with open(out, 'w') as handle:
        json.dump(results, handle, indent=1)


if __name__ == '__main__':
    main(sys.argv[1])
