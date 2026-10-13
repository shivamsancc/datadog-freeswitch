# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""A separate FreeSWITCH RTP quality listener (research prototype).

Collects post-call MOS and quality percentage from ``CHANNEL_HANGUP_COMPLETE`` events and reports bounded-cardinality
distributions and its own health through DogStatsD. It is independent of the polling integration in ``integration/``.
"""

__version__ = '0.1.0'
