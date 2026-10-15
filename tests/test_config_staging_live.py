"""Live-Postgres tests for staging and activating a config (cortex-0xdh).

The routes run against the deployed schema (tests/fixtures/), because version
numbering and the one-active rule live in triggers and the reload signal is a
row in worker_signals.

Skipped unless CORTEX_TEST_DSN points at a throwaway Postgres; CI starts one.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from cortex_utils.triage_config import import_yaml_to_db
from flask import Flask
from flask.testing import FlaskClient

import gateway.services.postgres as pg
from gateway.blueprints.config import config_bp

DSN = os.environ.get("CORTEX_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CORTEX_TEST_DSN to a throwaway Postgres to run these"
)

SCHEMA = (Path(__file__).parent / "fixtures" / "triage_config_schema.sql").read_text()

HEADERS = {"X-Created-By": "test"}


def _config(label: str) -> str:
    return f"""
version: 1
label_prefix: Cortex
default_label: Uncategorized
chains:
  main:
    auto_run: true
    rules:
      - match: {{from_contains: example.com}}
        action: {{label: {label}}}
"""


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    c = psycopg2.connect(DSN)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS t_gw_stage CASCADE")
        cur.execute("CREATE SCHEMA t_gw_stage")
        cur.execute("SET search_path = t_gw_stage, public")
        cur.execute(SCHEMA)
    c.autocommit = False
    import_yaml_to_db(c, _config("Live"), "seed")
    monkeypatch.setattr(pg, "get_connection", lambda: c)
    monkeypatch.setattr(pg, "put_connection", lambda _c: None)
    try:
        yield c
    finally:
        c.rollback()
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS t_gw_stage CASCADE")
        c.close()


@pytest.fixture
def client(conn: Any) -> FlaskClient:
    app = Flask("test")
    app.register_blueprint(config_bp, url_prefix="/config")
    return app.test_client()


def _statuses(client: FlaskClient) -> dict[int, str]:
    versions = client.get("/config/versions").get_json()["versions"]
    return {v["version"]: v["status"] for v in versions}


def _signalled(conn: Any) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT (payload->>'version')::int FROM worker_signals ORDER BY id")
        return [r[0] for r in cur.fetchall()]


def _stage(client: FlaskClient, label: str) -> int:
    r = client.put(
        "/config?activate=false", data=_config(label), headers=HEADERS, content_type="text/plain"
    )
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["active"] is False
    return int(r.get_json()["version"])


def test_staging_stores_an_inactive_version_and_signals_nothing(
    client: FlaskClient, conn: Any
) -> None:
    staged = _stage(client, "Next")
    assert _statuses(client) == {1: "active", staged: "staged"}
    assert _signalled(conn) == []


def test_activating_a_staged_version_creates_and_signals_a_new_one(
    client: FlaskClient, conn: Any
) -> None:
    staged = _stage(client, "Next")
    r = client.post(f"/config/activate/{staged}", headers=HEADERS)
    assert r.status_code == 201, r.get_json()
    new = r.get_json()["new_version"]
    assert new > staged
    assert _statuses(client) == {1: "superseded", staged: "superseded", new: "active"}
    assert _signalled(conn) == [new]
    exported = client.get("/config").get_data(as_text=True)
    assert "label: Next" in exported


def test_activating_the_active_version_is_refused(client: FlaskClient) -> None:
    assert client.post("/config/activate/1", headers=HEADERS).status_code == 409


def test_activating_a_missing_version_is_404(client: FlaskClient) -> None:
    assert client.post("/config/activate/99", headers=HEADERS).status_code == 404


def test_activate_must_be_true_or_false(client: FlaskClient) -> None:
    r = client.put(
        "/config?activate=no", data=_config("Next"), headers=HEADERS, content_type="text/plain"
    )
    assert r.status_code == 400


def test_a_plain_put_still_activates_and_signals(client: FlaskClient, conn: Any) -> None:
    r = client.put("/config", data=_config("Next"), headers=HEADERS, content_type="text/plain")
    assert r.status_code == 201
    version = r.get_json()["version"]
    assert _statuses(client)[version] == "active"
    assert _signalled(conn) == [version]
