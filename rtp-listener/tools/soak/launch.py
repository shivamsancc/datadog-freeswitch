#!/usr/bin/env python3
# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Launch one detached soak run (runner + keep-awake + watchdog) and verify it. Returns without waiting for the run.

    python launch.py [--duration 14400] [--smoke] [--dir <experiment dir>]

Detachment: every process is started in its own session (``start_new_session``), with stdin from /dev/null and all output
in files of the experiment directory, so it is re-parented to launchd and survives the end of the calling shell, Claude
Code or the terminal. ``caffeinate -i -m -s -w <runner pid>`` holds off idle sleep only while the runner exists.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PY = sys.executable
LOCK = ROOT / 'results' / 'soak4h.lock'


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec='seconds')


def sh(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout


def lstart(pid: int) -> str:
    return sh(['ps', '-o', 'lstart=', '-p', str(pid)]).strip()


def running_runners() -> list[str]:
    out = sh(['ps', '-axo', 'pid=,command='])
    return [line.strip() for line in out.splitlines() if 'soak_runner.py' in line and '--check-only' not in line
            and 'launch.py' not in line]  # fmt: skip


def soak_containers() -> list[str]:
    return [n for n in sh(['docker', 'ps', '-a', '--filter', 'label=datadog-freeswitch.soak', '--format', '{{.Names}}']).split()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--duration', type=float, default=14400.0)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--dir')
    args = parser.parse_args()

    # duplicate guards: lock file with a live PID, any soak_runner process, any labelled soak container
    LOCK.parent.mkdir(exist_ok=True)
    if LOCK.exists():
        try:
            held = json.loads(LOCK.read_text())
            if lstart(held['pid']) == held.get('lstart'):
                print('REFUSED: a soak run is already active (pid {}, dir {})'.format(held['pid'], held['dir']))
                return 1
        except (OSError, ValueError, KeyError):
            pass
    others = running_runners()
    if others:
        print('REFUSED: soak_runner process(es) already running:', others)
        return 1
    leftovers = soak_containers()
    if leftovers:
        print('REFUSED: containers from an earlier soak run still exist:', leftovers)
        print('Inspect them, then remove with: docker rm -f', ' '.join(leftovers))
        return 1

    stamp = time.strftime('%Y%m%dT%H%M%S')
    exp = Path(args.dir) if args.dir else ROOT / 'results' / ('soak4h_{}{}'.format('smoke_' if args.smoke else '', stamp))
    exp.mkdir(parents=True, exist_ok=False)
    runner_cmd = [PY, '-u', str(HERE / 'soak_runner.py'), '--dir', str(exp), '--duration', str(args.duration)]
    if args.smoke:
        runner_cmd.append('--smoke')
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin'),
           'HOME': os.environ.get('HOME', ''), 'PYTHONDONTWRITEBYTECODE': '1', 'LANG': 'en_US.UTF-8'}  # fmt: skip
    start = time.time()
    out = open(exp / 'runner.stdout', 'ab', buffering=0)
    runner = subprocess.Popen(runner_cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                              start_new_session=True, env=env, cwd=str(exp))  # fmt: skip
    rstart = ''
    for _ in range(20):
        rstart = lstart(runner.pid)
        if rstart:
            break
        time.sleep(0.2)
    LOCK.write_text(json.dumps({'pid': runner.pid, 'lstart': rstart, 'dir': str(exp)}))

    caff_cmd = ['/usr/bin/caffeinate', '-i', '-m', '-s', '-w', str(runner.pid)]
    caff = subprocess.Popen(caff_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)  # fmt: skip
    # generation starts after setup (a few minutes); the watchdog deadline leaves a generous margin
    deadline = start + args.duration + 3600
    wd_cmd = [PY, '-u', str(HERE / 'supervise.py'), '--dir', str(exp), '--pid', str(runner.pid), '--start', rstart,
              '--deadline', str(deadline)]  # fmt: skip
    wd = subprocess.Popen(wd_cmd, stdin=subprocess.DEVNULL, stdout=open(exp / 'watchdog.stdout', 'ab'),
                          stderr=subprocess.STDOUT, start_new_session=True, env=env, cwd=str(exp))  # fmt: skip

    launch = {
        'launched': iso(start), 'experiment_dir': str(exp), 'duration_seconds': args.duration, 'smoke': args.smoke,
        'runner': {'pid': runner.pid, 'lstart': rstart, 'command': ' '.join(runner_cmd)},
        'keep_awake': {'pid': caff.pid, 'command': ' '.join(caff_cmd)},
        'watchdog': {'pid': wd.pid, 'command': ' '.join(wd_cmd)},
        'expected_generation_end_approx': iso(start + 300 + args.duration),
        'logs': {'runner_log': str(exp / 'runner.log'), 'runner_stdout': str(exp / 'runner.stdout'),
                 'health': str(exp / 'health.json'), 'progress': str(exp / 'progress.jsonl'),
                 'reconciliation_live': str(exp / 'reconciliation_live.json'),
                 'interruptions': str(exp / 'interruptions.json'),
                 'watchdog_heartbeat': str(exp / 'watchdog_heartbeat.json'),
                 'final_summary': str(exp / 'final_summary.json'), 'status': str(exp / 'STATUS')},
    }  # fmt: skip
    (exp / 'launch.json').write_text(json.dumps(launch, indent=1))

    # verify: first progress record, process relationships and independence from this shell
    ok = False
    for _ in range(240):
        if runner.poll() is not None:
            break
        if (exp / 'progress.jsonl').exists() and (exp / 'health.json').exists():
            ok = True
            break
        time.sleep(2)
    info = sh(['ps', '-o', 'pid,ppid,sess,pgid,tty,stat,etime,command', '-p', '{},{},{}'.format(runner.pid, caff.pid, wd.pid)])
    me = sh(['ps', '-o', 'pid,ppid,sess,pgid,tty', '-p', str(os.getpid())])
    print(json.dumps(launch, indent=1))
    print('--- processes\n' + info)
    print('--- launcher\n' + me)
    if not ok:
        print('LAUNCH NOT VERIFIED: runner exited or produced no progress; see', exp / 'runner.log', exp / 'runner.stdout')
        return 2
    state = json.loads((exp / 'state.json').read_text())
    gen_end = state.get('expected_generation_end')
    print('VERIFIED: runner alive, progress and heartbeat files present. expected generation end:', gen_end)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
