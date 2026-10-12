# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import annotations

import time
from typing import Any

from datadog_checks.base import AgentCheck, ConfigurationError
from datadog_checks.base.types import InitConfigType, InstanceType

from .config_models import ConfigMixin
from .esl import CMD_CHANNELS, CMD_UPTIME, EslAuthError, EslClient, EslError, parse_channels, parse_uptime

DEFAULT_COLLECTION_INTERVAL = 15.0
# While a failure persists with the same error_type, warn again only this often (seconds).
LOG_REMINDER_SECONDS = 900.0


class FreeswitchCheck(AgentCheck, ConfigMixin):
    # This will be the prefix of every metric the integration sends
    __NAMESPACE__ = 'freeswitch'

    def __init__(self, name: str, init_config: InitConfigType, instances: list[InstanceType]) -> None:
        super().__init__(name, init_config, instances)
        self._client: EslClient | None = None
        self._base_tags: list[str] = []
        self._run_budget = 9.0
        self._consecutive_failures = 0
        self._last_error_type: str | None = None
        self._last_warning = 0.0
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

        # The whole run (connect, authenticate, both commands, one possible reconnect) must finish
        # before the next scheduled run.
        interval: Any = (
            self.instance.get('min_collection_interval')
            or self.init_config.get('min_collection_interval')
            or DEFAULT_COLLECTION_INTERVAL
        )
        self._run_budget = min(connect_timeout + 2 * read_timeout, float(interval) * 0.9)

        self._base_tags = ['freeswitch_host:{}'.format(config.host), 'freeswitch_port:{}'.format(port)]
        self._base_tags.extend(config.tags or [])

    def check(self, _: InstanceType) -> None:
        start = time.monotonic()
        error_type = None
        reason = ''
        uptime = channels = 0
        try:
            uptime, channels = self._collect(start + self._run_budget)
        except EslError as e:
            error_type = e.error_type
            reason = str(e)  # always a fixed message; never server text or credentials
        except BaseException:
            # A defect or an interrupt: the stream state is unknown, so do not reuse it.
            self._discard()
            raise
        duration = time.monotonic() - start
        self._log_outcome(error_type, reason)

        tags = self._base_tags
        if error_type is None:
            self.gauge('uptime', uptime, tags=tags)
            self.gauge('channels.active', channels, tags=tags)
            self.gauge('can_connect', 1, tags=tags)
            self.count('collection.errors', 0, tags=tags)
        else:
            self.gauge('can_connect', 0, tags=tags)
            self.count('collection.errors', 1, tags=tags + ['error_type:{}'.format(error_type)])
        self.gauge('collection.duration', duration, tags=tags)

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

    def _collect(self, deadline: float) -> tuple[int, int]:
        client = self._client
        if client is None:
            raise ConfigurationError('check is not configured')

        # A reused connection that the server closed in the meantime is replaced up front. If it dies
        # during the exchange anyway, exactly one fresh connection is tried within the same run.
        if client.connected and not client.is_stale():
            try:
                return self._query(client, deadline)
            except EslAuthError:
                raise
            except EslError:
                pass  # the client closed itself, or the data was unusable; reconnect once below

        client.connect(deadline)
        return self._query(client, deadline)

    @staticmethod
    def _query(client: EslClient, deadline: float) -> tuple[int, int]:
        uptime = parse_uptime(client.api(CMD_UPTIME, deadline))
        channels = parse_channels(client.api(CMD_CHANNELS, deadline))
        return uptime, channels
