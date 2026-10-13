# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Helpers for the RTP experiments that need a real Datadog Agent: lifecycle, DogStatsD sender and sink decoding.

Experiment support only. The Agent's intake URL points at a local sink container on a private Docker network, so
nothing leaves the machine and no real API key is used.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from typing import Any

AGENT_IMAGE = os.environ.get('FS_AGENT_IMAGE', 'datadog/agent:7.80.3')
PACKAGE_DIR = Path(__file__).resolve().parents[2] / 'datadog_checks' / 'freeswitch'
AGENT_PACKAGE_DIR = '/opt/datadog-agent/embedded/lib/python3.13/site-packages/datadog_checks/freeswitch'

SINK_SOURCE = (
    'import http.server,itertools\n'
    'n=itertools.count()\n'
    'class H(http.server.BaseHTTPRequestHandler):\n'
    '    def do_POST(self):\n'
    '        body=self.rfile.read(int(self.headers.get("Content-Length","0")))\n'
    '        open("/data/%06d.bin"%next(n),"wb").write(self.path.encode()+b"\\n"+body)\n'
    '        self.send_response(202); self.end_headers(); self.wfile.write(b"{}")\n'
    '    do_PUT=do_POST\n'
    '    def do_GET(self):\n'
    '        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")\n'
    '    def log_message(self,*a): pass\n'
    'http.server.ThreadingHTTPServer(("0.0.0.0",8080),H).serve_forever()\n'
)


