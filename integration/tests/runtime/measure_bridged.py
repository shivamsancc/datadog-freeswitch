# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Measurement (not a test): response size and duration of the check's queries as bridged calls accumulate.

Disposable FreeSWITCH only. Creates real SIP bridges over the container loopback (each bridge = 4 channels) and, at each
level, times ``show bridged_calls count`` and the complete five-command run with the check's own client.
Usage: python -m tests.runtime.measure_bridged <out.json> [levels, comma separated]
"""

from __future__ import annotations

import json
import re
import statistics
import sys
import time

from datadog_checks.freeswitch import esl

from .testbed import DOMAIN, RESTRICTED_USER, Testbed

SAMPLES = 15


def main(out: str, levels: list[int]) -> None:
    results = []
    with Testbed(max_sessions=2000, sessions_per_second=500, rtp_end=24000) as bed:
        deadline = time.time() + 90
        while time.time() < deadline and bed.raw('show registrations count').strip() != '1 total.':
            time.sleep(1)
        while time.time() < deadline and not re.search(r'^Status\s+UP', bed.raw('sofia status gateway loopreg'), re.M):
            time.sleep(1)
        client = esl.EslClient(
            '127.0.0.1', bed.port, bed.restricted_password, username='{}@{}'.format(RESTRICTED_USER, DOMAIN),
            connect_timeout=3, read_timeout=5,
        )  # fmt: skip
        client.connect(time.monotonic() + 10)
        bridges = 0
        for level in levels:
            while bridges < level:
                reply = bed.raw('originate sofia/gateway/loopreg/5001 &bridge(sofia/gateway/loopreg/5000)')
                assert reply.startswith('+OK'), reply
                bridges += 1
            for _ in range(60):  # the show tables trail call setup
                if bed.raw('show bridged_calls count').strip().endswith('{} total.'.format(level)):
                    break
                time.sleep(0.5)
            sizes, times, runs = [], [], []
            for _ in range(SAMPLES):
                deadline_s = time.monotonic() + 20
                t = time.perf_counter()
                body = client.api(esl.CMD_BRIDGED, deadline_s, esl.MAX_LISTING_BYTES)
                times.append(time.perf_counter() - t)
                sizes.append(len(body))
                t = time.perf_counter()
                for command in (esl.CMD_UPTIME, esl.CMD_CHANNELS, esl.CMD_STATUS, esl.CMD_REGISTRATIONS):
                    client.api(command, deadline_s)
                client.api(esl.CMD_BRIDGED, deadline_s, esl.MAX_LISTING_BYTES)
                runs.append(time.perf_counter() - t)
                time.sleep(0.2)
            status = bed.raw('status')
            results.append(
                {
                    'bridges': level,
                    'channels': int(bed.raw('show channels count').split()[0]),
                    'bridged_response_bytes': sizes[0],
                    'bridged_ms_median': round(statistics.median(times) * 1000, 1),
                    'bridged_ms_max': round(max(times) * 1000, 1),
                    'full_run_ms_median': round(statistics.median(runs) * 1000, 1),
                    'full_run_ms_max': round(max(runs) * 1000, 1),
                    'samples': SAMPLES,
                    'parsed': esl.parse_bridged_calls(body),
                    'idle_cpu_line': (re.findall(r'min idle cpu .*', status) or [''])[0],
                }
            )  # fmt: skip
            print(results[-1], flush=True)
        client.close()
    with open(out, 'w') as handle:
        json.dump(results, handle, indent=1)


if __name__ == '__main__':
    main(sys.argv[1], [int(x) for x in (sys.argv[2] if len(sys.argv) > 2 else '0,1,5,10,25,50,100,150').split(',')])
