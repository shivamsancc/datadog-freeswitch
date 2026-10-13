# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import annotations

import socket
from typing import Any

import pytest

from fs_rtp_listener.config import Config, ConfigError
from fs_rtp_listener.statsd import MAX_DATAGRAM, StatsdClient

from .conftest import SENTINEL, UdpSink


def make(**kw: Any) -> Config:
    base: dict[str, Any] = {'username': 'esl_ev@test.local', 'password': SENTINEL}
    base.update(kw)
    return Config(**base)


def test_defaults_and_repr_hide_the_password() -> None:
    config = make()
    assert SENTINEL not in repr(config) and SENTINEL not in str(config)
    assert config.min_packets == 50 and config.queue_size == 2000 and config.strict_permissions


@pytest.mark.parametrize(
    'kw',
    [
        {'host': ''},
        {'host': '   '},
        {'port': 0},
        {'port': 70000},
        {'statsd_port': 0},
        {'min_packets': 0},
        {'queue_size': 0},
        {'health_interval': 0},
        {'backoff_min': 10, 'backoff_max': 1},
        {'password': ''},
        {'password': 'a\nb'},
        {'username': 'noat'},
        {'username': 'a b@c'},
        {'username': None},
        {'codecs': ('PCMU',)},
        {'static_tags': ('direction:x',)},
        {'static_tags': ('bad tag',)},
        {'metric_prefix': 'A B'},
    ],
)
def test_invalid_configuration_is_rejected_with_a_fixed_message(kw: dict[str, Any]) -> None:
    with pytest.raises(ConfigError) as error:
        make(**kw)
    assert SENTINEL not in str(error.value)


def test_shared_password_mode_needs_an_explicit_opt_in() -> None:
    assert make(username=None, allow_shared_password=True).username is None


def test_from_env_and_password_file(tmp_path: Any) -> None:
    secret = tmp_path / 'pw'
    secret.write_text(SENTINEL + '\n')
    config = Config.from_env(
        {
            'FS_RTP_HOST': '10.0.0.5',
            'FS_RTP_PORT': '8022',
            'FS_RTP_USERNAME': 'u@d',
            'FS_RTP_PASSWORD_FILE': str(secret),
            'FS_RTP_STATSD_PORT': '9125',
            'FS_RTP_CODECS': 'PCMU, opus',
            'FS_RTP_TAGS': 'env_name:lab,site:a',
        }
    )
    assert (config.host, config.port, config.statsd_port) == ('10.0.0.5', 8022, 9125)
    assert config.password == SENTINEL and config.codecs == ('pcmu', 'opus')
    assert config.static_tags == ('env_name:lab', 'site:a')
    with pytest.raises(ConfigError):
        Config.from_env({'FS_RTP_PORT': 'x', 'FS_RTP_USERNAME': 'u@d', 'FS_RTP_PASSWORD': 'p'})
    with pytest.raises(ConfigError) as error:
        Config.from_env({'FS_RTP_PASSWORD_FILE': str(tmp_path / 'missing'), 'FS_RTP_USERNAME': 'u@d'})
    assert str(tmp_path) not in str(error.value)


def test_statsd_line_formats() -> None:
    assert (
        StatsdClient.line('a.b', 4.5, 'd', ['direction:inbound', 'codec:pcmu'])
        == 'a.b:4.5|d|#direction:inbound,codec:pcmu'
    )
    assert StatsdClient.line('a.b', 3, 'c') == 'a.b:3|c'
    assert StatsdClient.line('a.b', 1, 'g', []) == 'a.b:1|g'


def test_statsd_sends_and_batches_within_one_datagram(sink: UdpSink, wait_for: Any) -> None:
    client = StatsdClient('127.0.0.1', sink.port)
    assert client.distribution('x.mos', 4.5, ['direction:inbound'])
    lines = ['m.{}:1|c'.format(i) for i in range(300)]
    assert client.send_lines(lines) == 300
    assert wait_for(lambda: len(sink.lines) >= 301)
    assert sink.lines[0] == 'x.mos:4.5|d|#direction:inbound'
    assert client.datagrams >= 3  # 300 lines do not fit one datagram of {} bytes
    client.close()
    assert MAX_DATAGRAM == 1400


def test_statsd_send_error_is_counted_and_recovers(sink: UdpSink, wait_for: Any) -> None:
    created: list[socket.socket] = []

    def factory() -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        created.append(sock)
        return sock

    client = StatsdClient('256.256.256.256', 9, factory)  # connect fails
    assert not client.distribution('x', 1.0)
    assert client.send_errors == 1
    client._host, client._port = '127.0.0.1', sink.port
    assert client.distribution('x', 2.0)
    assert wait_for(lambda: sink.lines == ['x:2.0|d'])
    assert len(created) == 3  # the failed send tried two sockets (one retry), the next send a third
    client.close()
