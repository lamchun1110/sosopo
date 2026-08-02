"""Dedicated Sosopo delivery worker.

Run this separately from the web process. For production, use PostgreSQL so the
worker can be scaled safely after the job-claiming backend is upgraded to use
row-level locking.
"""

import logging

from server import scheduler, setup_database


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    setup_database()
    scheduler()
