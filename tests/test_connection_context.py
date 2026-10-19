"""ConnectionContext returns its pool slot even when the rollback fails.

psycopg2 frees a slot only inside putconn, so an exception escaping __exit__
burns one for the life of the process. Ten of those exhaust maxconn (10) and
every route touching the database 500s until the container is restarted --
which `restart: unless-stopped` does not do for a process that is merely
wedged. gateway-fcc1.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import psycopg2
import pytest

from gateway.services.postgres import ConnectionContext


@patch("gateway.services.postgres.put_connection")
@patch("gateway.services.postgres.get_connection")
def test_slot_is_returned_when_rollback_raises(mock_get, mock_put) -> None:
    """The case that leaks: the dead connection fails the query AND the rollback.

    InterfaceError is what psycopg2 raises on a closed connection, which is
    the same condition that usually raised inside the body.
    """
    conn = MagicMock()
    conn.rollback.side_effect = psycopg2.InterfaceError("connection already closed")
    mock_get.return_value = conn

    with pytest.raises(ValueError, match="original"):
        with ConnectionContext():
            raise ValueError("original")

    mock_put.assert_called_once_with(conn)


@patch("gateway.services.postgres.put_connection")
@patch("gateway.services.postgres.get_connection")
def test_the_body_exception_is_not_replaced(mock_get, mock_put) -> None:
    """A raising rollback used to surface instead of the real failure."""
    conn = MagicMock()
    conn.rollback.side_effect = psycopg2.InterfaceError("connection already closed")
    mock_get.return_value = conn

    with pytest.raises(psycopg2.Error) as seen:
        with ConnectionContext():
            raise psycopg2.OperationalError("server closed the connection unexpectedly")

    assert "server closed the connection" in str(seen.value)


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
