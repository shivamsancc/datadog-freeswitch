# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Experiment (not a test): what does a real Datadog Agent make of ``freeswitch.sessions.created`` across a restart?

Starts a disposable FreeSWITCH and the stock Agent image (this check mounted, running on its own schedule) on a private
Docker network. The Agent's intake URL is a local sink container on that network, so nothing leaves the machine and no
real API key is used. While calls and one FreeSWITCH restart are played out, the sink records what the Agent's
forwarder sends. Writes ``<out>/agent_points.json`` (every ``freeswitch.*`` point as the Agent serialized it) and
``<out>/timeline.json`` (raw ``status`` values with timestamps).

Usage: python -m tests.runtime.restart_experiment <out_dir>   (from the integration directory; needs Docker)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .testbed import Testbed

AGENT_IMAGE = os.environ.get('FS_AGENT_IMAGE', 'datadog/agent:7.80.3')
AGENT_PACKAGE_DIR = '/opt/datadog-agent/embedded/lib/python3.13/site-packages/datadog_checks/freeswitch'


def raw_created(bed: Testbed) -> int | None:
    try:
        match = re.search(r'^(\d+) session\(s\) since startup', bed.raw('status', timeout=3), re.M)
    except (OSError, RuntimeError):
        return None
    return int(match.group(1)) if match else None


SINK = (
    'import http.server,itertools,sys\n'
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


def decode_sink(directory: Path) -> list[dict]:
    """Series points of ``freeswitch.*`` that the Agent's forwarder delivered to the local sink (v1 JSON API)."""
    import zlib

    points: list[dict] = []
    for path in sorted(directory.glob('*.bin')):
        head, _, body = path.read_bytes().partition(b'\n')
        if b'series' not in head:
            continue
        try:
            payload = json.loads(zlib.decompress(body))
        except (zlib.error, ValueError):
            try:
                payload = json.loads(body)
            except ValueError:
                continue  # not JSON (for example protobuf): kept raw in the output directory
        for series in payload.get('series', []):
            if series['metric'].startswith('freeswitch.'):
                for ts, value in series['points']:
                    points.append(
                        {'ts': ts, 'metric': series['metric'], 'value': value, 'type': series.get('type'),
                         'interval': series.get('interval')}
                    )  # fmt: skip
    return points


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    net = 'fs-exp-' + uuid.uuid4().hex[:6]
    agent = 'fs-exp-agent-' + uuid.uuid4().hex[:6]
    sink = 'fs-exp-sink-' + uuid.uuid4().hex[:6]
    conf = Path(tempfile.mkdtemp(prefix='fs-agentconf-'))
    data = Path(tempfile.mkdtemp(prefix='fs-sink-'))
    os.chmod(data, 0o777)
    subprocess.run(['docker', 'network', 'create', net], check=True, capture_output=True)
    timeline: list[dict] = []
    t0 = time.time()

    def mark(event: str, bed: Testbed) -> None:
        timeline.append(
            {'t': round(time.time() - t0, 1), 'wall': int(time.time()), 'event': event,
             'raw_since_startup': raw_created(bed)}
        )  # fmt: skip
        print(timeline[-1], flush=True)

    try:
        subprocess.run(
            ['docker', 'run', '-d', '--name', sink, '--network', net, '-v', '{}:/data'.format(data),
             'python:3.13-slim', 'python', '-c', SINK],
            check=True, capture_output=True,
        )  # fmt: skip
        with Testbed(network=net) as bed:
            (conf / 'conf.yaml').write_text(
                'init_config:\ninstances:\n  - host: fs-testbed\n    port: 8021\n    username: esl_ro@test.local\n'
                '    password: {}\n    min_collection_interval: 5\n    tags:\n      - tier:exp\n'.format(
                    bed.restricted_password
                )
            )
            os.chmod(conf, 0o755)
            os.chmod(conf / 'conf.yaml', 0o644)
            package = Path(__file__).resolve().parents[2] / 'datadog_checks' / 'freeswitch'
            subprocess.run(
                [
                    'docker', 'run', '-d', '--name', agent, '--network', net,
                    '-e', 'DD_API_KEY=00000000000000000000000000000000', '-e', 'DD_HOSTNAME=exp',
                    '-e', 'DD_DD_URL=http://{}:8080'.format(sink), '-e', 'DD_USE_V2_API_SERIES=false',
                    '-e', 'DD_SERIALIZER_COMPRESSOR_KIND=zlib',
                    '-e', 'DD_LOGS_ENABLED=false', '-e', 'DD_APM_ENABLED=false',
                    '-e', 'DD_PROCESS_AGENT_ENABLED=false', '-e', 'DD_USE_DOGSTATSD=false',
                    '-v', '{}:{}:ro'.format(package, AGENT_PACKAGE_DIR),
                    '-v', '{}:/etc/datadog-agent/conf.d/freeswitch.d:ro'.format(conf),
                    AGENT_IMAGE,
                ],
                check=True,
                capture_output=True,
            )  # fmt: skip
            time.sleep(45)  # the Agent must be up and have taken its baseline sample
            t0 = time.time()
            mark('start (idle)', bed)
            time.sleep(15)
            for _ in range(2):
                bed.raw('originate sofia/gateway/loopreg/5000 &echo')
            time.sleep(1)
            mark('after 2 calls (4 sessions)', bed)
            time.sleep(20)
            mark('before restart', bed)
            bed.restart()
            mark('after restart (new process)', bed)
            deadline = time.time() + 60
            while time.time() < deadline and 'UP' not in re.findall(
                r'^Status\s+(\w+)', bed.raw('sofia status gateway loopreg'), re.M
            ):
                time.sleep(1)
            time.sleep(15)
            mark('idle after restart', bed)
            bed.raw('originate sofia/gateway/loopreg/5000 &echo')
            time.sleep(1)
            mark('after 1 call (2 sessions)', bed)
            time.sleep(20)
            for _ in range(3):
                bed.raw('originate sofia/gateway/loopreg/5000 &echo')
            time.sleep(1)
            mark('after 3 more calls (6 sessions)', bed)
            time.sleep(10)
            mark('before second restart', bed)
            # Second restart: create more sessions than the previous process had (8) before the Agent's next sample.
            # Failed originates through the unreachable gateway each count one session.
            bed.restart()
            for _ in range(12):
                bed.raw('originate sofia/gateway/deadreg/1 &park')
            mark('after second restart and 12 failed originates', bed)
            time.sleep(25)  # let the forwarder flush
            mark('end', bed)
            shutil.copytree(data, out / 'sink', dirs_exist_ok=True)
            points = decode_sink(data)
            (out / 'agent_points.json').write_text(json.dumps(points, indent=1))
    finally:
        subprocess.run(['docker', 'rm', '-f', agent, sink], capture_output=True)
        subprocess.run(['docker', 'network', 'rm', net], capture_output=True)
        shutil.rmtree(conf, ignore_errors=True)
        shutil.rmtree(data, ignore_errors=True)
    (out / 'timeline.json').write_text(json.dumps(timeline, indent=1))


if __name__ == '__main__':
    main(Path(sys.argv[1]))
