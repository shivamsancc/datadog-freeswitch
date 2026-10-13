# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Backend follow-up helper: send a KNOWN distribution to a local Agent's DogStatsD, print the expected percentiles.

For the non-production organization test described in the README. The metric name is a throw-away
(``experiment.rtp_backend_check.mos``), never ``freeswitch.rtp.mos``.

  python tools/known_distribution.py --host 127.0.0.1 --port 8125 --seed 1 --count 2000 --tag host_role:a
  python tools/known_distribution.py --expected --seeds 1,2 --count 2000     # percentiles of the union

Values are MOS-like numbers from a fixed pseudo-random generator, so the expected p50/p95/p99 are exact functions of
(seed, count) and can be compared with what the backend reports. No credentials are used or needed here.
"""

from __future__ import annotations

import argparse
import math
import random
import socket

NAME = 'experiment.rtp_backend_check.mos'


def values(seed: int, count: int) -> list[float]:
    rng = random.Random(seed)
    return [round(rng.uniform(1.0, 4.5), 2) for _ in range(count)]


def nearest_rank(data: list[float], q: float) -> float:
    ordered = sorted(data)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8125)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--seeds', default='')
    parser.add_argument('--count', type=int, default=2000)
    parser.add_argument('--tag', default='host_role:a')
    parser.add_argument('--expected', action='store_true')
    args = parser.parse_args()
    if args.expected:
        union: list[float] = []
        for seed in [int(x) for x in args.seeds.split(',') if x]:
            union += values(seed, args.count)
        print('samples', len(union))
        for q in (0.5, 0.95, 0.99):
            print('p{:g} (nearest rank) {}'.format(q * 100, nearest_rank(union, q)))
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for value in values(args.seed, args.count):
        sock.sendto('{}:{}|d|#{}'.format(NAME, value, args.tag).encode(), (args.host, args.port))
    print('sent', args.count, 'samples of', NAME)


if __name__ == '__main__':
    main()
