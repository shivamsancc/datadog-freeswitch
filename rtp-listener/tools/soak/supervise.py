#!/usr/bin/env python3
# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Independent watchdog for one soak run. Ties to exactly one runner PID and one experiment directory.

It restarts nothing. It (1) writes an independent heartbeat, (2) turns a vanished or stalled runner into a recorded
failure with a ``final_summary.json`` if the runner did not write one, and (3) removes only the containers and network
named in the run's ``state.json`` after capturing their state. It exits when the runner is gone.

Usage: python supervise.py --dir <experiment dir> --pid <runner pid> --start "<ps lstart of the runner>" --deadline <epoch>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

STALL_SECONDS = 240
MIN_DISK_GB = 2.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec='seconds')


def sh(cmd: list[str], timeout: float = 60) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ''


def write_json(path: Path, data: object) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def alive(pid: int, lstart: str) -> bool:
    out = sh(['ps', '-o', 'lstart=', '-p', str(pid)]).strip()
    return bool(out) and (not lstart or out == lstart)


def redact(text: str) -> str:
    text = re.sub(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', '<uuid>', text)
    out = []
    for line in text.splitlines():
        if re.search(r'passw|secret|auth', line, re.I):
            out.append('<line withheld>')
        else:
            out.append(re.sub(r'\b\d{4,}\b', '<n>', re.sub(r'sofia/\S+', 'sofia/<redacted>', line)))
    return '\n'.join(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--dir', required=True)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--start', default='')
    parser.add_argument('--deadline', type=float, required=True)
    args = parser.parse_args()
    d = Path(args.dir)
    note = {'killed_runner': None}
    while True:
        t = time.time()
        runner_alive = alive(args.pid, args.start)
        health = {}
        try:
            health = json.loads((d / 'health.json').read_text())
        except (OSError, ValueError):
            pass
        age = t - health.get('heartbeat_epoch', t) if health else None
        write_json(d / 'watchdog_heartbeat.json', {
            'heartbeat_epoch': t, 'heartbeat': iso(t), 'watchdog_pid': os.getpid(), 'runner_pid': args.pid,
            'runner_alive': runner_alive, 'runner_heartbeat_age_seconds': round(age, 1) if age is not None else None,
            'runner_phase': health.get('phase'), 'elapsed_seconds': health.get('elapsed_seconds'),
            'deadline': iso(args.deadline), 'status_file': (d / 'STATUS').read_text().strip() if (d / 'STATUS').exists() else None,
        })  # fmt: skip
        if not runner_alive:
            break
        problem = None
        if age is not None and age > STALL_SECONDS:
            problem = 'runner_heartbeat_stalled_{}s'.format(int(age))
        elif t > args.deadline:
            problem = 'deadline_exceeded'
        elif shutil.disk_usage(d).free / 1e9 < MIN_DISK_GB:
            problem = 'host_disk_critically_low'
        if problem and note['killed_runner'] is None:
            note['killed_runner'] = {'reason': problem, 'at': iso(t)}
            (d / 'WATCHDOG_STOP_REQUESTED').write_text(problem + '\n')
            try:
                os.kill(args.pid, signal.SIGTERM)
            except OSError:
                pass
            kill_after = t + 120
        if note['killed_runner'] and t > kill_after and runner_alive:  # type: ignore[possibly-undefined]
            try:
                os.kill(args.pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(15)

    # The runner is gone. If it did not leave a final summary, record what happened without inventing results.
    time.sleep(3)
    summary_path = d / 'final_summary.json'
    state = {}
    try:
        state = json.loads((d / 'state.json').read_text())
    except (OSError, ValueError):
        pass
    if not summary_path.exists():
        cap = d / 'failure_capture'
        cap.mkdir(exist_ok=True)
        for name in (state.get('containers') or {}).values():
            if name:
                info = sh(['docker', 'inspect', '-f',
                           '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} finished={{.State.FinishedAt}}',
                           name])  # fmt: skip
                logs = sh(['docker', 'logs', '--tail', '150', name], 30)
                (cap / (name + '.txt')).write_text(redact(info + '\n' + logs))
        last = None
        try:
            lines = (d / 'progress.jsonl').read_text().strip().splitlines()
            last = json.loads(lines[-1]) if lines else None
        except (OSError, ValueError):
            pass
        write_json(summary_path, {
            'status': 'failed', 'completed_full_duration': False,
            'abort_reason': 'runner_process_vanished_without_final_summary',
            'watchdog_note': note['killed_runner'], 'finished': iso(time.time()),
            'last_progress_record': last, 'run_id': state.get('run_id'),
        })  # fmt: skip
        (d / 'STATUS').write_text('failed\n')
    # Remove only what this run created (names come from this run's own state file).
    names = [n for n in (state.get('containers') or {}).values() if n]
    if names:
        sh(['docker', 'rm', '-f', *names], 90)
    if state.get('network'):
        sh(['docker', 'network', 'rm', state['network']], 30)
    write_json(d / 'watchdog_final.json', {'ended': iso(time.time()), 'runner_pid': args.pid,
                                           'removed_containers': names, 'note': note})  # fmt: skip
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
