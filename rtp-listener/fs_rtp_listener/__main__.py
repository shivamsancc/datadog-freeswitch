# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Command line entry point: ``python -m fs_rtp_listener``.

Configuration comes from ``FS_RTP_*`` environment variables.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from .config import Config, ConfigError
from .listener import Listener


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s', stream=sys.stderr)
    try:
        config = Config.from_env()
    except ConfigError as error:
        logging.getLogger('fs_rtp_listener').error('invalid configuration: %s', error)
        return 2
    listener = Listener(config)
    done = threading.Event()

    def handle(signum: int, _frame: object) -> None:
        done.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    logging.getLogger('fs_rtp_listener').info('starting: %r', config)
    listener.start()
    done.wait()
    clean = listener.stop()
    logging.getLogger('fs_rtp_listener').info('stopped (clean=%s)', clean)
    return 0 if clean else 1


if __name__ == '__main__':
    raise SystemExit(main())
