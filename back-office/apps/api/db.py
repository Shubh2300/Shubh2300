"""psycopg3 connection pool for the Back Office API.

The concrete Postgres schema is owned by ``db/schema.sql`` (sibling agent).
This module only opens a connection pool and hands out connections; the SQL
lives in the repository/service layer. Assumed table names referenced by the
API are documented in ``services/*`` and the router modules.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import get_settings

_pool: Optional[ConnectionPool] = None


def init_pool() -> ConnectionPool:
    """Create the global connection pool (idempotent)."""
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=10,
            open=True,
            kwargs={"row_factory": dict_row},
        )
    return _pool


def get_pool() -> ConnectionPool:
    if _pool is None:
        return init_pool()
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def get_connection() -> Iterator[Connection]:
    """Yield a pooled connection. Commits on clean exit, rolls back on error."""
    pool = get_pool()
    with pool.connection() as conn:
        yield conn
