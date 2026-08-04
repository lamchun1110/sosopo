"""Dedicated Sosopo delivery worker.

Run this separately from the web process.

Run more than one replica only on PostgreSQL. Claiming is atomic on every
backend, so a second worker never steals a claimed job, but only PostgreSQL
uses SELECT ... FOR UPDATE SKIP LOCKED, which lets parallel workers step over
contended rows instead of serializing on them. On SQLite, extra workers add
lock contention without adding throughput.
"""

import logging

from server import scheduler, setup_database


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    setup_database()
    scheduler()
