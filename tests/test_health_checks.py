"""Health-check failures log the exception type, never its message."""

from unittest.mock import patch

import httpx
import structlog

from gateway import app as app_module

SECRET = "host=db.internal password=hunter2"


def test_postgres_failure_logs_type_not_message():
    with (
        patch.object(app_module, "ConnectionContext", side_effect=RuntimeError(SECRET)),
        structlog.testing.capture_logs() as logs,
    ):
        assert app_module.check_postgres() == ("postgres", False)
    assert [e["error_type"] for e in logs] == ["RuntimeError"]
    assert SECRET not in repr(logs)


def test_duckdb_failure_logs_type_not_message():
    with (
        patch.object(app_module.httpx, "get", side_effect=httpx.ConnectError(SECRET)),
        structlog.testing.capture_logs() as logs,
    ):
        assert app_module.check_duckdb() == ("duckdb", False)
    assert [e["error_type"] for e in logs] == ["ConnectError"]
    assert SECRET not in repr(logs)
