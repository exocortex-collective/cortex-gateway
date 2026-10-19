"""The pool slot is returned on every path out of a connection (gateway-fcc1)."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import psycopg2
import pytest
from psycopg2 import extensions, pool

import gateway.services.postgres as pg
from gateway.app import check_postgres
from gateway.services.postgres import ConnectionContext

DEAD = psycopg2.InterfaceError("connection already closed")


class FakeConn:
    """Enough connection for psycopg2's pool bookkeeping to run for real."""

    def __init__(self, rollback_raises: bool = False) -> None:
        self.closed = 0
        self.info = MagicMock(transaction_status=extensions.TRANSACTION_STATUS_INTRANS)
        self._rollback_raises = rollback_raises

    def rollback(self) -> None:
        if self._rollback_raises:
            raise DEAD

    def close(self) -> None:
        self.closed = 1

    @contextmanager
    def cursor(self, *a, **kw):
        yield MagicMock()


@pytest.fixture
def real_pool(monkeypatch):
    """A genuine ThreadedConnectionPool over fake connections.

    The leak lives in psycopg2's own bookkeeping -- putconn is the only thing
    that removes the key from _used -- so a test that patches put_connection
    proves the guard called a stub and nothing more. This exercises the real
    accounting without a database.
    """
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **kw: FakeConn(rollback_raises=True))
    p = pool.ThreadedConnectionPool(0, 2, "fake-dsn")
    monkeypatch.setattr(pg, "_pool", p)
    return p


def test_a_raising_rollback_does_not_consume_the_pool(real_pool) -> None:
    """Past maxconn failures, because the slot is lost for the process.

    On the unguarded version _used grows by one per failure and the third
    getconn raises PoolError: connection pool exhausted.
    """
    for _ in range(5):
        with pytest.raises(ValueError):
            with ConnectionContext():
                raise ValueError("boom")

    assert len(real_pool._used) == 0


def test_the_health_check_does_not_consume_the_pool(real_pool) -> None:
    """/health is unauthenticated, so this one needed only ten GETs."""
    for _ in range(5):
        assert check_postgres() == ("postgres", True)

    assert len(real_pool._used) == 0


@patch("gateway.services.postgres.put_connection")
@patch("gateway.services.postgres.get_connection")
def test_the_body_exception_survives_a_raising_rollback(mock_get, mock_put) -> None:
    conn = MagicMock()
    conn.rollback.side_effect = DEAD
    mock_get.return_value = conn

    with pytest.raises(psycopg2.Error) as seen:
        with ConnectionContext():
            raise psycopg2.OperationalError("server closed the connection unexpectedly")

    assert "server closed the connection" in str(seen.value)
    mock_put.assert_called_once_with(conn)


@patch("gateway.services.postgres.logger")
@patch("gateway.services.postgres.put_connection")
@patch("gateway.services.postgres.get_connection")
def test_the_failure_is_logged_without_the_message(mock_get, mock_put, mock_logger) -> None:
    """error_type only: psycopg2 messages carry DETAIL lines holding row values."""
    conn = MagicMock()
    conn.rollback.side_effect = DEAD
    mock_get.return_value = conn

    with pytest.raises(ValueError):
        with ConnectionContext():
            raise ValueError("boom")

    mock_logger.warning.assert_called_once_with("rollback_failed", error_type="InterfaceError")


@patch("gateway.services.postgres.put_connection")
@patch("gateway.services.postgres.get_connection")
def test_an_exception_still_rolls_back(mock_get, mock_put) -> None:
    """The guard must not become a reason to skip the rollback."""
    conn = MagicMock()
    mock_get.return_value = conn

    with pytest.raises(ValueError):
        with ConnectionContext():
            raise ValueError("boom")

    conn.rollback.assert_called_once()
    mock_put.assert_called_once_with(conn)


@patch("gateway.services.postgres.put_connection")
@patch("gateway.services.postgres.get_connection")
def test_a_clean_exit_does_not_roll_back(mock_get, mock_put) -> None:
    conn = MagicMock()
    mock_get.return_value = conn

    with ConnectionContext() as c:
        assert c is conn

    conn.rollback.assert_not_called()
    mock_put.assert_called_once_with(conn)
