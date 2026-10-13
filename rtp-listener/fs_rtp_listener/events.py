# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Parsing of one ``CHANNEL_HANGUP_COMPLETE`` event (ESL plain format) down to the few fields the listener keeps.

Everything else in the event (caller and callee data, identifiers, SDP, addresses) is discarded while splitting; no raw
text is returned, stored or logged.
"""

from __future__ import annotations

import urllib.parse
from typing import NamedTuple

F_ID = 'Unique-ID'
F_DIRECTION = 'Call-Direction'
F_TIMESTAMP = 'Event-Date-Timestamp'
F_CODEC = 'variable_rtp_use_codec_name'
F_MOS = 'variable_rtp_audio_in_mos'
F_QUALITY = 'variable_rtp_audio_in_quality_percentage'
F_PACKETS = 'variable_rtp_audio_in_packet_count'
WANTED = frozenset({F_ID, F_DIRECTION, F_TIMESTAMP, F_CODEC, F_MOS, F_QUALITY, F_PACKETS})


class RawSample(NamedTuple):
    """The only data kept from an event. Values are still unvalidated text (or None when absent)."""

    event_id: str | None
    direction: str | None
    codec: str | None
    mos: str | None
    quality: str | None
    packets: str | None
    timestamp_us: int | None


def parse_event_body(body: str) -> RawSample:
    """Split an event body into headers and keep only WANTED ones, URL-decoded.

    The headers end at the first blank line (anything after is the event's own body and is ignored). A line without
    :  is ignored. Raises nothing on odd input; fields that are missing stay None.
    """
    kept: dict[str, str] = {}
    for line in body.split('\n'):
        if not line:
            break
        key, sep, value = line.partition(': ')
        if sep and key in WANTED and key not in kept:
            kept[key] = urllib.parse.unquote(value)
    timestamp: int | None
    try:
        timestamp = int(kept[F_TIMESTAMP]) if F_TIMESTAMP in kept else None
    except ValueError:
        timestamp = None
    return RawSample(
        event_id=kept.get(F_ID),
        direction=kept.get(F_DIRECTION),
        codec=kept.get(F_CODEC),
        mos=kept.get(F_MOS),
        quality=kept.get(F_QUALITY),
        packets=kept.get(F_PACKETS),
        timestamp_us=timestamp,
    )
