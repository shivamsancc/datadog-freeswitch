# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Listener configuration. Every value is validated; the password is never part of repr, str or errors."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping

DEFAULT_CODECS = ('pcmu', 'pcma', 'g722', 'g729', 'opus', 'speex', 'l16', 'ilbc', 'amr', 'amr-wb')
_USERNAME = re.compile(r'[A-Za-z0-9._-]+@[A-Za-z0-9._-]+')
_TAG = re.compile(r'[a-z][a-z0-9_.-]*:[A-Za-z0-9_./-]+')
_NAME = re.compile(r'[a-z0-9_.-]+')


class ConfigError(ValueError):
    """Raised with a fixed message; never contains a secret."""


@dataclass(frozen=True)
class Config:
    host: str = '127.0.0.1'
    port: int = 8021
    username: str | None = None
    password: str = field(default='', repr=False)
    allow_shared_password: bool = False
    strict_permissions: bool = True
    statsd_host: str = '127.0.0.1'
    statsd_port: int = 8125
    min_packets: int = 50
    queue_size: int = 2000
    health_interval: float = 10.0
    connect_timeout: float = 5.0
    read_timeout: float = 1.0
    backoff_min: float = 1.0
    backoff_max: float = 30.0
    max_event_bytes: int = 262144
    codecs: tuple[str, ...] = DEFAULT_CODECS
    static_tags: tuple[str, ...] = ()
    metric_prefix: str = 'freeswitch.rtp'

    def __post_init__(self) -> None:
        if not self.host or not self.host.strip():
            raise ConfigError('host must not be empty')
        for name, value, low, high in (
            ('port', self.port, 1, 65535),
            ('statsd_port', self.statsd_port, 1, 65535),
            ('min_packets', self.min_packets, 1, 1_000_000),
            ('queue_size', self.queue_size, 1, 1_000_000),
            ('max_event_bytes', self.max_event_bytes, 1024, 16_777_216),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise ConfigError('{} is out of range'.format(name))
        for fname, fvalue in (
            ('health_interval', self.health_interval),
            ('connect_timeout', self.connect_timeout),
            ('read_timeout', self.read_timeout),
            ('backoff_min', self.backoff_min),
            ('backoff_max', self.backoff_max),
        ):
            if not 0 < fvalue <= 3600:
                raise ConfigError('{} is out of range'.format(fname))
        if self.backoff_min > self.backoff_max:
            raise ConfigError('backoff_min must not exceed backoff_max')
        if not self.password or any(ord(c) < 0x20 or ord(c) == 0x7F for c in self.password):
            raise ConfigError('password must be set and must not contain control characters')
        if self.username is None:
            if not self.allow_shared_password:
                raise ConfigError('a restricted username is required unless allow_shared_password is set')
        elif not _USERNAME.fullmatch(self.username):
            raise ConfigError('username must have the form user@domain')
        for codec in self.codecs:
            if not _NAME.fullmatch(codec):
                raise ConfigError('codec names must be lowercase words')
        for tag in self.static_tags:
            if not _TAG.fullmatch(tag) or tag.split(':', 1)[0] in (
                'direction',
                'codec',
                'outcome',
                'counter',
                'metric',
            ):
                raise ConfigError('static tags must be key:value and must not reuse a reserved key')
        if not _NAME.fullmatch(self.metric_prefix):
            raise ConfigError('metric_prefix is invalid')

    def __repr__(self) -> str:
        return 'Config(host={!r}, port={!r}, username={!r}, statsd={}:{})'.format(
            self.host, self.port, self.username, self.statsd_host, self.statsd_port
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env

        def get(name: str, default: str | None = None) -> str | None:
            return env.get('FS_RTP_' + name, default)

        password = get('PASSWORD')
        password_file = get('PASSWORD_FILE')
        if password_file:
            try:
                with open(password_file) as handle:
                    password = handle.read().strip()
            except OSError:
                raise ConfigError('password file could not be read') from None
        values: dict[str, object] = {'password': password or ''}
        casts = {
            'HOST': ('host', str),
            'PORT': ('port', int),
            'USERNAME': ('username', str),
            'STATSD_HOST': ('statsd_host', str),
            'STATSD_PORT': ('statsd_port', int),
            'MIN_PACKETS': ('min_packets', int),
            'QUEUE_SIZE': ('queue_size', int),
            'HEALTH_INTERVAL': ('health_interval', float),
            'CONNECT_TIMEOUT': ('connect_timeout', float),
            'READ_TIMEOUT': ('read_timeout', float),
            'BACKOFF_MIN': ('backoff_min', float),
            'BACKOFF_MAX': ('backoff_max', float),
            'METRIC_PREFIX': ('metric_prefix', str),
        }
        for key, (attr, cast) in casts.items():
            raw = get(key)
            if raw not in (None, ''):
                try:
                    values[attr] = cast(raw)  # type: ignore[operator]
                except ValueError:
                    raise ConfigError('{} is not valid'.format(attr)) from None
        if get('ALLOW_SHARED_PASSWORD') == '1':
            values['allow_shared_password'] = True
        if get('STRICT_PERMISSIONS') == '0':
            values['strict_permissions'] = False
        if get('CODECS'):
            values['codecs'] = tuple(x.strip().lower() for x in get('CODECS').split(',') if x.strip())  # type: ignore[union-attr]
        if get('TAGS'):
            values['static_tags'] = tuple(x.strip() for x in get('TAGS').split(',') if x.strip())  # type: ignore[union-attr]
        return cls(**values)  # type: ignore[arg-type]
