# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Soak experiment (not a test, not the production listener): hangup-event collection into a real Agent over time.

A temporary harness: a disposable FreeSWITCH, a call generator with known ground truth, a minimal listener that keeps
only a few numeric fields of each event (a bounded queue, a drop-newest policy, no raw event retained or logged) and a
real Agent with a local sink. The Agent also runs this repository's polling check against the same FreeSWITCH.

Usage: python -m tests.runtime.rtp_soak_experiment <out.json> [duration_seconds]
"""

from __future__ import annotations

import heapq
import json
import math
import queue
import random
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from .rtp_agent_lib import AgentRig, series_points, sketch_points
from .rtp_load_experiment import ready
from .testbed import EVENT_USER, Testbed

PREFIX = 'experiment.rtp'
MIN_PACKETS = 50
QUEUE_SIZE = 2000
CODECS = {'PCMU': 'pcmu', 'PCMA': 'pcma', 'SPEEX': 'speex', 'L16': 'l16'}
DIRECTIONS = {'inbound', 'outbound'}
# Fields the listener may read; every other header is discarded as soon as the event is split.
WANTED = (
    'Unique-ID', 'Call-Direction', 'Event-Date-Timestamp', 'variable_rtp_use_codec_name',
    'variable_rtp_audio_in_mos', 'variable_rtp_audio_in_quality_percentage', 'variable_rtp_audio_in_packet_count',
)  # fmt: skip


class Listener:
    """Reader thread -> bounded queue -> emitter thread. All counters are plain integers."""

    def __init__(self, bed: Testbed, rig: AgentRig) -> None:
        self.bed, self.rig = bed, rig
        self.queue: queue.Queue[dict] = queue.Queue(QUEUE_SIZE)
        self.lock = threading.Lock()
        names = (
            'events dropped_queue_full parse_errors duplicates reconnects connect_failures auth_failures '
            'outcome_valid outcome_no_media outcome_short outcome_missing outcome_invalid '
            'submitted_mos submitted_quality submitted_counts send_errors'
        )
        self.c = dict.fromkeys(names.split(), 0)
        self.connected = False
        self.stop_flag = threading.Event()
        self.hold_until = 0.0  # planned disconnect: do not (re)connect before this time
        self.drop_connection = threading.Event()
        self.gaps: list[dict] = []
        self.latency_ms: list[float] = []
        self.max_queue = 0
        self.seen: dict[str, float] = {}
        self.sock: socket.socket | None = None
        self.out = rig.sender()
        self.threads = [
            threading.Thread(target=self._reader, daemon=True),
            threading.Thread(target=self._emitter, daemon=True),
        ]

    def start(self) -> None:
        for thread in self.threads:
            thread.start()

    def stop(self) -> None:
        self.stop_flag.set()
        self.drop_connection.set()

    # -- reader ---------------------------------------------------------------------------------------------

    def _connect(self) -> socket.socket | None:
        try:
            sock = socket.create_connection(('127.0.0.1', self.bed.port), timeout=5)
            sock.settimeout(1)
            _, _, buf = _read(sock, b'')  # the auth/request greeting
            sock.sendall('userauth {}@test.local:{}\n\n'.format(EVENT_USER, self.bed.restricted_password).encode())
            headers, _, buf = _read(sock, buf)
            if not headers.get('Reply-Text', '').startswith('+OK'):
                self.c['auth_failures'] += 1
                sock.close()
                return None
            sock.sendall(b'event plain CHANNEL_HANGUP_COMPLETE\n\n')
            _read(sock, buf)
            self._buf = buf
            return sock
        except (OSError, ValueError):
            self.c['connect_failures'] += 1
            return None

    def _reader(self) -> None:
        backoff = 1.0
        gap_start: float | None = time.time()
        while not self.stop_flag.is_set():
            if time.time() < self.hold_until:
                time.sleep(0.2)
                continue
            self._buf = b''
            sock = self._connect()
            if sock is None:
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
                continue
            backoff = 1.0
            with self.lock:
                self.connected = True
                if gap_start is not None and self.c['reconnects'] >= 0:
                    self.gaps.append({'start': gap_start, 'end': time.time()})
                    gap_start = None
                self.c['reconnects'] += 1
            self.sock = sock
            buf = self._buf
            try:
                while not self.stop_flag.is_set() and not self.drop_connection.is_set():
                    try:
                        headers, body, buf = _read(sock, buf)
                    except TimeoutError:
                        continue
                    if headers.get('Content-Type') == 'text/disconnect-notice':
                        break
                    if headers.get('Content-Type') == 'text/event-plain':
                        self._handle(body)
            except (OSError, ConnectionError, ValueError):
                pass
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
                self.connected = False
                self.drop_connection.clear()
                gap_start = time.time()

    def _handle(self, body: str) -> None:
        wanted: dict[str, str] = {}
        for line in body.split('\n'):
            key, sep, value = line.partition(': ')
            if sep and key in WANTED:
                wanted[key] = urllib.parse.unquote(value)
        wanted['_recv'] = repr(time.time())
        with self.lock:
            self.c['events'] += 1
            uid = wanted.get('Unique-ID', '')
            if uid in self.seen:
                self.c['duplicates'] += 1
                return
            self.seen[uid] = time.time()
            if len(self.seen) > 5000:  # bounded dedupe memory
                for key in sorted(self.seen, key=self.seen.get)[:1000]:  # type: ignore[arg-type]
                    del self.seen[key]
        try:
            self.queue.put_nowait(wanted)
        except queue.Full:
            with self.lock:
                self.c['dropped_queue_full'] += 1
        self.max_queue = max(self.max_queue, self.queue.qsize())

    # -- emitter --------------------------------------------------------------------------------------------

    def _send(self, line: str) -> None:
        try:
            self.out.send(line.encode())
        except OSError:
            self.c['send_errors'] += 1

    def _emitter(self) -> None:
        last_gauge = 0.0
        while not self.stop_flag.is_set() or not self.queue.empty():
            try:
                event = self.queue.get(timeout=0.5)
            except queue.Empty:
                event = None
            if event is not None:
                self._process(event)
            if time.time() - last_gauge >= 10:
                last_gauge = time.time()
                self._send('{}.listener.connected:{}|g'.format(PREFIX, 1 if self.connected else 0))
                self._send('{}.listener.queue_depth:{}|g'.format(PREFIX, self.queue.qsize()))

    def _process(self, event: dict) -> None:
        direction = event.get('Call-Direction', '')
        direction = direction if direction in DIRECTIONS else 'other'
        codec = CODECS.get((event.get('variable_rtp_use_codec_name') or '').upper(), 'other')
        mos_raw = event.get('variable_rtp_audio_in_mos')
        quality_raw = event.get('variable_rtp_audio_in_quality_percentage')
        packets_raw = event.get('variable_rtp_audio_in_packet_count')
        timestamp = event.get('Event-Date-Timestamp')
        outcome: str
        try:
            packets = int(packets_raw) if packets_raw is not None else None
            mos = float(mos_raw) if mos_raw is not None else None
            quality = float(quality_raw) if quality_raw is not None else None
        except ValueError:
            packets = mos = quality = None
            outcome = 'invalid'
            mos_raw = quality_raw = None
        else:
            if mos is None or quality is None or packets is None:
                outcome = 'missing'
            elif packets < MIN_PACKETS:
                outcome = 'short' if packets > 0 else 'no_media'
            elif not (math.isfinite(mos) and math.isfinite(quality)):
                outcome = 'no_media'
            elif not (0.0 < mos <= 5.0 and 0.0 <= quality <= 100.0):
                outcome = 'invalid'
            else:
                outcome = 'valid'
        tags = '#direction:{},codec:{}'.format(direction, codec)
        if outcome == 'valid':
            self._send('{}.mos:{}|d|{}'.format(PREFIX, mos, tags))
            self._send('{}.quality:{}|d|{}'.format(PREFIX, quality, tags))
            self.c['submitted_mos'] += 1
            self.c['submitted_quality'] += 1
        self._send('{}.samples:1|c|#outcome:{}'.format(PREFIX, outcome))
        self.c['submitted_counts'] += 1
        self.c['outcome_' + outcome] += 1
        if timestamp:
            self.latency_ms.append(time.time() * 1000 - int(timestamp) / 1000)


def _recv_message(sock: socket.socket, buf: bytes) -> bytes:
    while b'\n\n' not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError('closed')
        buf += chunk
    return buf


def _read(sock: socket.socket, buf: bytes) -> tuple[dict[str, str], str, bytes]:
    buf = _recv_message(sock, buf)
    head, buf = buf.split(b'\n\n', 1)
    headers = dict(line.split(': ', 1) for line in head.decode().split('\n') if ': ' in line)
    length = int(headers.get('Content-Length', 0))
    if length > 262144:
        raise ValueError('event too large')
    while len(buf) < length:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError('closed')
        buf += chunk
    body, buf = buf[:length], buf[length:]
    return headers, body.decode('utf-8', 'replace'), buf


# -- generator ------------------------------------------------------------------------------------------------


class Generator:
    CATEGORIES = [('media', 0.70), ('park', 0.15), ('short', 0.08), ('busy', 0.07)]
    CODEC_VARIANTS = [
        ('', 0.50),
        ('absolute_codec_string=PCMA', 0.20),
        ('absolute_codec_string=speex@16000h@20i', 0.10),
        ('rtp_secure_media=true', 0.10),
        ('rtp_secure_media=true,absolute_codec_string=PCMA', 0.10),
    ]

    def __init__(self, bed: Testbed, rate: float, max_calls: int) -> None:
        self.bed, self.rate, self.max_calls = bed, rate, max_calls
        self.paused = threading.Event()
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.heap: list[tuple[float, str]] = []
        self.calls: list[dict] = []  # ground truth: category, started, ends
        self.active = 0
        self.failed_originates = 0

    def _pick(self, table: list[tuple[str, float]]) -> str:
        roll, total = random.random(), 0.0
        for value, weight in table:
            total += weight
            if roll < total:
                return value
        return table[-1][0]

    def run(self) -> None:
        threading.Thread(target=self._killer, daemon=True).start()
        interval = 1.0 / self.rate
        while not self.stop_flag.is_set():
            time.sleep(random.expovariate(1.0 / interval))
            if self.paused.is_set() or self.active >= self.max_calls:
                continue
            category = self._pick(self.CATEGORIES)
            variant = self._pick(self.CODEC_VARIANTS) if category == 'media' else ''
            uid = str(uuid.uuid4())
            variables = 'origination_uuid={}'.format(uid) + (',' + variant if variant else '')
            if category == 'media':
                app, dest, hold = '&playback(tone_stream://L=400;%(1000,0,440))', '5000', random.uniform(4, 25)
            elif category == 'park':
                app, dest, hold = '&park', '5000', 5.0
            elif category == 'short':
                app, dest, hold = '&park', '5000', 0.3
            else:
                app, dest, hold = '&park', '5002', 0.0
            try:
                reply = self.bed.raw('originate {{{}}}sofia/gateway/loopreg/{} {}'.format(variables, dest, app))
            except (OSError, RuntimeError):
                self.failed_originates += 1
                continue
            started = time.time()
            if reply.startswith('+OK'):
                with self.lock:
                    self.active += 1
                    heapq.heappush(self.heap, (started + hold, uid))
                    self.calls.append({'category': category, 'started': started, 'end': started + hold})
            elif category == 'busy' and 'USER_BUSY' in reply:
                with self.lock:
                    self.calls.append({'category': 'busy', 'started': started, 'end': started})
            else:
                self.failed_originates += 1

    def _killer(self) -> None:
        while not self.stop_flag.is_set() or self.heap:
            with self.lock:
                due = self.heap and self.heap[0][0] <= time.time()
                item = heapq.heappop(self.heap) if due else None
            if item is None:
                time.sleep(0.05)
                continue
            try:
                self.bed.raw('uuid_kill ' + item[1])
            except (OSError, RuntimeError):
                pass  # FreeSWITCH is restarting; the call is gone with it
            with self.lock:
                self.active = max(0, self.active - 1)


# -- orchestration ---------------------------------------------------------------------------------------------


def rss_kb(pid: int) -> int:
    out = subprocess.run(['ps', '-o', 'rss=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def docker_stat(name: str) -> dict[str, str]:
    out = subprocess.run(['docker', 'stats', '--no-stream', '--format', '{{.CPUPerc}}|{{.MemUsage}}', name],
                         capture_output=True, text=True).stdout.strip().split('|')  # fmt: skip
    return {'cpu': out[0], 'mem': out[1].split(' / ')[0] if len(out) > 1 else ''}


def main(out: str, duration: float) -> None:
    import os

    net = 'fs-soak-' + uuid.uuid4().hex[:6]
    subprocess.run(['docker', 'network', 'create', net], check=True, capture_output=True)
    sink_dir = Path(out).with_suffix('.sink')
    timeline: list[dict] = []
    markers: list[dict] = []
    rig = None
    try:
        with Testbed(network=net, max_sessions=3000, sessions_per_second=1000, rtp_end=30000,
                     codecs='PCMU,PCMA,speex@8000h@20i,speex@16000h@20i,L16') as bed:  # fmt: skip
            ready(bed)
            rig = AgentRig(net, check_password=bed.restricted_password)
            rig.start()
            time.sleep(20)
            listener = Listener(bed, rig)
            listener.start()
            generator = Generator(bed, rate=0.8, max_calls=25)
            threading.Thread(target=generator.run, daemon=True).start()
            t0 = time.time()
            plan = [  # (fraction of the run, name)
                (0.33, 'listener_disconnect_60s'), (0.50, 'agent_restart'), (0.67, 'freeswitch_graceful_restart'),
                (0.83, 'listener_process_restart_15s'),
            ]  # fmt: skip
            pending = [(t0 + duration * f, name) for f, name in plan]
            next_sample = t0
            while time.time() - t0 < duration:
                now = time.time()
                if now >= next_sample:
                    next_sample = now + 30
                    status = subprocess.run(['docker', 'exec', bed.name, 'sh', '-c', 'true'], capture_output=True)
                    legs = (
                        re.search(r'^(\d+) session\(s\) - peak', bed.raw('status'), re.M)
                        if status.returncode == 0
                        else None
                    )
                    timeline.append({
                        't': round(now - t0), 'active_legs': int(legs.group(1)) if legs else None,
                        'queue': listener.queue.qsize(), 'connected': listener.connected,
                        'listener_rss_kb': rss_kb(os.getpid()), 'fs': docker_stat(bed.name),
                        'agent': docker_stat(rig.agent),
                        'events': listener.c['events'], 'dropped': listener.c['dropped_queue_full'],
                    })  # fmt: skip
                    print(timeline[-1], flush=True)
                if pending and now >= pending[0][0]:
                    _, name = pending.pop(0)
                    marker = {'name': name, 'start': now, 't': round(now - t0)}
                    print('PLAN', name, flush=True)
                    if name == 'listener_disconnect_60s':
                        listener.hold_until = now + 60
                        listener.drop_connection.set()
                    elif name == 'agent_restart':
                        subprocess.run(['docker', 'restart', '-t', '2', rig.agent], capture_output=True)
                        time.sleep(1)
                        rig.refresh_port()
                        listener.out = rig.sender()
                    elif name == 'freeswitch_graceful_restart':
                        generator.paused.set()
                        time.sleep(12)  # let in-flight calls finish before the restart
                        bed.restart()
                        ready(bed)
                        generator.paused.clear()
                    elif name == 'listener_process_restart_15s':
                        listener.hold_until = now + 15
                        listener.drop_connection.set()
                        listener.seen.clear()  # a new process has no memory of earlier events
                    marker['end'] = time.time()
                    markers.append(marker)
                time.sleep(1)
            generator.stop_flag.set()
            time.sleep(40)  # calls end, last events arrive
            listener.stop()
            time.sleep(3)
            time.sleep(35)  # Agent flush
            payloads = rig.payloads()
            series = series_points(payloads, PREFIX)
            sketches = sketch_points(payloads, PREFIX)
            check_series = series_points(payloads, 'freeswitch.sessions.created')
            counters = rig.dogstatsd_counters()
            rig.stop(keep_sink_dir=sink_dir)
            rig = None
            truth = generator.calls
            lat = sorted(listener.latency_ms)

            def pct(p: float) -> float | None:
                return round(lat[int(len(lat) * p)], 1) if lat else None

            results = {
                'duration_seconds': round(time.time() - t0),
                'planned_duration_seconds': duration,
                'markers': markers,
                'ground_truth': {
                    'calls': len(truth),
                    'by_category': {c: sum(1 for x in truth if x['category'] == c) for c, _ in Generator.CATEGORIES},
                    'legs_expected_events': 2 * len(truth),
                    'valid_samples_expected': 2 * sum(1 for x in truth if x['category'] == 'media'),
                    'failed_originates': generator.failed_originates,
                },
                'listener': listener.c,
                'listener_gaps': [
                    {'seconds': round(g['end'] - g['start'], 1), 't': round(g['start'] - t0)} for g in listener.gaps
                ],
                'max_queue_depth': listener.max_queue,
                'queue_capacity': QUEUE_SIZE,
                'processing_latency_ms': {'p50': pct(0.5), 'p95': pct(0.95), 'max': round(lat[-1], 1) if lat else None},
                'agent_observed': {
                    'mos_samples': sum(p['count'] for p in sketches if p['metric'] == PREFIX + '.mos'),
                    'quality_samples': sum(p['count'] for p in sketches if p['metric'] == PREFIX + '.quality'),
                    'mos_sum': round(sum(p['sum'] for p in sketches if p['metric'] == PREFIX + '.mos'), 2),
                    'outcome_counts': {},
                    'distinct_tag_sets': sorted({tuple(p['tags']) for p in sketches}),
                    'dogstatsd_counters_after_agent_restart': counters,
                },
                'timeline': timeline,
            }
            counts: dict[str, float] = {}
            for p in series:
                if p['metric'] == PREFIX + '.samples':
                    tag = [t for t in p['tags'] if t.startswith('outcome:')][0]
                    counts[tag] = counts.get(tag, 0) + p['value'] * (p['interval'] or 10)
            results['agent_observed']['outcome_counts'] = {k: round(v) for k, v in counts.items()}  # type: ignore[index]
            results['polling_check_sessions_created_points'] = len(check_series)
            results['polling_check_sessions_created_increase_seen_by_agent'] = round(
                sum(p['value'] for p in check_series)
            )
            results['listener_connected_gauge'] = [
                [p['ts'] - int(t0), p['value']] for p in series if p['metric'].endswith('listener.connected')
            ]
            results['calls'] = [[c['category'], round(c['started'] - t0, 1), round(c['end'] - t0, 1)] for c in truth]
            results['series_metric_names'] = sorted({p['metric'] for p in series})
            Path(out).write_text(json.dumps(results, indent=1, default=str))
    finally:
        if rig is not None:
            rig.stop()
        subprocess.run(['docker', 'network', 'rm', net], capture_output=True)


if __name__ == '__main__':
    main(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 3600.0)
