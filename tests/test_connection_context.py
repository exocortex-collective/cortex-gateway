"""The pool slot is freed on every path out of a connection (gateway-fcc1)."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import psycopg2
import pytest
from psycopg2 import extensions, pool

import gateway.services.postgres as pg
from gateway.app import check_postgres
from gateway.services.postgres import ConnectionContext, put_connection

DEAD = psycopg2.InterfaceError("connection already closed")

# minconn, transaction_status, closed. minconn matters because psycopg2 only
# takes the branch that rolls back inside putconn while the idle pool is below
# it -- production runs minconn=2, and a fixture at 0 cannot see that path.
POOL_STATES = [
    (0, extensions.TRANSACTION_STATUS_INTRANS, 0),
    (2, extensions.TRANSACTION_STATUS_INTRANS, 0),
    (2, extensions.TRANSACTION_STATUS_UNKNOWN, 0),
    (2, extensions.TRANSACTION_STATUS_INTRANS, 1),
]


class FakeConn:
    """Enough connection for psycopg2's pool bookkeeping to run for real."""

    def __init__(self, status: int, closed: int) -> None:
        self.closed = closed
        self.info = MagicMock(transaction_status=status)

    def rollback(self) -> None:
        raise DEAD

    def close(self) -> None:
        self.closed = 1

    @contextmanager
    def cursor(self, *a, **kw):
        yield MagicMock()


@pytest.fixture(params=POOL_STATES, ids=lambda p: f"minconn{p[0]}-status{p[1]}-closed{p[2]}")
def real_pool(request, monkeypatch):
    """A genuine ThreadedConnectionPool over fake connections.

    The slot is freed only inside putconn, so a test that patches
    put_connection proves the guard called a stub and nothing more.
    """
    minconn, status, closed = request.param
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **kw: FakeConn(status, closed))
    p = pool.ThreadedConnectionPool(minconn, 2, "fake-dsn")
    monkeypatch.setattr(pg, "_pool", p)
    return p


def test_a_raising_rollback_does_not_consume_the_pool(real_pool) -> None:
    """More iterations than maxconn, since a lost slot never comes back."""
    for _ in range(5):
        with pytest.raises(ValueError):
            with ConnectionContext():
                raise ValueError("boom")

    assert len(real_pool._used) == 0


def test_the_body_exception_is_what_propagates(real_pool) -> None:
    """A rollback failing in either place must not replace the real error."""
    with pytest.raises(ValueError, match="boom"):
        with ConnectionContext():
            raise ValueError("boom")


def test_the_health_check_does_not_consume_the_pool(real_pool) -> None:
    """/health takes no credential, so this one needs no failure at all."""
    for _ in range(5):
        assert check_postgres() == ("postgres", True)

    assert len(real_pool._used) == 0


def test_a_raising_putconn_is_retried_with_close(monkeypatch) -> None:
    """putconn's own rollback is unguarded and raises before it frees the key."""
    fake_pool = MagicMock()
    fake_pool.putconn.side_effect = [DEAD, None]
    monkeypatch.setattr(pg, "_pool", fake_pool)
    conn = MagicMock()

    put_connection(conn)

    assert fake_pool.putconn.call_args_list[-1].kwargs == {"close": True}


@patch("gateway.services.postgres.put_connection")
@patch("gateway.services.postgres.get_connection")
def test_the_failure_is_logged_without_the_message(
    mock_get,
    mock_put,
) -> None:
    """error_type only: psycopg2 messages carry DETAIL lines holding row values."""
    conn = MagicMock()
    conn.rollback.side_effect = DEAD
    mock_get.return_value = conn

    with patch("gateway.services.postgres.logger") as mock_logger:
        with pytest.raises(ValueError):
            with ConnectionContext():
                raise ValueError("boom")

    mock_logger.warning.assert_called_once_with("rollback_failed", error_type="InterfaceError")


@patch("gateway.services.postgres.put_connection")
@patch("gateway.services.postgres.get_connection")
def test_a_baseexception_from_the_rollback_still_releases(mock_get, mock_put) -> None:
    """`except Exception` does not catch KeyboardInterrupt; the finally does."""
    conn = MagicMock()
    conn.rollback.side_effect = KeyboardInterrupt
    mock_get.return_value = conn

    with pytest.raises(KeyboardInterrupt):
        with ConnectionContext():
            raise ValueError("boom")

    mock_put.assert_called_once_with(conn)


@patch("gateway.services.postgres.put_connection")
@patch("gateway.services.postgres.get_connection")
def test_a_raising_logger_still_releases(mock_get, mock_put) -> None:
    """The log call sits on the failure path, so it must not gate the release."""
    conn = MagicMock()
    conn.rollback.side_effect = DEAD
    mock_get.return_value = conn

    with patch("gateway.services.postgres.logger") as mock_logger:
        mock_logger.warning.side_effect = RuntimeError("log sink down")
        with pytest.raises(RuntimeError, match="log sink down"):
            with ConnectionContext():
                raise ValueError("boom")

    mock_put.assert_called_once_with(conn)


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
