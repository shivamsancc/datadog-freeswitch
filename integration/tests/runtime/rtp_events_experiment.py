# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Research experiment (not a test, not part of the check): RTP statistics in ``CHANNEL_HANGUP_COMPLETE``.

Disposable FreeSWITCH only, real SIP and RTP over the container's loopback. The event listener below uses the testbed's
full-control password and exists only to observe what FreeSWITCH publishes; a collector would use a dedicated account.
Output: sanitized JSON (no UUIDs, addresses or SDP; extensions are the fixed test extensions).

Usage: python -m tests.runtime.rtp_events_experiment <out.json>   (from the integration directory; needs Docker)
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
import urllib.parse

from .testbed import Testbed

FIELD = re.compile(
    r'^variable_rtp_audio_(?:in|out)_|^variable_rtp_audio_rtcp_|^Hangup-Cause$|^Call-Direction$|^Channel-Name$|'
    r'^variable_billsec$|^variable_duration$|^variable_read_codec$|^Event-Date-Timestamp$|^Unique-ID$|'
    r'^Channel-Call-UUID$|^variable_rtp_use_codec_name$'
)


class Listener:
    def __init__(self, bed: Testbed, events: str = 'CHANNEL_HANGUP_COMPLETE') -> None:
        self.sock = socket.create_connection(('127.0.0.1', bed.port), timeout=30)
        self.sock.settimeout(1)
        self.buf = b''
        self._message()
        self.sock.sendall('auth {}\n\n'.format(bed.password).encode())
        self._message()
        self.sock.sendall('event plain {}\n\n'.format(events).encode())
        self._message()

    def _message(self) -> tuple[dict[str, str], str]:
        while b'\n\n' not in self.buf:
            self.buf += self.sock.recv(65536)
        head, self.buf = self.buf.split(b'\n\n', 1)
        headers = dict(line.split(': ', 1) for line in head.decode().split('\n') if ': ' in line)
        length = int(headers.get('Content-Length', 0))
        while len(self.buf) < length:
            self.buf += self.sock.recv(65536)
        body, self.buf = self.buf[:length], self.buf[length:]
        return headers, body.decode('utf-8', 'replace')

    def collect(self, seconds: float) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        end = time.time() + seconds
        while time.time() < end:
            try:
                headers, body = self._message()
            except TimeoutError:
                continue
            if headers.get('Content-Type') != 'text/event-plain':
                continue
            event: dict[str, str] = {}
            for line in body.split('\n'):
                if ': ' in line:
                    key, value = line.split(': ', 1)
                    event[key] = urllib.parse.unquote(value)
            event['_received'] = repr(time.time())
            out.append(event)
        return out


def ready(bed: Testbed) -> None:
    for _ in range(90):
        if bed.raw('show registrations count').strip() == '1 total.' and re.search(
            r'^Status\s+UP', bed.raw('sofia status gateway loopreg'), re.M
        ):
            return
        time.sleep(1)
    raise RuntimeError('testbed not ready')


def sanitize(events: list[dict[str, str]]) -> list[dict[str, object]]:
    ids: dict[str, int] = {}
    out = []
    for event in events:
        uid = event.get('Unique-ID', '')
        ordinal = ids.setdefault(uid, len(ids))
        row: dict[str, object] = {'ordinal': ordinal}
        for key, value in event.items():
            if key in ('Unique-ID', 'Channel-Call-UUID', '_received'):
                continue
            if FIELD.search(key):
                row[key.replace('variable_rtp_audio_', '').replace('variable_', '')] = value
        timestamp = event.get('Event-Date-Timestamp')
        if timestamp and event.get('_received'):
            row['delivery_lag_ms'] = round(float(event['_received']) * 1000 - int(timestamp) / 1000, 1)
        out.append(row)
    return out


def run_calls(bed: Testbed, listener: Listener, count: int, app: str, hold: float, dest: str = '5000') -> list[dict]:
    uuids = []
    for _ in range(count):
        reply = bed.raw('originate sofia/gateway/loopreg/{} {}'.format(dest, app))
        if reply.startswith('+OK'):
            uuids.append(reply.split()[1])
    time.sleep(hold)
    for uid in uuids:
        bed.raw('uuid_kill ' + uid)
    events = listener.collect(4)
    return sanitize(events)


def netem(bed: Testbed, rule: str | None) -> None:
    prefix = ['docker', 'exec', '-u', '0', bed.name]
    subprocess.run([*prefix, 'tc', 'qdisc', 'del', 'dev', 'lo', 'root'], capture_output=True)
    if rule:
        subprocess.run(
            [*prefix, 'tc', 'qdisc', 'add', 'dev', 'lo', 'root', 'netem', *rule.split()],
            check=True,
            capture_output=True,
        )


def main(out_path: str) -> None:
    results: dict[str, object] = {}
    with Testbed(cap_net_admin=True) as bed:
        ready(bed)
        listener = Listener(bed)
        tone = '&playback(tone_stream://L=200;%(1000,0,440))'
        for name, app, hold, count in [
            ('tone_3s_x5', tone, 3, 5),
            ('tone_10s_x5', tone, 10, 5),
            ('tone_20s_x3', tone, 20, 3),
            ('park_5s_control_x5', '&park', 5, 5),
            ('answer_then_hangup_0.3s_x5', '&park', 0.3, 5),
            ('concurrent_tone_8s_x10', tone, 8, 10),
        ]:
            results[name] = run_calls(bed, listener, count, app, hold)
            print(name, len(results[name]), flush=True)  # type: ignore[arg-type]
        # a call that never gets an answer: no media at all
        busy = bed.raw('originate sofia/gateway/loopreg/5002 &park')
        results['busy_486_originate_reply'] = busy.strip()
        results['busy_486_events'] = sanitize(listener.collect(3))
        # installing the traffic-control tool needs the container's package repositories (test container only)
        subprocess.run(
            [
                'docker',
                'exec',
                '-u',
                '0',
                bed.name,
                'sh',
                '-c',
                'apt-get update -qq && apt-get install -y -qq iproute2',
            ],
            capture_output=True,
        )
        for name, rule in [('loss_10pct', 'loss 10%'), ('loss_30pct', 'loss 30%'), ('delay_jitter', 'delay 60ms 40ms')]:
            try:
                netem(bed, rule)
                results[name + '_tone_10s_x3'] = run_calls(bed, listener, 3, tone, 10)
                print(name, len(results[name + '_tone_10s_x3']), flush=True)  # type: ignore[arg-type]
            except subprocess.CalledProcessError as error:
                results[name] = 'netem unavailable: {}'.format(error.stderr[-200:] if error.stderr else error)
            finally:
                netem(bed, None)
        # delivery under a subscriber that disconnects mid-run: events published meanwhile are not replayed
        second = Listener(bed)
        reply = bed.raw('originate sofia/gateway/loopreg/5000 &playback(tone_stream://L=200;%(1000,0,440))')
        second.sock.close()  # the subscriber goes away while the call is up
        time.sleep(2)
        bed.raw('uuid_kill ' + reply.split()[1])
        time.sleep(1)
        third = Listener(bed)
        results['subscriber_reconnect_events_after_gap'] = len(third.collect(3))
        results['channels_after'] = bed.raw('show channels count').strip()
    with open(out_path, 'w') as handle:
        json.dump(results, handle, indent=1)


if __name__ == '__main__':
    main(sys.argv[1])
