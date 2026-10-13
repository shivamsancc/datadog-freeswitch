# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""The listener: reader thread -> bounded queue -> emitter thread, with reconnect, gap accounting and health telemetry.

Delivery is best effort and says so: FreeSWITCH does not replay events missed while disconnected, the queue drops
the newest event when full, and DogStatsD over UDP has no acknowledgement. Cumulative totals are emitted as gauges so
that datagrams lost on the way to the Agent can be detected afterwards by comparing them with the distribution counts.
"""

from __future__ import annotations

import logging
import queue
import random
import threading
import time
from collections import OrderedDict
from typing import Callable

from .config import Config
from .esl import EslError, EventClient
from .events import RawSample, parse_event_body
from .quality import OUTCOMES, classify
from .statsd import StatsdClient

LOG = logging.getLogger('fs_rtp_listener')
DEDUPE_SIZE = 5000

COUNTERS = (
    'events',
    'duplicates',
    'parse_errors',
    'dropped',
    'reconnects',
    'connect_errors',
    'send_errors',
    'submitted_mos',
    'submitted_quality',
)


class Listener:
    def __init__(
        self,
        config: Config,
        statsd: StatsdClient | None = None,
        client_factory: Callable[[], EventClient] | None = None,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.statsd = statsd or StatsdClient(config.statsd_host, config.statsd_port)
        self._client_factory = client_factory or self._default_client
        self._rng, self._clock = rng, clock
        self._queue: queue.Queue[RawSample] = queue.Queue(config.queue_size)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._counts = dict.fromkeys(COUNTERS, 0)
        self._outcomes = dict.fromkeys(OUTCOMES, 0)
        self._connect_reasons: dict[str, int] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._gap_closed = 0.0
        self._gap_since: float | None = clock()  # collection has not started yet: that time is a gap
        self._connected = False
        self._client: EventClient | None = None
        self._threads: list[threading.Thread] = []
        self._last_logged: tuple[str, float] = ('', 0.0)
        self._emitted_snapshot: dict[str, float] = {}
        self._emit_hook: Callable[[], object] | None = None  # tests only: slows the emitter to force overload
        self.max_queue_depth = 0

    # -- construction helpers ---------------------------------------------------------------------------------

    def _default_client(self) -> EventClient:
        c = self.config
        return EventClient(
            c.host,
            c.port,
            c.password,
            c.username,
            c.strict_permissions,
            c.connect_timeout,
            c.read_timeout,
            c.max_event_bytes,
        )

    # -- lifecycle ----------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._threads:
            raise RuntimeError('already started')
        self._threads = [
            threading.Thread(target=self._reader, name='fs-rtp-reader', daemon=True),
            threading.Thread(target=self._emitter, name='fs-rtp-emitter', daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop reading, finish the queued samples, emit a final health report and close. True if all threads ended."""
        self._stop.set()
        client = self._client
        if client is not None:
            client.close()  # unblocks the reader
        end = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, end - time.monotonic()))
        alive = any(t.is_alive() for t in self._threads)
        self.statsd.close()
        return not alive

    def force_disconnect(self) -> None:
        """Close the current connection as if the network dropped it (used by tests and operators)."""
        client = self._client
        if client is not None:
            client.close()

    # -- state --------------------------------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                'counters': dict(self._counts, send_errors=self.statsd.send_errors),
                'outcomes': dict(self._outcomes),
                'connect_reasons': dict(self._connect_reasons),
                'gap_seconds_total': round(self._gap_total(), 3),
                'connected': self._connected,
                'queue_depth': self._queue.qsize(),
                'max_queue_depth': self.max_queue_depth,
            }

    def _gap_total(self) -> float:
        open_gap = (self._clock() - self._gap_since) if self._gap_since is not None else 0.0
        return self._gap_closed + open_gap

    def _bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[name] += amount

    # -- reader -------------------------------------------------------------------------------------------------

    def _log(self, key: str, message: str, *args: object) -> None:
        """Rate-limited warning: only fixed text and bounded reasons are ever logged."""
        now = self._clock()
        if key != self._last_logged[0] or now - self._last_logged[1] > 60:
            self._last_logged = (key, now)
            LOG.warning(message, *args)

    def _reader(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            client = self._client_factory()
            self._client = client
            try:
                client.connect()
            except EslError as error:
                with self._lock:
                    self._counts['connect_errors'] += 1
                    self._connect_reasons[error.reason] = self._connect_reasons.get(error.reason, 0) + 1
                self._log('connect:' + error.reason, 'FreeSWITCH event connection failed (reason=%s)', error.reason)
                attempt += 1
                self._sleep_backoff(attempt)
                continue
            attempt = 0
            with self._lock:
                if self._gap_since is not None:
                    self._gap_closed += self._clock() - self._gap_since
                    self._gap_since = None
                self._connected = True
                self._counts['reconnects'] += 1
            LOG.info('FreeSWITCH event subscription established')
            try:
                while not self._stop.is_set():
                    for body in client.events():
                        self._receive(body)
                        if self._stop.is_set():
                            break
            except EslError as error:
                self._log('lost:' + error.reason, 'FreeSWITCH event connection ended (reason=%s)', error.reason)
            finally:
                client.close()
                with self._lock:
                    self._connected = False
                    if self._gap_since is None:
                        self._gap_since = self._clock()
            if not self._stop.is_set():
                attempt += 1
                self._sleep_backoff(attempt)

    def _sleep_backoff(self, attempt: int) -> None:
        """Bounded exponential backoff with jitter: a delay in [d/2, d], d = min(max, min * 2^(attempt-1))."""
        ceiling = min(self.config.backoff_max, self.config.backoff_min * (2 ** max(0, attempt - 1)))
        delay = ceiling * (0.5 + 0.5 * self._rng())
        self._stop.wait(delay)

    def _receive(self, body: str) -> None:
        try:
            sample = parse_event_body(body)
        except Exception:  # defensive: a hostile event must never stop the reader
            self._bump('parse_errors')
            return
        self._bump('events')
        if sample.event_id is not None:
            with self._lock:
                if sample.event_id in self._seen:
                    self._counts['duplicates'] += 1
                    return
                self._seen[sample.event_id] = None
                if len(self._seen) > DEDUPE_SIZE:
                    self._seen.popitem(last=False)  # bounded memory
        try:
            self._queue.put_nowait(sample)
        except queue.Full:
            self._bump('dropped')  # drop-newest policy
        depth = self._queue.qsize()
        if depth > self.max_queue_depth:
            self.max_queue_depth = depth

    # -- emitter ------------------------------------------------------------------------------------------------

    def _emitter(self) -> None:
        last_report = self._clock()
        while True:
            try:
                sample = self._queue.get(timeout=0.2)
            except queue.Empty:
                sample = None
                if self._stop.is_set():
                    break
            if sample is not None:
                if self._emit_hook is not None:
                    self._emit_hook()
                try:
                    self._process(sample)
                except Exception:  # defensive: one bad sample must not end the emitter
                    self._bump('parse_errors')
            if self._clock() - last_report >= self.config.health_interval:
                last_report = self._clock()
                self.report_health()
        self.report_health()

    def _process(self, sample: RawSample) -> None:
        c = self.config
        result = classify(sample, c.min_packets, c.codecs)
        with self._lock:
            self._outcomes[result.outcome] += 1
        if result.outcome != 'valid':
            return
        tags = ['direction:' + result.direction, 'codec:' + result.codec, *c.static_tags]
        prefix = c.metric_prefix
        mos_ok = self.statsd.distribution(prefix + '.mos', result.mos, tags)  # type: ignore[arg-type]
        quality_ok = self.statsd.distribution(prefix + '.quality_percentage', result.quality, tags)  # type: ignore[arg-type]
        with self._lock:
            self._counts['submitted_mos'] += 1 if mos_ok else 0
            self._counts['submitted_quality'] += 1 if quality_ok else 0

    # -- health -------------------------------------------------------------------------------------------------

    def report_health(self) -> None:
        """Emit health: deltas as counts, state and cumulative totals as gauges (totals survive lost datagrams)."""
        c = self.config
        prefix = c.metric_prefix + '.listener'
        base = list(c.static_tags)
        snap = self.snapshot()
        counters = dict(snap['counters'])  # type: ignore[call-overload]
        outcomes: dict[str, int] = snap['outcomes']  # type: ignore[assignment]
        gap_total = float(snap['gap_seconds_total'])  # type: ignore[arg-type]
        lines: list[str] = []
        line = StatsdClient.line
        lines.append(line(prefix + '.connected', 1 if snap['connected'] else 0, 'g', base))
        lines.append(line(prefix + '.queue_depth', int(snap['queue_depth']), 'g', base))  # type: ignore[call-overload]
        last = self._emitted_snapshot
        for name in ('events', 'duplicates', 'parse_errors', 'dropped', 'reconnects', 'connect_errors', 'send_errors'):
            delta = counters[name] - int(last.get('c:' + name, 0))
            lines.append(line('{}.{}'.format(prefix, name), delta, 'c', base))
            last['c:' + name] = counters[name]
        for outcome in OUTCOMES:
            delta = outcomes[outcome] - int(last.get('o:' + outcome, 0))
            lines.append(line(prefix + '.samples', delta, 'c', ['outcome:' + outcome, *base]))
            last['o:' + outcome] = outcomes[outcome]
        gap_delta = round(gap_total - last.get('gap', 0.0), 3)
        lines.append(line(prefix + '.gap_seconds', gap_delta, 'c', base))
        last['gap'] = gap_total
        # cumulative totals: gauges, one metric, bounded `counter` tag values
        totals = {name: counters[name] for name in COUNTERS}
        totals.update({'samples_' + o: outcomes[o] for o in OUTCOMES})
        totals['gap_seconds'] = round(gap_total, 1)  # type: ignore[assignment]
        for name, value in sorted(totals.items()):
            lines.append(line(prefix + '.total', value, 'g', ['counter:' + name, *base]))
        accepted = self.statsd.send_lines(lines)
        if accepted != len(lines):
            self._log('health', 'some health datagrams could not be sent')
