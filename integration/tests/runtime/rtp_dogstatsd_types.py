# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Experiment (not a test, not a listener): how a real Agent treats per-call RTP values sent over DogStatsD.

Values are real MOS and quality figures read from FreeSWITCH hangup events: a fresh clean run on the disposable
instance plus the sanitized events of an earlier run with controlled packet loss, for a spread of values. They are
sent under the name prefix ``experiment.rtp`` as gauge, histogram and distribution to a local Agent whose intake is a
local sink. Nothing is registered and nothing leaves the machine.

Usage: python -m tests.runtime.rtp_dogstatsd_types <out.json>   (from the integration directory; needs Docker)
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .rtp_agent_lib import AgentRig, series_points, sketch_points
from .rtp_load_experiment import TONE, Observer, ready, start_calls
from .testbed import Testbed

PREFIX = 'experiment.rtp'


def netem(bed: Testbed, rule: str | None) -> None:
    prefix = ['docker', 'exec', '-u', '0', bed.name]
    subprocess.run([*prefix, 'tc', 'qdisc', 'del', 'dev', 'lo', 'root'], capture_output=True)
    if rule:
        subprocess.run(
            [*prefix, 'tc', 'qdisc', 'add', 'dev', 'lo', 'root', 'netem', *rule.split()],
            check=True,
            capture_output=True,
        )


def real_samples(bed: Testbed) -> list[dict]:
    """Real hangup-event values: a fresh set of clean calls plus the sanitized events of the earlier impairment run.

    ``tc netem`` could not be installed in this session (the package mirror answered 403), so the impaired values come
    from ``docs/evidence/data/rtp_hangup_events.json``, captured from the same FreeSWITCH build with real packet loss.
    """
    samples = []
    observer = Observer(bed)
    start_calls(bed, 6, 'ds', app=TONE)
    time.sleep(8)
    bed.raw('hupall NORMAL_CLEARING')
    for event in observer.read(5):
        try:
            mos = float(event.get('variable_rtp_audio_in_mos', 'nan'))
            quality = float(event.get('variable_rtp_audio_in_quality_percentage', 'nan'))
        except ValueError:
            continue
        if math.isfinite(mos) and math.isfinite(quality):
            samples.append({'mos': mos, 'quality': quality, 'direction': event.get('Call-Direction'),
                            'packets': int(event.get('variable_rtp_audio_in_packet_count', 0))})  # fmt: skip
    observer.close()
    recorded = json.loads(
        (Path(__file__).resolve().parents[3] / 'docs/evidence/data/rtp_hangup_events.json').read_text()
    )
    for name, rows in recorded.items():
        if not (name.startswith('loss_') or name.startswith('delay_')) or not isinstance(rows, list):
            continue
        for row in rows:
            try:
                mos, quality = float(row['in_mos']), float(row['in_quality_percentage'])
            except (KeyError, ValueError):
                continue
            if math.isfinite(mos) and math.isfinite(quality):
                samples.append({'mos': mos, 'quality': quality, 'direction': row.get('Call-Direction'),
                                'packets': int(row.get('in_packet_count', 0))})  # fmt: skip
    return samples


def send(sock, line: str) -> None:  # type: ignore[no-untyped-def]
    sock.send(line.encode())


def main(out: str) -> None:
    net = 'fs-exp-' + uuid.uuid4().hex[:6]
    subprocess.run(['docker', 'network', 'create', net], check=True, capture_output=True)
    results: dict[str, object] = {}
    rig = None
    try:
        with Testbed(network=net) as bed:
            ready(bed)
            samples = real_samples(bed)
            results['real_samples'] = {'count': len(samples), 'mos_min': min(s['mos'] for s in samples),
                                       'mos_max': max(s['mos'] for s in samples),
                                       'mos_sum': round(sum(s['mos'] for s in samples), 2)}  # fmt: skip
            print(results['real_samples'], flush=True)
            rig = AgentRig(net)
            rig.start()
            sock = rig.sender()
            counters0 = rig.dogstatsd_counters()
            # A: one burst, one flush interval
            t0 = int(time.time())
            for s in samples:
                tags = '#direction:{}'.format(s['direction'])
                for kind, name in (('g', 'mos_gauge'), ('h', 'mos_histogram'), ('d', 'mos_distribution')):
                    send(sock, '{}.{}:{}|{}|{}'.format(PREFIX, name, s['mos'], kind, tags))
            results['A_burst'] = {'sent_samples_per_type': len(samples), 'sent_at': t0}
            # C: flush boundary, two bursts 12 s apart
            time.sleep(12)
            t1 = int(time.time())
            for _ in range(5):
                send(sock, '{}.boundary_distribution:3.0|d|#direction:outbound'.format(PREFIX))
                send(sock, '{}.boundary_gauge:3.0|g|#direction:outbound'.format(PREFIX))
            # D: tag combinations (bounded)
            for direction in ('inbound', 'outbound'):
                for codec in ('pcmu', 'pcma', 'speex'):
                    send(sock, '{}.tagged_distribution:4.0|d|#direction:{},codec:{}'.format(PREFIX, direction, codec))
            # B: invalid input
            before = rig.dogstatsd_counters()
            invalid = ['nan', 'inf', '-inf', 'abc', '', '4.5.5', '1e999', '-1']
            for value in invalid:
                send(sock, '{}.invalid_distribution:{}|d|#direction:outbound'.format(PREFIX, value))
            send(sock, '{}.invalid_type:4.5|x'.format(PREFIX))
            send(sock, '{}.no_type:4.5'.format(PREFIX))
            send(sock, 'x' * 10 + ':' + '1|d|#' + ','.join('t{}:v'.format(i) for i in range(10)))
            time.sleep(3)
            after = rig.dogstatsd_counters()
            results['B_invalid'] = {'sent_invalid_lines': len(invalid) + 3, 'counters_before': before,
                                    'counters_after': after}  # fmt: skip
            time.sleep(45)  # at least two flush intervals
            payloads = rig.payloads()
            series = series_points(payloads, PREFIX)
            sketches = sketch_points(payloads, PREFIX)
            results['series_points'] = series
            results['sketch_points'] = sketches
            results['counters_start_end'] = {'start': counters0, 'end': rig.dogstatsd_counters()}
            results['t0_t1'] = [t0, t1]
            results['payload_paths'] = sorted({p.split('?')[0] for p, _ in payloads})
    finally:
        if rig is not None:
            rig.stop()
        subprocess.run(['docker', 'network', 'rm', net], capture_output=True)
    Path(out).write_text(json.dumps(results, indent=1, default=str))


if __name__ == '__main__':
    main(sys.argv[1])
