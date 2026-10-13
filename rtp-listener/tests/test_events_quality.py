# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import annotations

import pytest

from fs_rtp_listener.config import DEFAULT_CODECS
from fs_rtp_listener.events import RawSample, parse_event_body
from fs_rtp_listener.quality import classify, normalize_codec, normalize_direction

from .conftest import event_body

CODECS = DEFAULT_CODECS


def test_parse_keeps_only_the_wanted_fields() -> None:
    sample = parse_event_body(event_body().decode())
    assert sample == RawSample('u-1', 'outbound', 'PCMU', '4.50', '100.00', '500', 1791824000000000)
    assert not hasattr(sample, 'caller')
    assert '5551110000' not in repr(sample)  # caller data never survives parsing


def test_parse_url_decodes_and_stops_at_the_blank_line() -> None:
    body = 'Unique-ID: a%2Fb\nvariable_rtp_audio_in_mos: 4.50\n\nvariable_rtp_audio_in_packet_count: 999\n'
    sample = parse_event_body(body)
    assert sample.event_id == 'a/b'
    assert sample.packets is None  # after the blank line: the event's own body, ignored


@pytest.mark.parametrize('body', ['', '\n\n', 'garbage', 'no separator here\nanother', ': \n: x', '\x00\x01'])
def test_parse_never_raises_and_returns_empty_samples(body: str) -> None:
    assert parse_event_body(body) == RawSample(None, None, None, None, None, None, None)


def test_parse_bad_timestamp_is_none_and_first_duplicate_header_wins() -> None:
    sample = parse_event_body(
        'Event-Date-Timestamp: nope\nvariable_rtp_audio_in_mos: 4.5\nvariable_rtp_audio_in_mos: 1.0\n'
    )
    assert sample.timestamp_us is None
    assert sample.mos == '4.5'


def sample(**kw: str | None) -> RawSample:
    base = {
        'event_id': 'x',
        'direction': 'outbound',
        'codec': 'PCMU',
        'mos': '4.50',
        'quality': '100.00',
        'packets': '500',
        'timestamp_us': None,
    }
    base.update(kw)
    return RawSample(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    'kw, outcome',
    [
        ({}, 'valid'),
        ({'mos': '0.99', 'quality': '4.00'}, 'valid'),  # a genuinely bad call is still a valid measurement
        ({'mos': '5', 'quality': '0'}, 'valid'),
        ({'packets': '50'}, 'valid'),
        ({'packets': '49'}, 'short'),
        ({'packets': '1'}, 'short'),
        ({'packets': '0', 'mos': 'nan', 'quality': 'nan'}, 'no_media'),
        ({'packets': None}, 'missing_fields'),
        ({'mos': None}, 'missing_fields'),
        ({'quality': None}, 'missing_fields'),
        ({'mos': 'nan'}, 'non_finite'),
        ({'quality': 'NaN'}, 'non_finite'),
        ({'mos': 'inf'}, 'non_finite'),
        ({'mos': '-inf'}, 'non_finite'),
        ({'quality': 'Infinity'}, 'non_finite'),
        ({'mos': 'abc'}, 'invalid_value'),
        ({'quality': ''}, 'invalid_value'),
        ({'packets': 'many'}, 'invalid_value'),
        ({'packets': '-3'}, 'invalid_value'),
        ({'packets': '1.5'}, 'invalid_value'),
        ({'mos': '5.01'}, 'out_of_range'),
        ({'mos': '0.1'}, 'out_of_range'),
        ({'mos': '-1'}, 'out_of_range'),
        ({'quality': '100.5'}, 'out_of_range'),
        ({'quality': '-0.1'}, 'out_of_range'),
    ],
)
def test_classification(kw: dict[str, str | None], outcome: str) -> None:
    result = classify(sample(**kw), 50, CODECS)
    assert result.outcome == outcome
    if outcome == 'valid':
        assert result.mos == float(kw.get('mos') or '4.50') and result.quality is not None
    else:
        assert result.mos is None and result.quality is None  # never a zero in place of data


def test_min_packets_is_configurable() -> None:
    assert classify(sample(packets='20'), 10, CODECS).outcome == 'valid'
    assert classify(sample(packets='20'), 100, CODECS).outcome == 'short'


@pytest.mark.parametrize(
    'value, expected',
    [
        ('PCMU', 'pcmu'),
        ('pcma', 'pcma'),
        (' SPEEX ', 'speex'),
        ('Opus', 'opus'),
        ('G722', 'g722'),
        ('G.723', 'other'),
        ('weird-codec', 'other'),
        ('', 'unknown'),
        (None, 'unknown'),
        ('a' * 500, 'other'),
        ('PCMU,PCMA', 'other'),
        ('5551110000', 'other'),
    ],
)
def test_codec_policy(value: str | None, expected: str) -> None:
    assert normalize_codec(value, CODECS) == expected


@pytest.mark.parametrize(
    'value, expected',
    [
        ('inbound', 'inbound'),
        ('OUTBOUND', 'outbound'),
        ('x', 'other'),
        (None, 'other'),
        ('', 'other'),
        ('sofia/internal/1000@test.local', 'other'),
    ],
)
def test_direction_policy(value: str | None, expected: str) -> None:
    assert normalize_direction(value) == expected


def test_tag_values_are_always_from_a_bounded_set() -> None:
    seen_dir, seen_codec = set(), set()
    for i in range(500):
        s = sample(direction='d%d' % i, codec='c%d' % i)
        r = classify(s, 50, CODECS)
        seen_dir.add(r.direction)
        seen_codec.add(r.codec)
    assert seen_dir == {'other'} and seen_codec == {'other'}
