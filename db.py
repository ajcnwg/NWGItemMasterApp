"""
Shared Azure SQL connection helper for the Item Master App.

Reads connection details from environment variables (see .env.example) so
credentials never live in source code. Auto-picks whichever SQL Server ODBC
driver is actually installed on this machine, preferring the newest.
"""

import os
import time
import urllib.parse
from contextlib import contextmanager

import pyodbc
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

load_dotenv()

# SQLSTATEs/phrases typical of a connection-level timeout — most commonly an
# Azure SQL serverless database that auto-paused after being idle and hasn't
# finished waking up yet, not a real query/logic error. Safe to retry: this
# always fails before any statement runs, so nothing has partially executed.
TRANSIENT_MARKERS = ("08001", "08S01", "HYT00", "HYT01", "Login timeout expired", "TCP Provider")


def is_transient_connection_error(exc: Exception) -> bool:
    message = str(exc)
    return any(marker in message for marker in TRANSIENT_MARKERS)

PREFERRED_DRIVERS = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "SQL Server",
]


def _pick_driver() -> str:
    installed = pyodbc.drivers()
    for driver in PREFERRED_DRIVERS:
        if driver in installed:
            return driver
    raise RuntimeError(
        "No SQL Server ODBC driver found. Install 'ODBC Driver 18 for SQL "
        "Server' from Microsoft and try again."
    )


def get_engine():
    server = os.environ["DB_SERVER"]
    database = os.environ["DB_NAME"]
    user = os.environ["DB_USER"]
    password = os.environ["DB_PASSWORD"]
    driver = _pick_driver()

    odbc_str = (
        f"Driver={{{driver}}};"
        f"Server=tcp:{server},1433;"
        f"Database={database};"
        f"Uid={user};"
        f"Pwd={password};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )
    params = urllib.parse.quote_plus(odbc_str)
    return create_engine(f"mssql+pyodbc:///?odbc_connect={params}", fast_executemany=True)


def _retry_delays(attempts: int, base_delay: float) -> list:
    return [base_delay * (i + 1) for i in range(attempts - 1)]


@contextmanager
def robust_connect(engine, attempts: int = 4, base_delay: float = 3.0, on_retry=None):
    """Same as `with engine.connect() as conn:`, but retries a transient
    connection-timeout (e.g. a paused Azure SQL serverless database still
    waking up) instead of failing outright. Only retries failures at the
    connection step itself — nothing has run yet at that point, so a retry
    can't duplicate work. A real query error (wrong column, etc.) surfaces
    immediately, unretried. `on_retry(attempt, delay, exc)`, if given, is
    called before each wait (e.g. to show a UI message)."""
    delays = _retry_delays(attempts, base_delay)
    for attempt, delay in enumerate([*delays, None]):
        try:
            with engine.connect() as conn:
                yield conn
                return
        except OperationalError as e:
            if delay is None or not is_transient_connection_error(e):
                raise
            if on_retry:
                on_retry(attempt, delay, e)
            time.sleep(delay)


@contextmanager
def robust_begin(engine, attempts: int = 4, base_delay: float = 3.0, on_retry=None):
    """Same idea as robust_connect, for `with engine.begin() as conn:`
    (a connection plus a transaction). SQLAlchemy rolls back the transaction
    automatically on any exception out of the `with` block, so retrying the
    whole block on a connection-timeout is safe — nothing commits partway."""
    delays = _retry_delays(attempts, base_delay)
    for attempt, delay in enumerate([*delays, None]):
        try:
            with engine.begin() as conn:
                yield conn
                return
        except OperationalError as e:
            if delay is None or not is_transient_connection_error(e):
                raise
            if on_retry:
                on_retry(attempt, delay, e)
            time.sleep(delay)
