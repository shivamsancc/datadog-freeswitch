# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Validation and classification of one post-call measurement, and tag normalization. No I/O."""

from __future__ import annotations

import math
from typing import NamedTuple

from .events import RawSample

OUTCOMES = ('valid', 'no_media', 'short', 'missing_fields', 'invalid_value', 'non_finite', 'out_of_range')
DIRECTIONS = ('inbound', 'outbound')
MOS_RANGE = (1.0, 5.0)  # FreeSWITCH reports 0.99 to 4.50 in the evidence; below 0.5 or above 5 is not a MOS
QUALITY_RANGE = (0.0, 100.0)
MOS_FLOOR = 0.5


class Result(NamedTuple):
    outcome: str
    mos: float | None
    quality: float | None
    direction: str
    codec: str


def normalize_direction(value: str | None) -> str:
    return value.lower() if value and value.lower() in DIRECTIONS else 'other'


def normalize_codec(value: str | None, allowed: tuple[str, ...]) -> str:
    """Allowlisted lowercase codec name, ``unknown`` if absent, ``other`` for any value outside the allowlist."""
    if not value:
        return 'unknown'
    name = value.strip().lower()
    return name if name in allowed else 'other'


def _number(text: str) -> float | None:
    """A float from text, or None if it is not a number at all (finite or not)."""
    try:
        return float(text)
    except ValueError:
        return None


def classify(sample: RawSample, min_packets: int, codecs: tuple[str, ...]) -> Result:
    """Decide what to do with one event. A measurement is emitted only for outcome ``valid``.

    Order: packets decide whether media existed; only with enough media are MOS and quality required to be finite and in
    range. Missing or unusable data is never replaced by 0.
    """
    direction = normalize_direction(sample.direction)
    codec = normalize_codec(sample.codec, codecs)

    def result(outcome: str, mos: float | None = None, quality: float | None = None) -> Result:
        return Result(outcome, mos, quality, direction, codec)

    if sample.packets is None:
        return result('missing_fields')
    try:
        packets = int(sample.packets)
    except ValueError:
        return result('invalid_value')
    if packets < 0:
        return result('invalid_value')
    if packets == 0:
        return result('no_media')
    if packets < min_packets:
        return result('short')
    if sample.mos is None or sample.quality is None:
        return result('missing_fields')
    mos, quality = _number(sample.mos), _number(sample.quality)
    if mos is None or quality is None:
        return result('invalid_value')
    if not (math.isfinite(mos) and math.isfinite(quality)):
        return result('non_finite')
    if not (MOS_FLOOR <= mos <= MOS_RANGE[1] and QUALITY_RANGE[0] <= quality <= QUALITY_RANGE[1]):
        return result('out_of_range')
    return result('valid', mos, quality)
