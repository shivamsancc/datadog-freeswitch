#!/usr/bin/env python3
# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Four-hour soak of the real ``fs_rtp_listener`` package against a disposable FreeSWITCH and a disposable Agent.

Research tool, not a test and not part of the polling integration. It never touches the lab container: it creates its own
uniquely named containers and network (all labelled ``datadog-freeswitch.soak=<run id>``), applies memory/CPU/log limits
to them, and removes only what it created. Intake of the Agent is a local sink container, so nothing leaves the machine.

Usage (normally started by ``launch.py``):
    python soak_runner.py --dir <experiment dir> [--duration 14400] [--smoke] [--check-only]

Files written to the experiment directory (all bounded, no UUIDs, caller IDs, extensions or raw events):
    state.json, health.json (every 10 s), progress.jsonl (every 30 s), reconciliation_live.json (every 60 s),
    interruptions.json, ground_truth_calls.json (at the end), final_summary.json, STATUS, runner.log (rotated).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import heapq
import json
import logging
import logging.handlers
import os
import platform
import random
import re
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LISTENER_DIR = HERE.parents[1]
ROOT = HERE.parents[2]
sys.path.insert(0, str(LISTENER_DIR))
sys.path.insert(0, str(ROOT / 'integration' / 'tests' / 'runtime'))

from fs_rtp_listener.config import Config  # noqa: E402
from fs_rtp_listener.esl import EslConnectError, EventClient  # noqa: E402
from fs_rtp_listener.listener import Listener  # noqa: E402
from rtp_agent_lib import AGENT_IMAGE, SINK_SOURCE, AgentRig, series_points, sketch_points  # noqa: E402
from testbed import EVENT_USER, IMAGE as FS_IMAGE, LABEL, Testbed  # noqa: E402

LOG = logging.getLogger('soak')
PREFIX = 'experiment.rtp4h'
SOAK_LABEL = 'datadog-freeswitch.soak'

# ---- explicit, conservative limits ---------------------------------------------------------------------------------
LIMITS = {
    'fs_container_memory': '2048m',  # docker enforces (OOM kill is detected and recorded as a failure)
    'fs_container_cpus': '3',
    'fs_log_tmpfs': '400m',
    'agent_container_memory': '768m',
    'agent_container_cpus': '2',
    'sink_container_memory': '192m',
    'container_log_max': '5m x 3 files',
    'runner_rss_mb_max': 600,
    'host_disk_free_gb_min': 4.0,
    'experiment_dir_mb_max': 150,
    'sink_backlog_files_max': 5000,
    'battery_percent_min_on_battery': 15,
    'queue_saturation_fraction': 0.9,
    'queue_saturation_seconds': 90,
    'consecutive_originate_failures_max': 30,
    'progress_stall_seconds': 180,
}
CALL_RATE = 0.8  # calls per second (Poisson); proven sustainable for 1 h by the earlier harness
MAX_CALLS = 25
CODECS_CONF = 'PCMU,PCMA,speex@8000h@20i,speex@16000h@20i,L16'
UUID_RE = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
ALLOWED_CODEC_TAGS = {'pcmu', 'pcma', 'speex', 'l16'}
SECRETS: list[str] = []


def redact(text: str) -> str:
    for secret in SECRETS:
        if secret:
            text = text.replace(secret, '<secret>')
    return UUID_RE.sub('<uuid>', text)


def redact_container_log(text: str) -> str:
    """Stricter, for the rare captured container log: also drops lines naming credentials and long digit runs."""
    out = []
    for line in text.splitlines():
        if re.search(r'passw|secret|auth', line, re.I):
            out.append('<line withheld>')
            continue
        line = re.sub(r'sofia/\S+', 'sofia/<redacted>', redact(line))
        out.append(re.sub(r'\b\d{4,}\b', '<n>', line))
    return '\n'.join(out)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec='seconds')


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=1, default=str))
    os.replace(tmp, path)


