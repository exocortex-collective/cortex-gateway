"""Postgres database service."""

from typing import Any

from cortex_utils.logging import get_logger
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from gateway.config import config

logger = get_logger()

# Connection pool (initialized lazily)
_pool: pool.ThreadedConnectionPool | None = None


def init_pool(minconn: int = 2, maxconn: int = 10) -> None:
    """Initialize the connection pool."""
    global _pool
    if _pool is None:
        _pool = pool.ThreadedConnectionPool(
            minconn,
            maxconn,
            config.postgres_dsn,
        )


def get_connection() -> Any:
    """Get a connection from the pool."""
    if _pool is None:
        init_pool()
    assert _pool is not None
    return _pool.getconn()


def put_connection(conn: Any) -> None:
    """Return a connection to the pool, freeing the slot even if that fails.

    putconn's own rollback can raise before it frees the slot; close=True
    skips that rollback.
    """
    if _pool is None:
        return
    try:
        _pool.putconn(conn)
    except Exception as exc:
        logger.warning("putconn_failed", error_type=type(exc).__name__)
        _pool.putconn(conn, close=True)


class ConnectionContext:
    """Context manager for database connections."""

    def __init__(self) -> None:
        self.conn: Any = None

    def __enter__(self) -> Any:
        self.conn = get_connection()
        return self.conn

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Always release the connection, even if the rollback raises."""
        if self.conn:
            try:
                if exc_type is not None:
                    self.conn.rollback()
            except Exception as exc:
                # error_type only: psycopg2 messages carry DETAIL lines
                # holding row values.
                logger.warning("rollback_failed", error_type=type(exc).__name__)
            finally:
                put_connection(self.conn)


def execute_query(query: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    """Execute a query and return results as list of dicts."""
    with ConnectionContext() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchall()


def execute_update_returning(
    query: str, params: tuple[Any, ...] | None = None
) -> list[dict[str, Any]]:
    """An UPDATE/INSERT/DELETE with a RETURNING clause: commits, then returns rows.

    execute_query() does not commit -- it is for SELECTs -- and ConnectionContext
    only rolls back on an exception, so a write routed through it is rolled back
    by the pool on putconn. The rows come back and the caller sees a success it
    did not get. This is the pairing that makes RETURNING safe to use.
    """
    with ConnectionContext() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            conn.commit()
            return rows


def execute_one(query: str, params: tuple[Any, ...] | None = None) -> dict[str, Any] | None:
    """Execute a query and return single result as dict."""
    results = execute_query(query, params)
    return results[0] if results else None


def execute_update(query: str, params: tuple[Any, ...] | None = None) -> int:
    """Execute an update/insert/delete and return affected row count."""
    with ConnectionContext() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            conn.commit()
            return cur.rowcount