class AgentRig:
    """A disposable Agent + intake sink. ``check`` optionally mounts this repository's check (polling the testbed)."""

    def __init__(self, network: str, check_password: str | None = None, statsd_host_port: int | None = None) -> None:
        tag = uuid.uuid4().hex[:6]
        self.network = network
        self.agent = 'fs-exp-agent-' + tag
        self.sink = 'fs-exp-sink-' + tag
        self.data = Path(tempfile.mkdtemp(prefix='fs-sink-'))
        self.conf = Path(tempfile.mkdtemp(prefix='fs-agentconf-'))
        self.check_password = check_password
        self.fixed_port = statsd_host_port  # a stable UDP port survives an Agent restart
        self.statsd_port = 0
        os.chmod(self.data, 0o777)

    def start(self) -> None:
        subprocess.run(
            ['docker', 'run', '-d', '--name', self.sink, '--network', self.network, '-v', '{}:/data'.format(self.data),
             'python:3.13-slim', 'python', '-c', SINK_SOURCE],
            check=True, capture_output=True,
        )  # fmt: skip
        command = [
            'docker', 'run', '-d', '--name', self.agent, '--network', self.network,
            '-p', '127.0.0.1:{}:8125/udp'.format(self.fixed_port or ''),
            '-e', 'DD_API_KEY=00000000000000000000000000000000', '-e', 'DD_HOSTNAME=exp',
            '-e', 'DD_DD_URL=http://{}:8080'.format(self.sink), '-e', 'DD_USE_V2_API_SERIES=false',
            '-e', 'DD_SERIALIZER_COMPRESSOR_KIND=zlib', '-e', 'DD_LOGS_ENABLED=false', '-e', 'DD_APM_ENABLED=false',
            '-e', 'DD_PROCESS_AGENT_ENABLED=false', '-e', 'DD_USE_DOGSTATSD=true',
            '-e', 'DD_DOGSTATSD_NON_LOCAL_TRAFFIC=true', '-e', 'DD_DOGSTATSD_PORT=8125',
        ]  # fmt: skip
        if self.check_password is not None:
            (self.conf / 'conf.yaml').write_text(
                'init_config:\ninstances:\n  - host: fs-testbed\n    port: 8021\n    username: esl_ro@test.local\n'
                '    password: {}\n    min_collection_interval: 15\n    tags:\n      - tier:exp\n'.format(
                    self.check_password
                )
            )
            os.chmod(self.conf, 0o755)
            os.chmod(self.conf / 'conf.yaml', 0o644)
            command += [
                '-v', '{}:{}:ro'.format(PACKAGE_DIR, AGENT_PACKAGE_DIR),
                '-v', '{}:/etc/datadog-agent/conf.d/freeswitch.d:ro'.format(self.conf),
            ]  # fmt: skip
        command.append(AGENT_IMAGE)
        subprocess.run(command, check=True, capture_output=True)
        out = subprocess.run(['docker', 'port', self.agent, '8125/udp'], capture_output=True, text=True).stdout
        self.statsd_port = int(out.strip().splitlines()[0].rsplit(':', 1)[1])
        deadline = time.time() + 120
        while time.time() < deadline:  # wait until DogStatsD listens (status shows the section)
            status = self.status()
            if 'DogStatsD' in status and 'Udp Packets' in status:
                return
            time.sleep(3)
        raise RuntimeError('agent did not become ready')

    def status(self) -> str:
        out = subprocess.run(['docker', 'exec', self.agent, 'agent', 'status'], capture_output=True, text=True)
        return re.sub(r'\x1b\[[0-9;]*m', '', out.stdout)

    def dogstatsd_counters(self) -> dict[str, int]:
        text = self.status()
        section = text[text.find('DogStatsD') :]
        values = {}
        keys = (
            'Event Packets',
            'Event Parse Errors',
            'Metric Packets',
            'Metric Parse Errors',
            'Udp Packets',
            'Udp Bytes',
            'Unterminated Metric Errors',
            'Service Check Packets',
            'Service Check Parse Errors',
        )
        for key in keys:
            match = re.search(r'{}: (\d+)'.format(key), section)
            if match:
                values[key] = int(match.group(1))
        return values

    def refresh_port(self) -> None:
        """The published port may change when the Agent container is restarted."""
        out = subprocess.run(['docker', 'port', self.agent, '8125/udp'], capture_output=True, text=True).stdout
        self.statsd_port = int(out.strip().splitlines()[0].rsplit(':', 1)[1])

    def sender(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(('127.0.0.1', self.statsd_port))
        return sock

    def payloads(self) -> list[tuple[str, bytes]]:
        out = []
        for path in sorted(self.data.glob('*.bin')):
            head, _, body = path.read_bytes().partition(b'\n')
            out.append((head.decode(), body))
        return out

    def stop(self, keep_sink_dir: Path | None = None) -> None:
        if keep_sink_dir is not None:
            shutil.copytree(self.data, keep_sink_dir, dirs_exist_ok=True)
        subprocess.run(['docker', 'rm', '-f', self.agent, self.sink], capture_output=True)
        shutil.rmtree(self.data, ignore_errors=True)
        shutil.rmtree(self.conf, ignore_errors=True)


# -- sink decoding ------------------------------------------------------------------------------------------------


def _maybe_decompress(body: bytes) -> bytes:
    try:
        return zlib.decompress(body)
    except zlib.error:
        return body


def series_points(payloads: list[tuple[str, bytes]], prefix: str) -> list[dict]:
    """Series (gauge, count, rate) points delivered through ``/api/v1/series``."""
    points = []
    for path, body in payloads:
        if 'v1/series' not in path:
            continue
        try:
            data = json.loads(_maybe_decompress(body))
        except ValueError:
            continue
        for series in data.get('series', []):
            if series['metric'].startswith(prefix):
                for ts, value in series['points']:
                    points.append({'metric': series['metric'], 'ts': int(ts), 'value': value,
                                   'type': series.get('type'), 'tags': sorted(series.get('tags') or []),
                                   'interval': series.get('interval')})  # fmt: skip
    return sorted(points, key=lambda p: (p['ts'], p['metric']))


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _fields(buf: bytes) -> list[tuple[int, int, Any]]:
    pos = 0
    out: list[tuple[int, int, Any]] = []
    value: Any
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(buf, pos)
        elif wire == 1:
            value = struct.unpack('<d', buf[pos : pos + 8])[0]
            pos += 8
        elif wire == 2:
            length, pos = _varint(buf, pos)
            value = buf[pos : pos + length]
            pos += length
        elif wire == 5:
            value = struct.unpack('<f', buf[pos : pos + 4])[0]
            pos += 4
        else:
            raise ValueError('unsupported wire type')
        out.append((number, wire, value))
    return out


def sketch_points(payloads: list[tuple[str, bytes]], prefix: str) -> list[dict]:
    """Agent-side distribution sketches delivered to ``/api/beta/sketches`` (protobuf ``SketchPayload``).

    Decodes metric, tags and, per interval, count, min, max, average and sum. Quantiles are NOT derived here.
    """
    points = []
    for path, body in payloads:
        if 'sketches' not in path:
            continue
        raw = _maybe_decompress(body)
        for number, _, value in _fields(raw):
            if number != 1:  # repeated Sketch sketches = 1
                continue
            metric, tags, dog = '', [], []
            for n, _, v in _fields(value):
                if n == 1:
                    metric = v.decode()
                elif n == 4:
                    tags.append(v.decode())
                elif n == 7:
                    dog.append(v)
            if not metric.startswith(prefix):
                continue
            for item in dog:
                f = {n: v for n, _, v in _fields(item)}
                point = {'metric': metric, 'tags': sorted(tags), 'ts': int(f.get(1, 0)), 'count': int(f.get(2, 0))}
                point.update({'min': f.get(3), 'max': f.get(4), 'avg': f.get(5), 'sum': f.get(6)})
                points.append(point)
    return sorted(points, key=lambda p: (p['ts'], p['metric']))