def run(cmd: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def to_mib(text: str) -> float:
    m = re.match(r'([\d.]+)\s*([KMG]i?B|B)', text.strip())
    if not m:
        return 0.0
    value, unit = float(m.group(1)), m.group(2)
    return value * {'B': 1 / 1048576, 'KiB': 1 / 1024, 'KB': 1 / 1024, 'MiB': 1, 'MB': 1, 'GiB': 1024, 'GB': 1024}[unit]


# ---- resource-limited containers (subclasses; the shared helpers are not modified) ------------------------------------


class SoakBed(Testbed):
    """The disposable FreeSWITCH of the earlier experiments, with memory/CPU/log limits and a run label."""

    run_id = ''

    def start(self, wait: float = 60.0) -> None:
        self._dir = Path(tempfile.mkdtemp(prefix='fs-soak-conf-'))
        self._write_config(self._dir)
        cmd = [
            'docker', 'run', '-d', '--init', '--name', self.name, '--label', LABEL,
            '--label', '{}={}'.format(SOAK_LABEL, self.run_id),
            '--memory', LIMITS['fs_container_memory'], '--memory-swap', LIMITS['fs_container_memory'],
            '--cpus', LIMITS['fs_container_cpus'], '--pids-limit', '2048',
            '--log-opt', 'max-size=5m', '--log-opt', 'max-file=3',
            '-v', '{}:/usr/local/freeswitch/conf:ro'.format(self._dir),
            '--tmpfs', '/usr/local/freeswitch/log:uid=999,gid=999,size={}'.format(LIMITS['fs_log_tmpfs']),
            '--tmpfs', '/usr/local/freeswitch/db:uid=999,gid=999,size=64m',
            '--tmpfs', '/usr/local/freeswitch/run:uid=999,gid=999,size=16m',
            '-p', '127.0.0.1:{}:8021/tcp'.format(self.esl_host_port or ''),
            '--network', self.network, '--network-alias', 'fs-testbed',
            FS_IMAGE,
        ]  # fmt: skip
        subprocess.run(cmd, check=True, capture_output=True)
        self._wait_ready(wait)


class SoakRig(AgentRig):
    """Agent + local sink with limits; no polling check is mounted (the existing integration is not exercised)."""

    run_id = ''

    def start(self) -> None:
        label = '{}={}'.format(SOAK_LABEL, self.run_id)
        subprocess.run(
            ['docker', 'run', '-d', '--name', self.sink, '--network', self.network, '--label', label,
             '--memory', LIMITS['sink_container_memory'], '--log-opt', 'max-size=2m', '--log-opt', 'max-file=2',
             '-v', '{}:/data'.format(self.data), 'python:3.13-slim', 'python', '-c', SINK_SOURCE],
            check=True, capture_output=True,
        )  # fmt: skip
        command = [
            'docker', 'run', '-d', '--name', self.agent, '--network', self.network, '--label', label,
            '--memory', LIMITS['agent_container_memory'], '--cpus', LIMITS['agent_container_cpus'],
            '--log-opt', 'max-size=5m', '--log-opt', 'max-file=3',
            '-p', '127.0.0.1:{}:8125/udp'.format(self.fixed_port),
            '-e', 'DD_API_KEY=00000000000000000000000000000000', '-e', 'DD_HOSTNAME=exp',
            '-e', 'DD_DD_URL=http://{}:8080'.format(self.sink), '-e', 'DD_USE_V2_API_SERIES=false',
            '-e', 'DD_SERIALIZER_COMPRESSOR_KIND=zlib', '-e', 'DD_LOGS_ENABLED=false', '-e', 'DD_APM_ENABLED=false',
            '-e', 'DD_PROCESS_AGENT_ENABLED=false', '-e', 'DD_USE_DOGSTATSD=true',
            '-e', 'DD_DOGSTATSD_NON_LOCAL_TRAFFIC=true', '-e', 'DD_DOGSTATSD_PORT=8125',
            '-e', 'DD_LOG_LEVEL=warn', AGENT_IMAGE,
        ]  # fmt: skip
        subprocess.run(command, check=True, capture_output=True)
        self.statsd_port = int(self.fixed_port or 0)
        deadline = time.time() + 150
        while time.time() < deadline:
            status = self.status()
            if 'DogStatsD' in status and 'Udp Packets' in status:
                return
            time.sleep(3)
        raise RuntimeError('agent did not become ready')


# ---- listener harness -------------------------------------------------------------------------------------------------


class Gate:
    """While closed the listener's connection attempts are refused (a controlled, long disconnect)."""

    def __init__(self) -> None:
        self.until = 0.0

    def hold(self, seconds: float) -> None:
        self.until = time.monotonic() + seconds

    def blocked(self) -> bool:
        return time.monotonic() < self.until


class BlockedClient:
    def connect(self) -> None:
        raise EslConnectError('blocked by the experiment')

    def events(self):  # type: ignore[no-untyped-def]
        return iter(())

    def close(self) -> None:
        pass


class Probe:
    """Measurements the listener does not make itself: processing latency and submission times."""

    def __init__(self) -> None:
        self.latency_ms: list[float] = []
        self.submit_times: list[float] = []


def make_listener(cfg: Config, gate: Gate | None, probe: Probe | None) -> Listener:
    def factory() -> Any:
        if gate is not None and gate.blocked():
            return BlockedClient()
        return EventClient(cfg.host, cfg.port, cfg.password, cfg.username, cfg.strict_permissions,
                           cfg.connect_timeout, cfg.read_timeout, cfg.max_event_bytes)  # fmt: skip

    lst = Listener(cfg, client_factory=factory)
    if probe is not None:
        original = lst._process

        def timed(sample: Any) -> None:
            before = lst._counts['submitted_mos']
            original(sample)
            t = now()
            if sample.timestamp_us:
                probe.latency_ms.append((t * 1e6 - sample.timestamp_us) / 1000.0)
            if lst._counts['submitted_mos'] > before:
                probe.submit_times.append(t)

        lst._process = timed  # type: ignore[method-assign]
    return lst


# ---- call generator with ground truth ------------------------------------------------------------------------------------


class Generator:
    CATEGORIES = [('media', 0.70), ('park', 0.15), ('short', 0.08), ('busy', 0.07)]
    VARIANTS = [  # (variables, codec tag expected, weight): only scenarios exercised by the earlier experiments
        ('', 'pcmu', 0.45),
        ('absolute_codec_string=PCMA', 'pcma', 0.20),
        ('absolute_codec_string=speex@16000h@20i', 'speex', 0.10),
        ('rtp_secure_media=true', 'pcmu', 0.08),
        ('rtp_secure_media=true,absolute_codec_string=PCMA', 'pcma', 0.08),
        ('absolute_codec_string=L16', 'l16', 0.09),
    ]

    def __init__(self, bed: Testbed, rate: float, max_calls: int) -> None:
        self.bed, self.rate, self.max_calls = bed, rate, max_calls
        self.paused = threading.Event()
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.heap: list[tuple[float, str]] = []
        self.calls: dict[str, dict[str, Any]] = {}  # ground truth keyed by the (never exported) call id
        self.active = 0
        self.failed_originates = 0
        self.consecutive_failures = 0
        self.attempts = 0

    @staticmethod
    def _pick(table: list[tuple[Any, float]]) -> Any:
        roll, total = random.random(), 0.0
        for value, weight in table:
            total += weight
            if roll < total:
                return value
        return table[-1][0]

    def start_call(self, category: str, scenario: str = 'steady', hold: float | None = None) -> bool:
        variant, codec = '', 'pcmu'
        if category == 'media':
            variant, codec = self._pick([((v, c), w) for v, c, w in self.VARIANTS])
        uid = str(uuid.uuid4())
        variables = 'origination_uuid={}'.format(uid) + (',' + variant if variant else '')
        if category == 'media':
            app, dest, default_hold = '&playback(tone_stream://L=400;%(1000,0,440))', '5000', random.uniform(4, 25)
        elif category == 'park':
            app, dest, default_hold = '&park', '5000', 5.0
        elif category == 'short':
            app, dest, default_hold = '&park', '5000', 0.3
        else:
            app, dest, default_hold = '&park', '5002', 0.0
        hold = default_hold if hold is None else hold
        self.attempts += 1
        try:
            reply = self.bed.raw('originate {{{}}}sofia/gateway/loopreg/{} {}'.format(variables, dest, app))
        except (OSError, RuntimeError):
            reply = ''
        started = now()
        record = {'cat': category, 'scenario': scenario, 'codec': codec if category == 'media' else None,
                  'started': started, 'end': None, 'ok': False}  # fmt: skip
        if reply.startswith('+OK') and category != 'busy':
            self.consecutive_failures = 0
            with self.lock:
                self.active += 1
                self.calls[uid] = record
                heapq.heappush(self.heap, (started + hold, uid))
            return True
        if category == 'busy' and 'USER_BUSY' in reply:
            self.consecutive_failures = 0
            record.update(end=started, ok=True)
            with self.lock:
                self.calls[uid] = record
            return True
        self.failed_originates += 1
        self.consecutive_failures += 1
        return False

    def run(self) -> None:
        threading.Thread(target=self._killer, daemon=True, name='soak-killer').start()
        while not self.stop_flag.is_set():
            time.sleep(random.expovariate(self.rate))
            if self.paused.is_set() or self.active >= self.max_calls or self.stop_flag.is_set():
                continue
            self.start_call(self._pick(self.CATEGORIES))

    def _killer(self) -> None:
        while not self.stop_flag.is_set() or self.heap:
            with self.lock:
                item = heapq.heappop(self.heap) if self.heap and self.heap[0][0] <= time.time() else None
            if item is None:
                time.sleep(0.05)
                continue
            ok = True
            try:
                self.bed.raw('uuid_kill ' + item[1])
            except (OSError, RuntimeError):
                ok = False  # FreeSWITCH is not answering; the call is gone with it or will be recorded unresolved
            with self.lock:
                self.active = max(0, self.active - 1)
                record = self.calls.get(item[1])
                if record is not None:
                    record['end'] = now()
                    record['ok'] = ok


# ---- Agent sink observer -----------------------------------------------------------------------------------------------------


class SinkObserver:
    """Incrementally decodes the payloads the Agent delivered to the local sink; raw payloads are deleted after counting."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir
        self.files = 0
        self.sketch_count = {'mos': 0, 'quality': 0}
        self.sketch_sum = {'mos': 0.0, 'quality': 0.0}
        self.sketch_min = {'mos': None, 'quality': None}
        self.sketch_max = {'mos': None, 'quality': None}
        self.tag_sets: dict[str, int] = {}
        self.outcomes: dict[str, float] = {}
        self.listener_counts: dict[str, float] = {}
        self.gauge_last: dict[str, tuple[int, float]] = {}
        self.connected_zero_points: list[int] = []
        self.connected_points = 0
        self.max_queue_gauge = 0.0
        self.last_point_ts = 0
        self.points_by_ts: list[int] = []  # timestamps of listener gauge points (bounded below)
        self.errors = 0

    def pending(self) -> int:
        return sum(1 for _ in self.dir.glob('*.bin'))

    def poll(self) -> None:
        cutoff = time.time() - 2.5  # a file being written is left for the next poll
        for path in sorted(self.dir.glob('*.bin')):
            try:
                if path.stat().st_mtime > cutoff:
                    continue
                head, _, body = path.read_bytes().partition(b'\n')
                payload = [(head.decode(), body)]
                self._absorb(payload)
                path.unlink()
                self.files += 1
            except (OSError, ValueError, KeyError, IndexError):
                self.errors += 1

    def _absorb(self, payload: list[tuple[str, bytes]]) -> None:
        for p in sketch_points(payload, PREFIX):
            name = 'mos' if p['metric'] == PREFIX + '.mos' else 'quality' if p['metric'] == PREFIX + '.quality_percentage' else None
            if name is None:
                continue
            self.sketch_count[name] += p['count']
            self.sketch_sum[name] += p['sum'] or 0.0
            if p['count']:
                lo, hi = self.sketch_min[name], self.sketch_max[name]
                self.sketch_min[name] = p['min'] if lo is None else min(lo, p['min'])
                self.sketch_max[name] = p['max'] if hi is None else max(hi, p['max'])
            if name == 'mos':
                key = ','.join(p['tags'])
                self.tag_sets[key] = self.tag_sets.get(key, 0) + p['count']
        for p in series_points(payload, PREFIX + '.listener'):
            metric = p['metric'][len(PREFIX) + len('.listener.'):]
            self.last_point_ts = max(self.last_point_ts, p['ts'])
            interval = p['interval'] or 10
            if metric == 'samples':
                tag = next((t for t in p['tags'] if t.startswith('outcome:')), 'outcome:?')
                self.outcomes[tag[8:]] = self.outcomes.get(tag[8:], 0.0) + p['value'] * interval
            elif metric == 'total':
                tag = next((t for t in p['tags'] if t.startswith('counter:')), 'counter:?')[8:]
                if tag not in self.gauge_last or p['ts'] >= self.gauge_last[tag][0]:
                    self.gauge_last[tag] = (p['ts'], p['value'])
            elif metric == 'connected':
                self.connected_points += 1
                if p['value'] == 0:
                    self.connected_zero_points.append(p['ts'])
            elif metric == 'queue_depth':
                self.max_queue_gauge = max(self.max_queue_gauge, p['value'])
            else:
                self.listener_counts[metric] = self.listener_counts.get(metric, 0.0) + p['value'] * interval

    def view(self) -> dict[str, Any]:
        return {
            'payload_files_processed': self.files,
            'decode_errors': self.errors,
            'mos_samples': self.sketch_count['mos'],
            'quality_samples': self.sketch_count['quality'],
            'mos_min': self.sketch_min['mos'], 'mos_max': self.sketch_max['mos'],
            'mos_sum': round(self.sketch_sum['mos'], 2),
            'outcome_counts': {k: round(v) for k, v in sorted(self.outcomes.items())},
            'listener_counts': {k: round(v, 1) for k, v in sorted(self.listener_counts.items())},
            'tag_sets': dict(sorted(self.tag_sets.items())),
            'gauge_last': {k: v[1] for k, v in sorted(self.gauge_last.items())},
            'connected_gauge_points': self.connected_points,
            'connected_gauge_zero_points': len(self.connected_zero_points),
            'max_queue_depth_gauge': self.max_queue_gauge,
            'last_listener_point_epoch': self.last_point_ts,
        }  # fmt: skip


# ---- the experiment -------------------------------------------------------------------------------------------------------


class Soak:
    def __init__(self, directory: Path, duration: float, smoke: bool) -> None:
        self.dir = directory
        self.duration = duration
        self.smoke = smoke
        self.run_id = uuid.uuid4().hex[:8]
        self.net = 'fs-soak4h-' + self.run_id
        self.stop_event = threading.Event()
        self.abort_reason: str | None = None
        self.abort_status = 'failed'
        self.maintenance: set[str] = set()
        self.probe = Probe()
        self.gate = Gate()
        self.bed: SoakBed | None = None
        self.rig: SoakRig | None = None
        self.gen: Generator | None = None
        self.cfg: Config | None = None
        self.listener: Listener | None = None
        self.incarnations: list[dict[str, Any]] = []
        self.gaps: list[tuple[float, float]] = []
        self.interruptions: list[dict[str, Any]] = []
        self.agent_windows: list[tuple[float, float]] = []
        self.observer: SinkObserver | None = None
        self.agent_counters_log: list[dict[str, Any]] = []
        self.t0 = 0.0
        self.start_wall = now()
        self.max = {'runner_rss_mb': 0.0, 'fs_mem_mib': 0.0, 'agent_mem_mib': 0.0, 'sink_mem_mib': 0.0,
                    'fs_cpu': 0.0, 'agent_cpu': 0.0, 'queue_depth': 0, 'threads': 0, 'experiment_dir_mb': 0.0}  # fmt: skip
        self.saturated_since: float | None = None
        self.leak_findings: list[str] = []
        self.snap_lock = threading.Lock()
        self.warnings: list[str] = []
        self.phase = 'starting'
        self._open_gap_start: float | None = None
        self.lst_lock = threading.RLock()

    # -- plan --------------------------------------------------------------------------------------------------------

    def plan(self) -> list[dict[str, Any]]:
        if self.smoke:
            rows = [(25, 'listener_disconnect_hold', {'hold': 10}), (70, 'queue_overflow', {}),
                    (120, 'agent_restart', {}), (175, 'listener_restart', {'hold': 5}),
                    (225, 'freeswitch_restart', {}), (320, 'listener_force_disconnect', {})]  # fmt: skip
        else:
            rows = [(1500, 'listener_disconnect_hold', {'hold': 60}), (2700, 'queue_overflow', {}),
                    (3900, 'agent_restart', {}), (5100, 'listener_restart', {'hold': 15}),
                    (6300, 'freeswitch_restart', {}), (7500, 'listener_force_disconnect', {}),
                    (9000, 'agent_restart', {}), (10500, 'freeswitch_restart', {}),
                    (11700, 'listener_restart', {'hold': 15}), (12900, 'queue_overflow', {})]  # fmt: skip
        return [{'name': n, 'planned_t': t, 'params': p} for t, n, p in rows]

    # -- helpers ----------------------------------------------------------------------------------------------------

    def log_event(self, message: str, *args: object) -> None:
        LOG.info(message, *args)

    def abort(self, reason: str, status: str = 'failed') -> None:
        if self.abort_reason is None:
            self.abort_reason, self.abort_status = reason, status
            LOG.error('ABORT: %s', reason)
            self.stop_event.set()

    def wait(self, seconds: float) -> bool:
        """Sleep, returning False early if the experiment is being stopped."""
        return not self.stop_event.wait(seconds)

    def wait_for(self, predicate: Any, timeout: float, interval: float = 0.25) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.stop_event.is_set():
            if predicate():
                return True
            time.sleep(interval)
        return bool(predicate())

    def fs_ready(self, timeout: float = 150.0) -> bool:
        assert self.bed is not None

        def ok() -> bool:
            try:
                return self.bed.raw('show registrations count').strip() == '1 total.' and (  # type: ignore[union-attr]
                    re.search(r'^Status\s+UP', self.bed.raw('sofia status gateway loopreg'), re.M) is not None  # type: ignore[union-attr]
                )
            except (OSError, RuntimeError):
                return False

        return self.wait_for(ok, timeout, 1.0)

    def agent_ready(self, timeout: float = 150.0) -> bool:
        assert self.rig is not None
        return self.wait_for(lambda: 'DogStatsD' in self.rig.status(), timeout, 2.0)  # type: ignore[union-attr]

    # -- listener management ---------------------------------------------------------------------------------------------

    def start_listener(self) -> None:
        assert self.cfg is not None
        self.listener = make_listener(self.cfg, self.gate, self.probe)
        self.incarnations.append({'index': len(self.incarnations), 'start': now(), 'end': None, 'final': None,
                                  'clean_stop': None})  # fmt: skip
        self.listener.start()

    def stop_listener(self) -> bool:
        lst = self.listener
        if lst is None:
            return True
        clean = lst.stop(timeout=15.0)
        snap = lst.snapshot()
        with self.lst_lock:
            inc = self.incarnations[-1]
            inc.update(end=now(), final=snap, clean_stop=clean)
            self.listener = None
        return clean

    def totals(self) -> dict[str, Any]:
        counters: dict[str, float] = {}
        outcomes: dict[str, int] = {}
        gap_total = 0.0
        with self.lst_lock:
            snaps = [i['final'] for i in self.incarnations if i['final'] is not None]
            live = self.listener
            if live is not None:
                snaps.append(live.snapshot())
        for snap in snaps:
            for k, v in snap['counters'].items():
                counters[k] = counters.get(k, 0) + v
            for k, v in snap['outcomes'].items():
                outcomes[k] = outcomes.get(k, 0) + v
            gap_total += snap['gap_seconds_total']
        cur = snaps[-1] if live is not None else None
        return {'counters': counters, 'outcomes': outcomes, 'gap_seconds_listener': round(gap_total, 1),
                'queue_depth': cur['queue_depth'] if cur else 0, 'connected': bool(cur and cur['connected']),
                'max_queue_depth': max([s['max_queue_depth'] for s in snaps] or [0])}  # fmt: skip

    # -- gap sampler ----------------------------------------------------------------------------------------------------

    def gap_loop(self) -> None:
        start: float | None = None
        seen_connected = False
        while not self.stop_event.is_set() or self.phase == 'draining':
            lst = self.listener
            connected = bool(lst is not None and lst.connected)
            t = now()
            if connected:
                seen_connected = True
            if seen_connected:
                if not connected and start is None:
                    start = t
                    self._open_gap_start = t
                elif connected and start is not None:
                    self.gaps.append((start, t))
                    start = None
                    self._open_gap_start = None
            if self.phase == 'done':
                break
            time.sleep(0.2)
        if start is not None:
            self.gaps.append((start, now()))
            self._open_gap_start = None

    # -- interruptions --------------------------------------------------------------------------------------------------

    def run_interruption(self, item: dict[str, Any]) -> None:
        assert self.gen is not None and self.bed is not None and self.rig is not None
        name = item['name']
        record: dict[str, Any] = {
            'name': name, 'planned_t': item['planned_t'], 'planned_epoch': self.t0 + item['planned_t'],
            'planned_time': iso(self.t0 + item['planned_t']), 'params': item['params'],
        }  # fmt: skip
        start = now()
        record.update(actual_start_epoch=start, actual_start=iso(start), actual_t=round(start - self.t0, 1))
        LOG.info('INTERRUPTION start: %s (planned t=%s, actual t=%.0f)', name, item['planned_t'], start - self.t0)
        self.phase = 'interruption:' + name
        outcome: dict[str, Any] = {}
        try:
            before = self.totals()
            if name == 'listener_disconnect_hold':
                reconnects = before['counters'].get('reconnects', 0)
                self.gate.hold(item['params']['hold'])
                self.listener.force_disconnect()  # type: ignore[union-attr]
                outcome['disconnected'] = self.wait_for(lambda: not self.listener.connected, 10)  # type: ignore[union-attr]
                outcome['reconnected'] = self.wait_for(
                    lambda: self.totals()['counters'].get('reconnects', 0) > reconnects and self.listener.connected,  # type: ignore[union-attr]
                    item['params']['hold'] + 90)
            elif name == 'listener_force_disconnect':
                reconnects = before['counters'].get('reconnects', 0)
                self.listener.force_disconnect()  # type: ignore[union-attr]
                outcome['reconnected'] = self.wait_for(
                    lambda: self.totals()['counters'].get('reconnects', 0) > reconnects and self.listener.connected,  # type: ignore[union-attr]
                    60)
            elif name == 'listener_restart':
                t = now()
                outcome['clean_stop'] = self.stop_listener()
                outcome['stop_seconds'] = round(now() - t, 2)
                self.wait(item['params']['hold'])
                self.start_listener()
                outcome['reconnected'] = self.wait_for(lambda: self.listener.connected, 60)  # type: ignore[union-attr]
            elif name == 'agent_restart':
                self.maintenance.add('agent')
                self.agent_windows.append((now(), 0.0))
                run(['docker', 'restart', '-t', '2', self.rig.agent], 90)
                t = now()
                outcome['agent_ready'] = self.agent_ready()
                outcome['agent_ready_seconds'] = round(now() - t, 1)
                self.agent_windows[-1] = (self.agent_windows[-1][0], now())
                self.maintenance.discard('agent')
                self.agent_counters_log.append({'after': name, 'counters': self.rig.dogstatsd_counters()})
            elif name == 'freeswitch_restart':
                self.maintenance.add('fs')
                self.gen.paused.set()
                outcome['calls_in_flight_before_wait'] = self.gen.active
                self.wait_for(lambda: self.gen.active == 0, 45)  # type: ignore[union-attr]
                outcome['calls_in_flight_at_restart'] = self.gen.active
                t = now()
                self.bed.restart()
                outcome['fs_restart_seconds'] = round(now() - t, 1)
                outcome['fs_ready'] = self.fs_ready()
                outcome['listener_reconnected'] = self.wait_for(lambda: self.listener.connected, 120)  # type: ignore[union-attr]
                self.maintenance.discard('fs')
                events_before = self.totals()['counters'].get('events', 0)
                self.gen.paused.clear()
                outcome['events_resumed'] = self.wait_for(
                    lambda: self.totals()['counters'].get('events', 0) > events_before, 90)
            elif name == 'queue_overflow':
                outcome.update(self.scenario_overflow())
            after = self.totals()
            outcome['reconnects_delta'] = after['counters'].get('reconnects', 0) - before['counters'].get('reconnects', 0)
            outcome['connect_errors_delta'] = (
                after['counters'].get('connect_errors', 0) - before['counters'].get('connect_errors', 0))
        except Exception as error:  # an interruption that itself failed is a finding, not a reason to hide
            outcome['exception'] = type(error).__name__
            LOG.exception('interruption failed')
        finally:
            self.maintenance.clear()
            self.phase = 'steady'
        end = now()
        outcome['listener_gaps_seconds'] = [round(b - a, 1) for a, b in list(self.gaps) if b >= start - 1 and a <= end]
        record.update(actual_end_epoch=end, actual_end=iso(end), duration_seconds=round(end - start, 1),
                      outcome=outcome)  # fmt: skip
        record['ok'] = bool(outcome) and 'exception' not in outcome and all(
            v for k, v in outcome.items()
            if k in ('disconnected', 'reconnected', 'agent_ready', 'fs_ready', 'listener_reconnected', 'events_resumed',
                     'clean_stop', 'conserved', 'drops_observed', 'queue_bounded', 'overflow_listener_connected'))
        self.interruptions.append(record)
        write_json(self.dir / 'interruptions.json', {'interruptions': self.interruptions, 'planned': self.plan()})
        LOG.info('INTERRUPTION end: %s ok=%s', name, record['ok'])

    def scenario_overflow(self) -> dict[str, Any]:
        """A second listener (queue of 3, slowed emitter, private UDP target) gets a burst of real hangup events."""
        assert self.cfg is not None and self.gen is not None
        sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink.bind(('127.0.0.1', 0))  # never read: nothing reaches the Agent or any other service
        cfg = dataclasses.replace(self.cfg, queue_size=3, statsd_port=sink.getsockname()[1], health_interval=1.0)
        small = Listener(cfg)
        small._emit_hook = lambda: time.sleep(0.2)  # tests-only hook of the listener: forces the queue to overflow
        out: dict[str, Any] = {'queue_size': 3, 'burst_calls': 20}
        small.start()
        try:
            if not self.wait_for(lambda: small.connected, 30):
                out['overflow_listener_connected'] = False
                return out
            self.gen.paused.set()
            started = sum(1 for _ in range(20) if self.gen.start_call('media', scenario='overflow', hold=6.0))
            out['burst_started'] = started
            self.wait_for(lambda: small.snapshot()['counters']['events'] >= 2 * started, 45)  # type: ignore[index]
            self.wait_for(lambda: small.snapshot()['queue_depth'] == 0, 60)
            time.sleep(1.5)
            snap = small.snapshot()
            c, o = snap['counters'], snap['outcomes']  # type: ignore[assignment]
            processed = sum(o.values())  # type: ignore[union-attr]
            out.update(events=c['events'], processed=processed, dropped=c['dropped'], duplicates=c['duplicates'],  # type: ignore[index]
                       max_queue_depth=snap['max_queue_depth'])  # fmt: skip
            out['drops_observed'] = c['dropped'] > 0  # type: ignore[index]
            out['conserved'] = c['events'] == processed + c['dropped'] + c['duplicates'] + snap['queue_depth']  # type: ignore[index,operator]
            out['queue_bounded'] = snap['max_queue_depth'] <= 3  # type: ignore[operator]
        finally:
            small.stop()
            sink.close()
            self.gen.paused.clear()
        return out

    # -- accounting -----------------------------------------------------------------------------------------------------

    def reconcile(self, final: bool = False) -> dict[str, Any]:
        assert self.gen is not None and self.observer is not None
        with self.gen.lock:
            calls = [dict(c) for c in self.gen.calls.values()]
        open_start = self._open_gap_start
        gaps = list(self.gaps) + ([(open_start, now())] if open_start else [])
        resolved = [c for c in calls if c['end'] is not None]
        unresolved = len(calls) - len(resolved)

        def in_gap(t: float, margin: float = 0.0) -> bool:
            return any(gs - margin <= t <= ge + margin for gs, ge in gaps)

        expected = {'valid': 0, 'no_media': 0, 'missing_fields': 0}
        expected_gap = {'valid': 0, 'no_media': 0, 'missing_fields': 0}
        ambiguous = 0
        by_cat: dict[str, int] = {}
        expected_by_codec: dict[str, int] = {}
        for c in resolved:
            klass = {'media': 'valid', 'park': 'no_media', 'short': 'no_media', 'busy': 'missing_fields'}[c['cat']]
            by_cat[c['cat']] = by_cat.get(c['cat'], 0) + 1
            lost = in_gap(c['end'])
            if lost:
                expected_gap[klass] += 2
            else:
                expected[klass] += 2
                if c['cat'] == 'media':
                    expected_by_codec[c['codec']] = expected_by_codec.get(c['codec'], 0) + 2
                if in_gap(c['end'], 1.0):
                    ambiguous += 1
        tot = self.totals()
        c, o = tot['counters'], tot['outcomes']
        expected_events_all = 2 * len(resolved)
        expected_events_effective = sum(expected.values())
        received = int(c.get('events', 0))
        processed = sum(o.values())
        submitted = int(c.get('submitted_mos', 0))
        agent = self.observer.view()
        lost_to_agent = submitted - agent['mos_samples']
        windows = list(self.agent_windows)
        # unflushed samples (up to one 10 s aggregation interval, plus slack) die with the Agent process
        lead, tail = 15.0, 5.0
        in_windows = sum(1 for t in self.probe.submit_times
                         if any(a - lead <= t <= (b or now()) + tail for a, b in windows))
        return {
            'final': final,
            'generated_at': iso(now()),
            'calls': {'started_ok': len(calls), 'resolved': len(resolved), 'unresolved': unresolved,
                      'by_category': by_cat, 'failed_originates': self.gen.failed_originates,
                      'originate_attempts': self.gen.attempts},  # fmt: skip
            'expected_events_total': expected_events_all,
            'expected_events_lost_in_listener_gaps': sum(expected_gap.values()),
            'expected_events_effective': expected_events_effective,
            'events_in_gap_boundary_ambiguous_calls': ambiguous,
            'received_events': received,
            'processed_events': processed,
            'dropped_queue_full': int(c.get('dropped', 0)),
            'duplicates': int(c.get('duplicates', 0)),
            'parse_errors': int(c.get('parse_errors', 0)),
            'unexplained_missing_events': expected_events_effective - received,
            'conservation_residual': received - (processed + int(c.get('dropped', 0)) + int(c.get('duplicates', 0))
                                                 + tot['queue_depth']),
            'expected_outcomes_effective': expected,
            'observed_outcomes': o,
            'valid_samples_expected_effective': expected['valid'],
            'valid_samples_by_codec_expected': dict(sorted(expected_by_codec.items())),
            'rejected_samples_observed': sum(v for k, v in o.items() if k not in ('valid', 'no_media', 'missing_fields')),
            'submitted_dogstatsd_mos': submitted,
            'submitted_dogstatsd_quality': int(c.get('submitted_quality', 0)),
            'listener_send_errors': int(c.get('send_errors', 0)),
            'agent_observed': agent,
            'lost_between_listener_and_agent': lost_to_agent,
            'submitted_during_agent_down_windows': in_windows,
            'agent_down_windows': [[iso(a), iso(b) if b else None] for a, b in windows],
            'agent_window_definition': 'restart start minus 15 s (unflushed aggregation) to ready plus 5 s',
            'lost_not_attributable_to_agent_windows': lost_to_agent - in_windows,
            'listener_reconnects': int(c.get('reconnects', 0)),
            'listener_connect_errors': int(c.get('connect_errors', 0)),
            'gap_seconds_listener_total': tot['gap_seconds_listener'],
            'gap_seconds_sampled_total': round(sum(b - a for a, b in gaps), 1),
            'gap_count_sampled': len(gaps),
            'incarnations': len(self.incarnations),
        }

    # -- monitor ----------------------------------------------------------------------------------------------------------

    def sample_containers(self) -> dict[str, Any]:
        assert self.bed and self.rig
        names = [self.bed.name, self.rig.agent, self.rig.sink]
        out = run(['docker', 'stats', '--no-stream', '--format', '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.PIDs}}', *names],
                  45).stdout
        res: dict[str, Any] = {}
        for line in out.strip().splitlines():
            parts = line.split('|')
            if len(parts) == 4:
                label = 'fs' if parts[0] == self.bed.name else 'agent' if parts[0] == self.rig.agent else 'sink'
                res[label] = {'cpu': parts[1], 'mem_mib': round(to_mib(parts[2].split(' / ')[0]), 1), 'pids': parts[3]}
        return res

    def safety_checks(self) -> None:
        assert self.bed and self.rig
        # components must still run (planned restarts are announced through self.maintenance)
        names = {'fs': self.bed.name, 'agent': self.rig.agent, 'sink': self.rig.sink}
        out = run(['docker', 'inspect', '-f', '{{.Name}}|{{.State.Running}}|{{.State.ExitCode}}|{{.State.OOMKilled}}',
                   *names.values()], 30).stdout
        for line in out.strip().splitlines():
            name, running, code, oom = line.lstrip('/').split('|')
            label = next((k for k, v in names.items() if v == name), name)
            if running != 'true' and label not in self.maintenance:
                self.capture_failure(name)
                self.abort('container_exited:{} exit={} oom={}'.format(label, code, oom))
        # host
        free_gb = shutil.disk_usage(self.dir).free / 1e9
        if free_gb < LIMITS['host_disk_free_gb_min']:
            self.abort('host_disk_low:{:.1f}GB'.format(free_gb))
        size_mb = sum(p.stat().st_size for p in self.dir.rglob('*') if p.is_file()) / 1e6
        self.max['experiment_dir_mb'] = max(self.max['experiment_dir_mb'], size_mb)
        if size_mb > LIMITS['experiment_dir_mb_max']:
            self.abort('experiment_dir_too_large:{:.0f}MB'.format(size_mb))
        rss = int(run(['ps', '-o', 'rss=', '-p', str(os.getpid())]).stdout.strip() or 0) / 1024
        self.max['runner_rss_mb'] = max(self.max['runner_rss_mb'], rss)
        if rss > LIMITS['runner_rss_mb_max']:
            self.abort('runner_memory_high:{:.0f}MB'.format(rss))
        batt = run(['pmset', '-g', 'batt']).stdout
        m = re.search(r'(\d+)%', batt)
        if 'Battery Power' in batt and m and int(m.group(1)) < LIMITS['battery_percent_min_on_battery']:
            self.abort('battery_low:{}%'.format(m.group(1)), 'interrupted')
        if self.observer and self.observer.pending() > LIMITS['sink_backlog_files_max']:
            self.abort('sink_backlog_too_large')
        if self.gen and self.gen.consecutive_failures >= LIMITS['consecutive_originate_failures_max']:
            self.abort('call_generator_failing')
        # queue saturation outside the planned overflow scenario
        snap = self.totals()
        cap = self.cfg.queue_size if self.cfg else 2000
        if snap['queue_depth'] >= LIMITS['queue_saturation_fraction'] * cap:
            self.saturated_since = self.saturated_since or time.monotonic()
            if time.monotonic() - self.saturated_since > LIMITS['queue_saturation_seconds']:
                self.abort('main_queue_saturated')
        else:
            self.saturated_since = None

    def capture_failure(self, name: str) -> None:
        cap = self.dir / 'failure_capture'
        cap.mkdir(exist_ok=True)
        try:
            inspect = run(['docker', 'inspect', '-f',
                           '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} error={{.State.Error}} '
                           'finished={{.State.FinishedAt}}', name]).stdout
            logs = run(['docker', 'logs', '--tail', '150', name], 30)
            (cap / (name + '.txt')).write_text(redact_container_log(
                inspect + '\n' + logs.stdout + '\n' + logs.stderr))
        except Exception:
            LOG.exception('could not capture container state')

    def leak_scan(self) -> list[str]:
        findings = []
        for path in self.dir.rglob('*'):
            if not path.is_file() or path.name.endswith('.tmp'):
                continue
            try:
                text = path.read_text(errors='replace')
            except OSError:
                continue
            for secret in SECRETS:
                if secret and secret in text:
                    findings.append('{}: contains a generated credential'.format(path.name))
            if UUID_RE.search(text):
                findings.append('{}: contains a UUID-shaped string'.format(path.name))
        return findings

    def monitor_loop(self) -> None:
        last_progress = last_recon = last_leak = last_agent = 0.0
        last_health = last_poll = 0.0
        while self.phase != 'done':
            t = time.time()
            try:
                if t - last_health >= 10:
                    last_health = t
                    self.write_health()
                if self.observer and t - last_poll >= 5:
                    last_poll = t
                    self.observer.poll()
                if t - last_progress >= 30:
                    last_progress = t
                    self.write_progress()
                    self.safety_checks()
                if t - last_recon >= 60:
                    last_recon = t
                    write_json(self.dir / 'reconciliation_live.json', self.reconcile())
                if t - last_leak >= 600:
                    last_leak = t
                    found = self.leak_scan()
                    if found:
                        self.leak_findings = found
                        self.abort('sensitive_data_in_evidence', 'failed')
                if t - last_agent >= 300 and self.rig and 'agent' not in self.maintenance:
                    last_agent = t
                    self.agent_counters_log.append({'t': round(t - self.t0), 'counters': self.rig.dogstatsd_counters()})
            except Exception:
                LOG.exception('monitor iteration failed')
            time.sleep(1)

    def write_health(self) -> None:
        tot = self.totals()
        write_json(self.dir / 'health.json', {
            'heartbeat_epoch': now(), 'heartbeat': iso(now()), 'pid': os.getpid(),
            'elapsed_seconds': round(now() - self.t0, 1) if self.t0 else None, 'planned_seconds': self.duration,
            'phase': self.phase, 'abort_reason': self.abort_reason, 'listener_connected': tot['connected'],
            'queue_depth': tot['queue_depth'], 'events_received': tot['counters'].get('events', 0),
            'calls_active': self.gen.active if self.gen else None,
        })  # fmt: skip

    def write_progress(self) -> None:
        tot = self.totals()
        stats = self.sample_containers()
        rss = int(run(['ps', '-o', 'rss=', '-p', str(os.getpid())]).stdout.strip() or 0) / 1024
        self.max['runner_rss_mb'] = max(self.max['runner_rss_mb'], rss)
        self.max['threads'] = max(self.max['threads'], threading.active_count())
        self.max['queue_depth'] = max(self.max['queue_depth'], tot['max_queue_depth'])
        for label, key in (('fs', 'fs_mem_mib'), ('agent', 'agent_mem_mib'), ('sink', 'sink_mem_mib')):
            if label in stats:
                self.max[key] = max(self.max[key], stats[label]['mem_mib'])
        for label in ('fs', 'agent'):
            if label in stats:
                try:
                    self.max[label + '_cpu'] = max(self.max[label + '_cpu'], float(stats[label]['cpu'].rstrip('%')))
                except ValueError:
                    pass
        record = {
            'epoch': round(now(), 1), 'time': iso(now()), 't': round(now() - self.t0, 1), 'phase': self.phase,
            'listener': {'connected': tot['connected'], 'queue_depth': tot['queue_depth'],
                         'max_queue_depth': tot['max_queue_depth'], 'rss_mb_runner': round(rss, 1),
                         'threads': threading.active_count(), 'counters': tot['counters'],
                         'outcomes': tot['outcomes'], 'gap_seconds': tot['gap_seconds_listener']},
            'generator': {'active': self.gen.active if self.gen else None,
                          'calls': len(self.gen.calls) if self.gen else 0,
                          'failed_originates': self.gen.failed_originates if self.gen else 0},
            'containers': stats,
            'agent_observed': {'mos_samples': self.observer.sketch_count['mos'] if self.observer else None,
                               'payload_files': self.observer.files if self.observer else None},
            'latency_ms_p95': self.percentile(0.95),
            'abort_reason': self.abort_reason,
        }  # fmt: skip
        with open(self.dir / 'progress.jsonl', 'a') as handle:
            handle.write(json.dumps(record, default=str) + '\n')

    def percentile(self, p: float) -> float | None:
        data = sorted(self.probe.latency_ms)
        return round(data[min(len(data) - 1, int(len(data) * p))], 1) if data else None

    # -- lifecycle ----------------------------------------------------------------------------------------------------------

    def environment(self) -> dict[str, Any]:
        def sh(cmd: list[str]) -> str:
            try:
                return run(cmd, 20).stdout.strip()
            except Exception:
                return ''

        src = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:16]
               for p in sorted((LISTENER_DIR / 'fs_rtp_listener').glob('*.py'))}  # fmt: skip
        return {
            'python': sys.version.split()[0], 'platform': platform.platform(), 'cpus': os.cpu_count(),
            'docker_server': sh(['docker', 'version', '--format', '{{.Server.Version}}']),
            'images': {'freeswitch': sh(['docker', 'image', 'inspect', '-f', '{{.Id}}', FS_IMAGE])[:19],
                       'agent': sh(['docker', 'image', 'inspect', '-f', '{{.Id}}', AGENT_IMAGE])[:19],
                       'sink': sh(['docker', 'image', 'inspect', '-f', '{{.Id}}', 'python:3.13-slim'])[:19]},  # fmt: skip
            'git_head': sh(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']),
            'git_branch': sh(['git', '-C', str(ROOT), 'branch', '--show-current']),
            'listener_source_sha256_prefix': src,
            'listener_config': repr(self.cfg),
            'limits': LIMITS, 'call_rate_per_second': CALL_RATE, 'max_concurrent_calls': MAX_CALLS,
            'metric_prefix': PREFIX,
        }

    def setup(self) -> None:
        self.phase = 'setup'
        run(['docker', 'network', 'create', '--label', '{}={}'.format(SOAK_LABEL, self.run_id), self.net], 30)
        import socket as _s

        with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            esl_port = s.getsockname()[1]
        with _s.socket(_s.AF_INET, _s.SOCK_DGRAM) as s:
            s.bind(('127.0.0.1', 0))
            statsd_port = s.getsockname()[1]
        SoakBed.run_id = self.run_id
        self.bed = SoakBed(network=self.net, max_sessions=3000, sessions_per_second=1000, rtp_end=30000,
                           esl_host_port=esl_port, codecs=CODECS_CONF)  # fmt: skip
        SECRETS.extend([self.bed.password, self.bed.restricted_password, self.bed._reg_password])
        self.persist_state('starting')
        self.bed.start()
        self.persist_state('starting')
        if not self.fs_ready():
            raise RuntimeError('disposable FreeSWITCH did not become ready')
        SoakRig.run_id = self.run_id
        self.rig = SoakRig(self.net, statsd_host_port=statsd_port)
        self.persist_state('starting')
        self.rig.start()
        self.observer = SinkObserver(self.rig.data)
        self.cfg = Config(host='127.0.0.1', port=self.bed.port, username='{}@test.local'.format(EVENT_USER),
                          password=self.bed.restricted_password, statsd_port=statsd_port, metric_prefix=PREFIX)  # fmt: skip
        write_json(self.dir / 'environment.json', self.environment())

    def persist_state(self, status: str) -> None:
        write_json(self.dir / 'state.json', {
            'run_id': self.run_id, 'status': status, 'pid': os.getpid(), 'network': self.net,
            'containers': {'freeswitch': self.bed.name if self.bed else None,
                           'agent': self.rig.agent if self.rig else None, 'sink': self.rig.sink if self.rig else None},
            'duration_seconds': self.duration, 'smoke': self.smoke,
            'start': iso(self.start_wall), 'generation_start': iso(self.t0) if self.t0 else None,
            'expected_generation_end': iso(self.t0 + self.duration) if self.t0 else None,
        })  # fmt: skip

    def cleanup(self) -> None:
        """Remove only what this run created."""
        names = []
        if self.bed:
            names.append(self.bed.name)
        if self.rig:
            names += [self.rig.agent, self.rig.sink]
        if names:
            run(['docker', 'rm', '-f', *names], 90)
        run(['docker', 'network', 'rm', self.net], 30)
        if self.bed and self.bed._dir is not None:
            shutil.rmtree(self.bed._dir, ignore_errors=True)
        if self.rig:
            shutil.rmtree(self.rig.data, ignore_errors=True)
            shutil.rmtree(self.rig.conf, ignore_errors=True)

    def main(self) -> int:
        status = 'failed'
        try:
            self.setup()
            self.start_listener()
            if not self.wait_for(lambda: self.listener.connected, 60):  # type: ignore[union-attr]
                raise RuntimeError('listener could not connect')
            self.gen = Generator(self.bed, CALL_RATE, MAX_CALLS)  # type: ignore[arg-type]
            threading.Thread(target=self.gen.run, daemon=True, name='soak-generator').start()
            self.t0 = now()
            self.phase = 'steady'
            self.persist_state('running')
            threading.Thread(target=self.gap_loop, daemon=True, name='soak-gaps').start()
            threading.Thread(target=self.monitor_loop, daemon=True, name='soak-monitor').start()
            pending = self.plan()
            LOG.info('generation started; expected end %s', iso(self.t0 + self.duration))
            while not self.stop_event.is_set() and now() - self.t0 < self.duration:
                if pending and now() - self.t0 >= pending[0]['planned_t']:
                    self.run_interruption(pending.pop(0))
                    continue
                time.sleep(0.5)
            elapsed = now() - self.t0
            completed = self.abort_reason is None and elapsed >= self.duration
            self.phase = 'draining'
            self.gen.stop_flag.set()
            if completed:
                self.stop_event.clear()
            LOG.info('generation ended after %.0f s (completed=%s); draining', elapsed, completed)
            self.drain(full=completed)
            status = 'completed' if completed and self.abort_reason is None else self.abort_status
            summary = self.finalize(status, elapsed)
            return 0 if status == 'completed' else (3 if status == 'interrupted' else 4)
        except BaseException as error:
            LOG.exception('runner failed')
            if self.abort_reason is None:
                self.abort_reason = 'runner_exception:' + type(error).__name__
            try:
                self.phase = 'draining'
                if self.gen:
                    self.gen.stop_flag.set()
                    self.drain(full=False)
                    self.finalize('failed', now() - self.t0 if self.t0 else 0.0)
                else:
                    write_json(self.dir / 'final_summary.json', {
                        'status': 'failed', 'completed_full_duration': False, 'abort_reason': self.abort_reason,
                        'stage': 'setup', 'finished': iso(now()), 'run_id': self.run_id})  # fmt: skip
                    (self.dir / 'STATUS').write_text('failed\n')
            except Exception:
                LOG.exception('finalization failed')
            return 4
        finally:
            self.phase = 'done'
            try:
                self.cleanup()
            except Exception:
                LOG.exception('cleanup failed')

    def drain(self, full: bool) -> None:
        end = time.time() + (45 if full else 5)
        while time.time() < end and self.gen and (self.gen.active or self.gen.heap):
            time.sleep(0.5)
        time.sleep(5 if full else 1)
        if self.listener is not None:
            self.stop_listener()  # drains the queue and sends the final health report
        if full:
            time.sleep(35)  # two Agent flush intervals
        if self.observer:
            self.observer.poll()
            time.sleep(3)
            self.observer.poll()

    def checks(self, rec: dict[str, Any], completed: bool, elapsed: float) -> list[dict[str, Any]]:
        def row(name: str, ok: bool, observed: Any) -> dict[str, Any]:
            return {'check': name, 'ok': bool(ok), 'observed': observed}

        tags_ok = all(
            re.fullmatch(r'codec:([a-z0-9]+),direction:(inbound|outbound)', k) is not None
            and k.split(',')[0][6:] in ALLOWED_CODEC_TAGS
            for k in rec['agent_observed']['tag_sets']
        )
        tolerance = max(10, int(0.005 * max(1, rec['expected_events_effective'])))
        interruptions_ok = all(i.get('ok') for i in self.interruptions) and len(self.interruptions) == len(self.plan())
        return [
            row('ran_full_duration', completed and elapsed >= self.duration, round(elapsed)),
            row('no_abort', self.abort_reason is None, self.abort_reason),
            row('all_planned_interruptions_executed_and_recovered', interruptions_ok,
                '{}/{}'.format(sum(1 for i in self.interruptions if i.get('ok')), len(self.plan()))),
            row('listener_counter_conservation', rec['conservation_residual'] == 0, rec['conservation_residual']),
            row('unexplained_missing_events_within_tolerance',
                -tolerance <= rec['unexplained_missing_events'] <= tolerance,
                {'unexplained': rec['unexplained_missing_events'], 'tolerance': tolerance}),
            row('no_queue_drops_in_main_listener_outside_overflow_scenario', rec['dropped_queue_full'] == 0,
                rec['dropped_queue_full']),
            row('no_parse_errors', rec['parse_errors'] == 0, rec['parse_errors']),
            row('no_unexpected_invalid_outcomes', rec['rejected_samples_observed'] == 0, rec['rejected_samples_observed']),
            row('agent_loss_attributable_to_agent_restart_windows',
                0 <= rec['lost_between_listener_and_agent'] and rec['lost_not_attributable_to_agent_windows'] <= max(10, int(0.02 * rec['submitted_dogstatsd_mos'])),
                {'lost': rec['lost_between_listener_and_agent'], 'in_windows': rec['submitted_during_agent_down_windows']}),
            row('tag_cardinality_bounded', tags_ok and len(rec['agent_observed']['tag_sets']) <= 8,
                len(rec['agent_observed']['tag_sets'])),
            row('no_sensitive_data_in_evidence', not self.leak_findings, self.leak_findings),
        ]

    def finalize(self, status: str, elapsed: float) -> dict[str, Any]:
        assert self.gen is not None and self.observer is not None
        rec = self.reconcile(final=True)
        self.leak_findings = self.leak_scan()
        lat = sorted(self.probe.latency_ms)

        def pct(p: float) -> float | None:
            return round(lat[min(len(lat) - 1, int(len(lat) * p))], 1) if lat else None

        counters_now = None
        try:
            counters_now = self.rig.dogstatsd_counters() if self.rig else None  # type: ignore[union-attr]
        except Exception:
            pass
        checks = self.checks(rec, status == 'completed', elapsed)
        summary = {
            'status': status,
            'completed_full_duration': status == 'completed',
            'abort_reason': self.abort_reason,
            'run_id': self.run_id,
            'smoke_run': self.smoke,
            'generation_start': iso(self.t0), 'generation_end': iso(self.t0 + elapsed),
            'planned_generation_seconds': self.duration, 'actual_generation_seconds': round(elapsed, 1),
            'finished': iso(now()),
            'reconciliation': rec,
            'processing_latency_ms': {'samples': len(lat), 'p50': pct(0.5), 'p95': pct(0.95), 'p99': pct(0.99),
                                      'max': round(lat[-1], 1) if lat else None,
                                      'negative_values': sum(1 for x in lat if x < 0),
                                      'definition': 'FreeSWITCH event timestamp to end of listener processing '
                                                    '(includes queue wait and the local DogStatsD send; assumes '
                                                    'host and container clocks agree)'},  # fmt: skip
            'resources_max': self.max,
            'listener_incarnations': [
                {k: v for k, v in i.items() if k != 'final'} | {'final_counters': (i['final'] or {}).get('counters')}
                for i in self.incarnations],
            'interruptions': self.interruptions,
            'agent_dogstatsd_counters': {'periodic': self.agent_counters_log[-20:], 'final': counters_now},
            'warnings': self.warnings,
            'acceptance_checks': checks,
            'all_acceptance_checks_ok': all(c['ok'] for c in checks) if status == 'completed' else None,
            'limits': LIMITS,
            'notes': ['A passed check is a local observation, not production validation.',
                      'Events missed while the listener is disconnected are not replayed by FreeSWITCH.',
                      'Backend percentiles, merging and billing were not exercised (local sink only).'],
        }  # fmt: skip
        with self.gen.lock:
            gt = [[c['cat'], c['scenario'], round(c['started'] - self.t0, 1),
                   round(c['end'] - self.t0, 1) if c['end'] else None] for c in self.gen.calls.values()]
        write_json(self.dir / 'ground_truth_calls.json', {'fields': ['category', 'scenario', 'start_t', 'end_t'],
                                                           'calls': gt})  # fmt: skip
        write_json(self.dir / 'final_summary.json', summary)
        (self.dir / 'STATUS').write_text(status + '\n')
        self.persist_state(status)
        return summary


def check_only() -> int:
    problems = []
    for image in (FS_IMAGE, AGENT_IMAGE, 'python:3.13-slim'):
        if run(['docker', 'image', 'inspect', '-f', '{{.Id}}', image]).returncode != 0:
            problems.append('missing image ' + image)
    cfg = Config(password='x' * 8, username='{}@test.local'.format(EVENT_USER), metric_prefix=PREFIX)
    print('config ok:', repr(cfg))
    print('problems:', problems or 'none')
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--dir', required=True)
    parser.add_argument('--duration', type=float, default=14400.0)
    parser.add_argument('--smoke', action='store_true', help='compressed schedule for a short preflight run')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    if args.check_only:
        return check_only()
    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(directory / 'runner.log', maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(RedactingFormatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    (directory / 'runner.pid').write_text(str(os.getpid()) + '\n')
    soak = Soak(directory, args.duration, args.smoke)

    def stop(signum: int, _frame: object) -> None:
        soak.abort('terminated_by_signal_{}'.format(signum), 'interrupted')

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    code = soak.main()
    LOG.info('exit code %s', code)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
