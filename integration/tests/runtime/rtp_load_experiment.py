# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Research experiment (not a test, not a listener): hangup-event delivery under load, codecs, SRTP and failures.

Disposable FreeSWITCH only. The observer below uses the testbed's full-control password (the event-only account was
verified separately); it exists to measure what FreeSWITCH delivers, and it keeps only a handful of numeric fields per
event plus ordinals: no UUIDs, caller IDs, addresses or SDP reach the output.

Usage: python -m tests.runtime.rtp_load_experiment <out.json> [phases comma separated]
Phases: load, codecs, resilience
"""

from __future__ import annotations

import json
import math
import re
import resource
import socket
import statistics
import subprocess
import sys
import time
import urllib.parse
import uuid

from .testbed import Testbed

TONE = '&playback(tone_stream://L=400;%(1000,0,440))'
KEEP = (
    'in_packet_count', 'out_packet_count', 'in_mos', 'in_quality_percentage', 'in_flaw_total',
    'in_media_packet_count',
)  # fmt: skip


class Observer:
    """ESL event subscriber. ``read`` can be paused to model a slow consumer."""

    def __init__(
        self,
        bed: Testbed,
        user: str | None = None,
        password: str | None = None,
        events: str = 'CHANNEL_HANGUP_COMPLETE',
    ) -> None:
        self.sock = socket.create_connection(('127.0.0.1', bed.port), timeout=30)
        self.sock.settimeout(1)
        self.buf = b''
        self.disconnected = False
        self.reply = ''
        self._message()
        auth = 'auth {}'.format(bed.password) if user is None else 'userauth {}:{}'.format(user, password)
        self.sock.sendall((auth + '\n\n').encode())
        headers, _ = self._message()
        self.reply = headers.get('Reply-Text', '')
        if self.reply.startswith('+OK'):
            self.sock.sendall('event plain {}\n\n'.format(events).encode())
            headers, _ = self._message()
            self.reply = headers.get('Reply-Text', '')

    def _message(self) -> tuple[dict[str, str], str]:
        while b'\n\n' not in self.buf:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise ConnectionError('closed')
            self.buf += chunk
        head, self.buf = self.buf.split(b'\n\n', 1)
        headers = dict(line.split(': ', 1) for line in head.decode().split('\n') if ': ' in line)
        length = int(headers.get('Content-Length', 0))
        while len(self.buf) < length:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise ConnectionError('closed')
            self.buf += chunk
        body, self.buf = self.buf[:length], self.buf[length:]
        return headers, body.decode('utf-8', 'replace')

    def read(self, seconds: float) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        end = time.time() + seconds
        while time.time() < end:
            try:
                headers, body = self._message()
            except TimeoutError:
                continue
            except (ConnectionError, OSError):
                self.disconnected = True
                break
            if headers.get('Content-Type') == 'text/disconnect-notice':
                self.disconnected = True
                break
            if headers.get('Content-Type') != 'text/event-plain':
                continue
            event: dict[str, str] = {}
            for line in body.split('\n'):
                if ': ' in line:
                    key, value = line.split(': ', 1)
                    event[key] = urllib.parse.unquote(value)
            event['_recv'] = repr(time.time())
            event['_bytes'] = str(len(body))
            out.append(event)
        return out

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def ready(bed: Testbed) -> None:
    for _ in range(120):
        if bed.raw('show registrations count').strip() == '1 total.' and re.search(
            r'^Status\s+UP', bed.raw('sofia status gateway loopreg'), re.M
        ):
            return
        time.sleep(1)
    raise RuntimeError('testbed not ready')


def finite(value: str | None) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def summarize(events: list[dict[str, str]]) -> dict[str, object]:
    ids: dict[str, int] = {}
    mos, quality, lag, size = [], [], [], []
    categories = {'finite_mos': 0, 'nan_mos': 0, 'missing_fields': 0}
    for event in events:
        uid = event.get('Unique-ID', '')
        ids[uid] = ids.get(uid, 0) + 1
        raw = event.get('variable_rtp_audio_in_mos')
        if raw is None:
            categories['missing_fields'] += 1
        elif finite(raw) is None:
            categories['nan_mos'] += 1
        else:
            categories['finite_mos'] += 1
            mos.append(float(raw))
            q = finite(event.get('variable_rtp_audio_in_quality_percentage'))
            if q is not None:
                quality.append(q)
        timestamp = event.get('Event-Date-Timestamp')
        if timestamp:
            lag.append(float(event['_recv']) * 1000 - int(timestamp) / 1000)
        size.append(int(event.get('_bytes', 0)))

    def stats(values: list[float]) -> dict[str, float] | None:
        if not values:
            return None
        return {
            'min': round(min(values), 2),
            'median': round(statistics.median(values), 2),
            'max': round(max(values), 2),
        }

    return {
        'events': len(events),
        'unique_event_ids': len(ids),
        'duplicate_event_ids': sum(1 for n in ids.values() if n > 1),
        **categories,
        'mos': stats(mos),
        'quality_percentage': stats(quality),
        'delivery_lag_ms': stats(lag),
        'event_bytes': stats([float(x) for x in size]),
    }


def container_stats(bed: Testbed) -> dict[str, str]:
    out = subprocess.run(
        ['docker', 'stats', '--no-stream', '--format', '{{.CPUPerc}}|{{.MemUsage}}|{{.PIDs}}', bed.name],
        capture_output=True, text=True,
    )  # fmt: skip
    cpu, mem, pids = (out.stdout.strip().split('|') + ['', '', ''])[:3]
    return {'cpu': cpu, 'memory': mem, 'pids': pids}


def udp_datagrams(bed: Testbed) -> int:
    text = subprocess.run(['docker', 'exec', bed.name, 'cat', '/proc/net/snmp'], capture_output=True, text=True).stdout
    lines = [line for line in text.split('\n') if line.startswith('Udp:')]
    return int(dict(zip(lines[0].split()[1:], lines[1].split()[1:]))['InDatagrams'])


def start_calls(
    bed: Testbed, count: int, tag: str, extra: str = '', app: str = TONE, dest: str = '5000'
) -> list[tuple[str, str]]:
    """Originate ``count`` calls with a unique test id carried to both legs (SIP header on the far leg)."""
    started = []
    for index in range(count):
        test_id = '{}-{:03d}'.format(tag, index)
        uid = str(uuid.uuid4())
        variables = 'origination_uuid={},sip_h_X-Test-Id={}{}'.format(uid, test_id, (',' + extra) if extra else '')
        reply = bed.raw('originate {{{}}}sofia/gateway/loopreg/{} {}'.format(variables, dest, app))
        if reply.startswith('+OK'):
            started.append((uid, test_id))
    return started


def reconcile(started: list[tuple[str, str]], events: list[dict[str, str]]) -> dict[str, object]:
    expected = {uid for uid, _ in started}
    legs: dict[str, int] = dict.fromkeys(expected, 0)
    for event in events:
        uid = event.get('Unique-ID', '')
        if uid in legs:
            legs[uid] += 1
        else:
            # far leg: identified by the test id header it received
            test_id = event.get('variable_sip_h_X-Test-Id') or event.get('variable_sip_h_x-test-id')
            for a_uid, t_id in started:
                if t_id == test_id:
                    legs[a_uid] += 1
    far = sum(1 for event in events if event.get('Call-Direction') == 'inbound')
    return {
        'calls_started': len(started),
        'legs_expected': 2 * len(started),
        'events_received': len(events),
        'near_leg_events': sum(1 for event in events if event.get('Call-Direction') == 'outbound'),
        'far_leg_events': far,
        'calls_with_both_legs': sum(1 for n in legs.values() if n >= 2),
        'calls_missing_an_event': sum(1 for n in legs.values() if n < 2),
    }


def load_phase(bed: Testbed, observer: Observer) -> dict[str, object]:
    results: dict[str, object] = {}
    for calls in (13, 25, 50, 75):  # 26, 50, 100 and 150 concurrent legs
        udp_before = udp_datagrams(bed)
        t0 = time.time()
        started = start_calls(bed, calls, 'L{}'.format(calls))
        setup = time.time() - t0
        time.sleep(4)
        concurrent_legs = int(bed.raw('status').split('session(s) - peak')[0].split('\n')[-1].strip() or 0)
        stats_mid = container_stats(bed)
        time.sleep(4)
        udp_rate = (udp_datagrams(bed) - udp_before) / (time.time() - t0)
        before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        bed.raw('hupall NORMAL_CLEARING')
        events = observer.read(15)
        results['calls_{}'.format(calls)] = {
            **reconcile(started, events),
            'concurrent_sessions_at_peak': concurrent_legs,
            'setup_seconds': round(setup, 1),
            'call_duration_seconds': round(time.time() - t0 - 15, 1),
            **summarize(events),
            'container_during_calls': stats_mid,
            'udp_datagrams_per_second_in_container': round(udp_rate),
            'observer_maxrss_kb_delta': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before_rss,
        }
        print('load', calls, results['calls_{}'.format(calls)], flush=True)
        time.sleep(3)
    return results


def codec_phase(bed: Testbed, observer: Observer) -> dict[str, object]:
    results: dict[str, object] = {}
    scenarios = [
        ('PCMU_baseline', ''),
        ('PCMA', 'absolute_codec_string=PCMA'),
        ('SPEEX_16k_wideband', 'absolute_codec_string=speex@16000h@20i'),
        ('L16_8k', 'absolute_codec_string=L16'),
        ('SRTP_PCMU', 'rtp_secure_media=true'),
        ('SRTP_PCMA', 'rtp_secure_media=true,absolute_codec_string=PCMA'),
    ]
    for name, extra in scenarios:
        started = start_calls(bed, 5, name, extra)
        time.sleep(10)
        udp_before = udp_datagrams(bed)
        time.sleep(2)
        rate = (udp_datagrams(bed) - udp_before) / 2
        bed.raw('hupall NORMAL_CLEARING')
        events = observer.read(6)
        rows = []
        for event in events:
            rows.append({
                'direction': event.get('Call-Direction'),
                'codec': event.get('variable_rtp_use_codec_name'),
                'rate': event.get('variable_rtp_use_codec_rate'),
                'duration': event.get('variable_duration'),
                'in_packets': event.get('variable_rtp_audio_in_packet_count'),
                'out_packets': event.get('variable_rtp_audio_out_packet_count'),
                'mos': event.get('variable_rtp_audio_in_mos'),
                'quality': event.get('variable_rtp_audio_in_quality_percentage'),
                'srtp': event.get('variable_rtp_has_crypto'),
            })  # fmt: skip
        results[name] = {
            'reconcile': reconcile(started, events),
            'summary': summarize(events),
            'datagrams_per_second_in_container': round(rate),
            'legs': rows,
        }
        print('codec', name, results[name]['reconcile'], results[name]['summary']['mos'], flush=True)  # type: ignore[index]
        time.sleep(2)
    # transcoding: one leg PCMU, the bridged leg PCMA
    started = start_calls(
        bed, 3, 'transcode', '', '&bridge({absolute_codec_string=PCMA}sofia/gateway/loopreg/5003)', '5001'
    )
    time.sleep(10)
    bed.raw('hupall NORMAL_CLEARING')
    events = observer.read(6)
    results['transcoding_PCMU_to_PCMA'] = {
        'reconcile': reconcile(started, events),
        'legs': [{'channel_type': 'internal' if '/internal/' in e.get('Channel-Name', '') else 'edge',
                  'codec': e.get('variable_rtp_use_codec_name'),
                  'in_packets': e.get('variable_rtp_audio_in_packet_count'),
                  'mos': e.get('variable_rtp_audio_in_mos')} for e in events],
    }  # fmt: skip
    print('codec transcoding', results['transcoding_PCMU_to_PCMA']['reconcile'], flush=True)  # type: ignore[index]
    return results


def created_total(bed: Testbed) -> int:
    return int(re.search(r'^(\d+) session\(s\) since startup', bed.raw('status'), re.M).group(1))  # type: ignore[union-attr]


def resilience_phase(bed: Testbed) -> dict[str, object]:
    results: dict[str, object] = {}
    # 1. disconnect, calls end during the gap, reconnect
    observer = Observer(bed)
    started = start_calls(bed, 10, 'gap')
    time.sleep(3)
    observer.close()  # listener goes away while 10 calls (20 legs) are up
    time.sleep(1)
    bed.raw('hupall NORMAL_CLEARING')  # all hangups happen during the gap
    time.sleep(2)
    again = Observer(bed)
    during_after_reconnect = again.read(4)
    results['disconnect_gap'] = {
        'calls_ended_during_gap': len(started),
        'legs_lost_expected': 2 * len(started),
        'events_received_after_reconnect': len(during_after_reconnect),
    }
    # 2. calls that end *after* the reconnect are delivered again
    started = start_calls(bed, 5, 'after')
    time.sleep(2)
    bed.raw('hupall NORMAL_CLEARING')
    results['after_reconnect'] = reconcile(started, again.read(5))
    # 3. loss indicator: session counter (polled) versus events observed, with a gap in the middle
    base_created = created_total(bed)
    seen = 0
    started = start_calls(bed, 6, 'ind1')
    time.sleep(2)
    bed.raw('hupall NORMAL_CLEARING')
    seen += len(again.read(4))
    again.close()
    started = start_calls(bed, 4, 'ind2')
    time.sleep(2)
    bed.raw('hupall NORMAL_CLEARING')
    time.sleep(1)  # unobserved
    again = Observer(bed)
    started = start_calls(bed, 5, 'ind3')
    time.sleep(2)
    bed.raw('hupall NORMAL_CLEARING')
    seen += len(again.read(4))
    sessions = created_total(bed) - base_created
    results['loss_indicator'] = {
        'sessions_created_by_polling': sessions,
        'hangup_events_observed': seen,
        'difference': sessions - seen,
        'true_unobserved_legs': 8,
    }
    # 4. duplicate delivery: two simultaneous subscriptions (reconnect race)
    second = Observer(bed)
    started = start_calls(bed, 3, 'dup')
    time.sleep(2)
    bed.raw('hupall NORMAL_CLEARING')
    a, b = again.read(4), second.read(1)
    results['duplicate_subscriptions'] = {
        'legs': 6,
        'events_on_first': len(a),
        'events_on_second': len(b),
        'note': 'each subscription gets its own copy; dedupe by Unique-ID if two overlap',
    }
    second.close()
    # 5. burst: 150 legs hang up in one command
    started = start_calls(bed, 75, 'burst')
    time.sleep(3)
    t = time.time()
    bed.raw('hupall NORMAL_CLEARING')
    events = again.read(12)
    arrivals = sorted(float(e['_recv']) - t for e in events)
    results['burst_150_legs'] = {
        **reconcile(started, events),
        'first_arrival_s': round(arrivals[0], 2) if arrivals else None,
        'last_arrival_s': round(arrivals[-1], 2) if arrivals else None,
        'total_event_bytes': sum(int(e['_bytes']) for e in events),
    }
    print('burst', results['burst_150_legs'], flush=True)
    # 6. slow consumer: do not read while 4 rounds of 50 legs hang up (about 1.8 MB of events), then read
    expected = 0
    for round_no in range(4):
        started = start_calls(bed, 25, 'slow{}'.format(round_no))
        expected += 2 * len(started)
        time.sleep(2)
        bed.raw('hupall NORMAL_CLEARING')
        time.sleep(1)
    before = container_stats(bed)
    got = again.read(15)
    results['slow_consumer'] = {
        'legs_expected': expected,
        'events_received_after_resume': len(got),
        'disconnected_by_server': again.disconnected,
        'container': before,
    }
    print('slow', results['slow_consumer'], flush=True)
    again.close()
    # 7. authentication failure and denied subscription
    bad = Observer(bed, user='esl_ev@test.local', password='definitely-wrong')
    results['bad_password_reply'] = bad.reply
    bad.close()
    denied = Observer(bed, user='esl_ev@test.local', password=bed.restricted_password, events='CHANNEL_CREATE')
    results['denied_subscription_reply'] = denied.reply
    denied.close()
    allowed = Observer(bed, user='esl_ev@test.local', password=bed.restricted_password)
    results['event_only_account_reply'] = allowed.reply
    started = start_calls(bed, 3, 'evonly')
    time.sleep(2)
    bed.raw('hupall NORMAL_CLEARING')
    results['event_only_account_delivery'] = reconcile(started, allowed.read(4))
    # 8. FreeSWITCH restart with calls up
    started = start_calls(bed, 5, 'restart')
    time.sleep(2)
    created_before = created_total(bed)
    bed.restart()
    try:
        lost = allowed.read(3)
    except Exception:
        lost = []
    results['freeswitch_restart'] = {
        'calls_in_flight': len(started),
        'events_after_restart_on_old_subscription': len(lost),
        'old_subscription_disconnected': allowed.disconnected,
        'sessions_created_before': created_before,
    }
    allowed.close()
    ready(bed)
    fresh = Observer(bed, user='esl_ev@test.local', password=bed.restricted_password)
    started = start_calls(bed, 3, 'postrestart')
    time.sleep(2)
    bed.raw('hupall NORMAL_CLEARING')
    results['after_restart_new_subscription'] = reconcile(started, fresh.read(4))
    fresh.close()
    return results


def main(out: str, phases: list[str]) -> None:
    results: dict[str, object] = {'environment': {'host_cpus': 8, 'docker_memory_bytes': 6210043904}}
    with Testbed(max_sessions=3000, sessions_per_second=1000, rtp_end=30000,
                 codecs='PCMU,PCMA,speex@8000h@20i,speex@16000h@20i,L16') as bed:  # fmt: skip
        ready(bed)
        for phase in phases:
            observer = Observer(bed)
            if phase == 'load':
                results['load'] = load_phase(bed, observer)
            elif phase == 'codecs':
                results['codecs'] = codec_phase(bed, observer)
            elif phase == 'resilience':
                observer.close()
                results['resilience'] = resilience_phase(bed)
                continue
            observer.close()
    with open(out, 'w') as handle:
        json.dump(results, handle, indent=1)


if __name__ == '__main__':
    main(sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else 'load,codecs,resilience').split(','))
