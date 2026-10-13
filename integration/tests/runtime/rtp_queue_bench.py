# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Bench (not a test): field filtering and a bounded drop-newest queue with a synthetic event of the observed shape.

The event is SYNTHETIC: 229 header lines, about 11 KB, seven of them the fields the listener keeps. No FreeSWITCH is
needed. Usage: python -m tests.runtime.rtp_queue_bench
"""

from __future__ import annotations

import queue
import threading
import time
import tracemalloc
import urllib.parse

from .rtp_soak_experiment import QUEUE_SIZE, WANTED

EVENTS = 20000


def build_event() -> str:
    values = {
        'Unique-ID': 'u-%d', 'Call-Direction': 'outbound', 'Event-Date-Timestamp': '1791824000000000',
        'variable_rtp_use_codec_name': 'PCMU', 'variable_rtp_audio_in_mos': '4.50',
        'variable_rtp_audio_in_quality_percentage': '100.00', 'variable_rtp_audio_in_packet_count': '500',
    }  # fmt: skip
    lines = ['{}: {}'.format(key, values[key]) for key in WANTED]
    lines += ['variable_field_{:03d}: {}'.format(i, 'x' * 28) for i in range(229 - len(lines))]
    return '\n'.join(lines)


def handle(body: str) -> dict[str, str]:
    kept: dict[str, str] = {}
    for line in body.split('\n'):
        key, sep, value = line.partition(': ')
        if sep and key in WANTED:
            kept[key] = urllib.parse.unquote(value)
    return kept


def main() -> None:
    event = build_event()
    print('fields', event.count('\n') + 1, 'bytes', len(event))
    tracemalloc.start()
    start = time.perf_counter()
    for i in range(EVENTS):
        handle(event.replace('u-%d', 'u-%d' % i, 1))
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(
        'parse: {} events in {:.2f}s ({:.0f}/s), traced peak {:.1f} KiB'.format(
            EVENTS, elapsed, EVENTS / elapsed, peak / 1024
        )
    )

    pending: queue.Queue[dict[str, str]] = queue.Queue(QUEUE_SIZE)
    state = {'consumed': 0, 'stop': 0}

    def consumer() -> None:
        while not state['stop']:
            try:
                pending.get(timeout=0.1)
            except queue.Empty:
                continue
            state['consumed'] += 1
            time.sleep(0.005)

    thread = threading.Thread(target=consumer)
    thread.start()
    dropped = 0
    for _ in range(EVENTS):
        try:
            pending.put_nowait(handle(event))
        except queue.Full:
            dropped += 1
    time.sleep(QUEUE_SIZE * 0.005 + 1)
    state['stop'] = 1
    thread.join()
    print('overload: consumed', state['consumed'], 'dropped', dropped, 'queued', pending.qsize(),
          'conserved', state['consumed'] + dropped + pending.qsize() == EVENTS)  # fmt: skip


if __name__ == '__main__':
    main()
