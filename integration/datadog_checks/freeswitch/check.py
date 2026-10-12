# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import annotations

import time
from typing import Any, Callable

from datadog_checks.base import AgentCheck, ConfigurationError
from datadog_checks.base.types import InitConfigType, InstanceType

from .config_models import ConfigMixin
from .esl import (
    CMD_BRIDGED,
    CMD_CHANNELS,
    CMD_REGISTRATIONS,
    CMD_STATUS,
    CMD_UPTIME,
    MAX_LISTING_BYTES,
    EslAuthError,
    EslClient,
    EslError,
    EslParseError,
    StatusFields,
    parse_bridged_calls,
    parse_channels,
    parse_registrations,
    parse_status,
    parse_uptime,
)

DEFAULT_COLLECTION_INTERVAL = 15.0
# While a failure persists with the same error_type, warn again only this often (seconds).
LOG_REMINDER_SECONDS = 900.0
# ``uptime s``, ``show channels count``, ``show status`` and ``show registrations count``, plus
# ``show bridged_calls count`` when ``collect_bridged_calls`` is enabled: each gets at most one read timeout of the run
# budget.
COMMANDS_PER_RUN = 4


class FreeswitchCheck(AgentCheck, ConfigMixin):
    # This will be the prefix of every metric the integration sends
    __NAMESPACE__ = 'freeswitch'

    def __init__(self, name: str, init_config: InitConfigType, instances: list[InstanceType]) -> None:
        super().__init__(name, init_config, instances)
        self._client: EslClient | None = None
        self._base_tags: list[str] = []
        self._run_budget = 9.0
        self._collect_bridged = False
        self._consecutive_failures = 0
        self._last_error_type: str | None = None
        self._last_warning = 0.0
        # Restart tracking for sessions.created (see _submit_created).
        self._last_uptime: int | None = None
        self._last_created: int | None = None
        self._restart_pending = False
        # Runs after the generated configuration models are loaded.
        self.check_initializations.append(self._configure)

    def _configure(self) -> None:
        config = self.config
        connect_timeout = float(config.connect_timeout if config.connect_timeout is not None else 3.0)
        read_timeout = float(config.read_timeout if config.read_timeout is not None else 3.0)
        port = config.port if config.port is not None else 8021
        try:
            self._client = EslClient(
                host=config.host,
                port=port,
                password=config.password,
                username=config.username,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            )
        except ValueError as e:
            # The message is fixed text and never contains the credential values.
            raise ConfigurationError(str(e)) from None

        # The whole run (connect, authenticate, the three commands, one possible reconnect) must finish
        # before the next scheduled run.
        interval: Any = (
            self.instance.get('min_collection_interval')
            or self.init_config.get('min_collection_interval')
            or DEFAULT_COLLECTION_INTERVAL
        )
        # Off by default: the listing it returns carries caller details and grows with the number of calls.
        self._collect_bridged = bool(config.collect_bridged_calls)
        commands = COMMANDS_PER_RUN + (1 if self._collect_bridged else 0)
        self._run_budget = min(connect_timeout + commands * read_timeout, float(interval) * 0.9)

        self._base_tags = ['freeswitch_host:{}'.format(config.host), 'freeswitch_port:{}'.format(port)]
        self._base_tags.extend(config.tags or [])

    def check(self, _: InstanceType) -> None:
        start = time.monotonic()
        error_type = None
        reason = ''
        uptime = channels = 0
        optional = _Optional()
        try:
            uptime, channels, optional = self._collect(start + self._run_budget)
        except EslError as e:
            error_type = e.error_type
            reason = str(e)  # always a fixed message; never server text or credentials
            if not isinstance(e, EslParseError):
                # A refusal or a command error on a required query ends the connection (as before); an unparseable
                # but complete response leaves the stream in sync, so the connection is kept.
                self._discard()
        except BaseException:
            # A defect or an interrupt: the stream state is unknown, so do not reuse it.
            self._discard()
            raise
        duration = time.monotonic() - start

        tags = self._base_tags
        if error_type is not None:
            self._log_outcome(error_type, reason)
            self.gauge('can_connect', 0, tags=tags)
            self.count('collection.errors', 1, tags=tags + ['error_type:{}'.format(error_type)])
            self.gauge('collection.duration', duration, tags=tags)
            return

        # The connection, authentication and both required queries succeeded: the run is available. Failures of the
        # optional queries are reported through the same bounded error_type values without touching can_connect
        # or the samples that were collected.
        failed = optional.errors
        if failed:
            first_name, first_error = failed[0]
            self._log_outcome(
                first_error.error_type,
                '{} query failed: {}'.format(first_name, first_error),
            )
        else:
            self._log_outcome(None)

        self.gauge('uptime', uptime, tags=tags)
        self.gauge('channels.active', channels, tags=tags)
        if self._last_uptime is not None and uptime < self._last_uptime:
            self._restart_pending = True
        self._last_uptime = uptime
        status = optional.status
        if status is not None:
            self._submit_created(status.created, uptime, tags)
            self.gauge('sessions.limit', status.limit, tags=tags)
            if status.active is not None:
                self.gauge('sessions.active', status.active, tags=tags)
            if status.peak is not None:
                self.gauge('sessions.peak', status.peak, tags=tags)
            if status.rate_limit is not None:
                self.gauge('sessions.rate.limit', status.rate_limit, tags=tags)
        if optional.registrations is not None:
            self.gauge('registrations', optional.registrations, tags=tags)
        if optional.bridged is not None:
            self.gauge('calls.bridged', optional.bridged, tags=tags)
        self.gauge('can_connect', 1, tags=tags)
        if failed:
            for error in sorted({e.error_type for _, e in failed}):
                self.count('collection.errors', 1, tags=tags + ['error_type:{}'.format(error)])
        else:
            self.count('collection.errors', 0, tags=tags)
        self.gauge('collection.duration', duration, tags=tags)

    def _submit_created(self, created: int, uptime: int, tags: list[str]) -> None:
        """Submit the cumulative session counter as a monotonic count that survives a FreeSWITCH restart.

        The Agent turns a monotonic count into per-interval increases and ignores a sample lower than the previous
        one, taking that lower value as the new baseline. Measured with a real Agent: when the restarted process
        passes the old count before the first sample, only ``new - old`` is reported and the rest is lost. A restart is
        therefore detected here (uptime went down, or the counter went down) and a 0 sample is submitted first, which
        moves the Agent's baseline to 0 so that the whole new count is reported as an increase.
        """
        if self._last_created is not None and created < self._last_created:
            self._restart_pending = True
        if self._restart_pending:
            self.monotonic_count('sessions.created', 0, tags=tags)
            self._restart_pending = False
        self.monotonic_count('sessions.created', created, tags=tags)
        self._last_created = created

    def _log_outcome(self, error_type: str | None, reason: str = '') -> None:
        """Warn on the first failure and on a change of error type, remind rarely, report recovery.

        Only fixed text, the host/port from the configuration and the bounded error type are logged.
        """
        target = '{}:{}'.format(self.config.host, self.config.port if self.config.port is not None else 8021)
        if error_type is None:
            if self._consecutive_failures:
                self.log.info(
                    'FreeSWITCH collection on %s recovered after %d failed run(s)',
                    target,
                    self._consecutive_failures,
                )
            self._consecutive_failures = 0
            self._last_error_type = None
            return

        self._consecutive_failures += 1
        now = time.monotonic()
        changed = error_type != self._last_error_type
        if changed or now - self._last_warning >= LOG_REMINDER_SECONDS:
            self._last_warning = now
            self.log.warning(
                'FreeSWITCH collection on %s failed (error_type=%s, consecutive_failures=%d): %s',
                target,
                error_type,
                self._consecutive_failures,
                reason,
            )
        else:
            self.log.debug(
                'FreeSWITCH collection on %s still failing (error_type=%s, consecutive_failures=%d)',
                target,
                error_type,
                self._consecutive_failures,
            )
        self._last_error_type = error_type

    def _discard(self) -> None:
        if self._client is not None:
            self._client.close()

    def _collect(self, deadline: float) -> tuple[int, int, _Optional]:
        client = self._client
        if client is None:
            raise ConfigurationError('check is not configured')

        # A reused connection that the server closed in the meantime is replaced up front. If it dies
        # during the exchange anyway, exactly one fresh connection is tried within the same run.
        required: tuple[int, int] | None = None
        if client.connected and not client.is_stale():
            try:
                required = self._query_required(client, deadline)
            except EslAuthError:
                raise
            except EslError:
                pass  # the client closed itself, or the data was unusable; reconnect once below

        if required is None:
            client.connect(deadline)
            required = self._query_required(client, deadline)

        # The remaining queries are optional: a failure of one (not supported by this FreeSWITCH, refused,
        # malformed) must neither discard the samples above nor those of the other queries. A failure that left the
        # connection unusable (timeout, lost connection, protocol violation) ends the optional queries of this run;
        # the client has closed itself and the next run reconnects.
        optional = _Optional()
        for name, command, parse, max_body in _OPTIONAL_QUERIES:
            if name == 'bridged' and not self._collect_bridged:
                continue
            try:
                value = parse(client.api(command, deadline, max_body))
            except EslError as e:
                optional.errors.append((name, e))
                if not client.connected:
                    break
                continue
            setattr(optional, name, value)
        return required[0], required[1], optional

    @staticmethod
    def _query_required(client: EslClient, deadline: float) -> tuple[int, int]:
        uptime = parse_uptime(client.api(CMD_UPTIME, deadline))
        channels = parse_channels(client.api(CMD_CHANNELS, deadline))
        return uptime, channels


class _Optional:
    """Results of the optional queries of one run. ``None`` means the value is not available this run."""

    def __init__(self) -> None:
        self.status: StatusFields | None = None
        self.registrations: int | None = None
        self.bridged: int | None = None
        self.errors: list[tuple[str, EslError]] = []


# (attribute of _Optional, command, parser, response size limit); the order is the order of the queries.
_OPTIONAL_QUERIES: list[tuple[str, str, Callable[[bytes], Any], int]] = [
    ('status', CMD_STATUS, parse_status, 64 * 1024),
    ('registrations', CMD_REGISTRATIONS, parse_registrations, 64 * 1024),
    ('bridged', CMD_BRIDGED, parse_bridged_calls, MAX_LISTING_BYTES),
]
