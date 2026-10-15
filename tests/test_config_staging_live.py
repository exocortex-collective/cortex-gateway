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

import gateway.blueprints.config as config_module
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
priority_email_mappings:
  vip@example.com: {{label: VIP}}
fallback_email_mappings:
  news@example.com: {{label: News}}
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
    monkeypatch.setattr(pg, "get_connection", lambda: c)
    # The real pool rolls back an open transaction on putconn, so anything a
    # route did not commit is gone before the test looks.
    monkeypatch.setattr(pg, "put_connection", lambda c_: c_.rollback())
    try:
        import_yaml_to_db(c, _config("Live"), "seed")
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
    assert r.get_json()["message"] == "Config staged"
    assert r.get_json()["active"] is False
    return int(r.get_json()["version"])


def test_staging_stores_an_inactive_version_and_signals_nothing(
    client: FlaskClient, conn: Any
) -> None:
    staged = _stage(client, "Next")
    assert _statuses(client) == {1: "active", staged: "staged"}
    assert _signalled(conn) == []
    with conn.cursor() as cur:
        cur.execute("SELECT label FROM triage_email_mappings")
        assert sorted(cur.fetchall()) == [("News",), ("VIP",)]


def test_activating_a_staged_version_creates_and_signals_a_new_one(
    client: FlaskClient, conn: Any
) -> None:
    staged = _stage(client, "Next")
    r = client.post(f"/config/activate/{staged}", headers=HEADERS)
    assert r.status_code == 201, r.get_json()
    new = r.get_json()["new_version"]
    assert new > staged
    assert r.get_json()["message"] == f"Activated version {staged}"
    assert r.get_json()["created_by"] == "test"
    assert _statuses(client) == {1: "superseded", staged: "superseded", new: "active"}
    assert _signalled(conn) == [new]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolled_back_from, notes FROM triage_config_versions WHERE version = %s",
            (new,),
        )
        assert cur.fetchone() == (staged, f"Activate staged version {staged}")
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
    r = client.put(
        "/config",
        data=_config("Next"),
        headers={**HEADERS, "X-Notes": "why"},
        content_type="text/plain",
    )
    assert r.status_code == 201
    version = r.get_json()["version"]
    assert r.get_json() == {
        "message": "Config created successfully",
        "version": version,
        "active": True,
        "created_by": "test",
        "notes": "why",
    }
    with conn.cursor() as cur:
        cur.execute(
            "SELECT created_by, notes FROM triage_config_versions WHERE version = %s", (version,)
        )
        assert cur.fetchone() == ("test", "why")
    assert _statuses(client)[version] == "active"
    assert _signalled(conn) == [version]


def _restage(client: FlaskClient, label: str) -> Any:
    r = client.put(
        "/config?activate=false", data=_config(label), headers=HEADERS, content_type="text/plain"
    )
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_restaging_stored_content_stages_nothing_and_says_what_matched(
    client: FlaskClient,
) -> None:
    assert _restage(client, "Live") == {
        "message": "Identical to active version 1; nothing staged",
        "version": 1,
        "status": "active",
    }
    staged = _stage(client, "Next")
    assert _restage(client, "Next")["status"] == "staged"
    client.post(f"/config/activate/{staged}", headers=HEADERS)
    old = _stage(client, "Old")
    client.put("/config", data=_config("Newer"), headers=HEADERS, content_type="text/plain")
    assert _restage(client, "Old") == {
        "message": f"Identical to superseded version {old}; nothing staged",
        "version": old,
        "status": "superseded",
    }


def test_activating_content_that_is_already_live_changes_nothing(
    client: FlaskClient, conn: Any
) -> None:
    staged = _stage(client, "Next")
    live = client.post(f"/config/activate/{staged}", headers=HEADERS).get_json()["new_version"]
    r = client.post(f"/config/activate/{staged}", headers=HEADERS)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["active_version"] == live
    assert _signalled(conn) == [live]


@pytest.mark.parametrize(
    ("route", "operation"),
    [("/config/activate/2", "Activation"), ("/config/rollback/1", "Rollback")],
)
def test_a_failed_reimport_names_its_operation(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch, route: str, operation: str
) -> None:
    _stage(client, "Next")

    def boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(config_module, "import_yaml_to_db", boom)
    r = client.post(route, headers=HEADERS)
    assert r.status_code == 500
    assert r.get_json() == {"error": f"{operation} failed"}


@pytest.mark.parametrize("route", ["/config/activate/1", "/config/rollback/1"])
def test_reimport_routes_require_created_by(client: FlaskClient, route: str) -> None:
    _stage(client, "Next")
    client.post("/config/activate/2", headers=HEADERS)
    assert client.post(route).status_code == 400


def test_rollback_reimports_and_records_where_from(client: FlaskClient, conn: Any) -> None:
    staged = _stage(client, "Next")
    active = client.post(f"/config/activate/{staged}", headers=HEADERS).get_json()["new_version"]
    r = client.post("/config/rollback/1", headers=HEADERS)
    assert r.status_code == 201
    body = r.get_json()
    assert body["message"] == "Rolled back to version 1" and body["rolled_back_from"] == 1
    new = body["new_version"]
    assert new > active and _statuses(client)[new] == "active"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolled_back_from, notes FROM triage_config_versions WHERE version = %s",
            (new,),
        )
        assert cur.fetchone() == (1, "Rollback to version 1")
    assert "label: Live" in client.get("/config").get_data(as_text=True)


@pytest.mark.parametrize("route", ["/config/activate/2", "/config/rollback/1"])
def test_x_notes_overrides_the_default(client: FlaskClient, conn: Any, route: str) -> None:
    _stage(client, "Next")
    if route.endswith("/1"):
        client.post("/config/activate/2", headers=HEADERS)
    r = client.post(route, headers={**HEADERS, "X-Notes": "because"})
    assert r.status_code == 201, r.get_json()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT notes FROM triage_config_versions WHERE version = %s",
            (r.get_json()["new_version"],),
        )
        assert cur.fetchone()[0] == "because"


def test_rollback_to_a_missing_version_is_404(client: FlaskClient) -> None:
    assert client.post("/config/rollback/99", headers=HEADERS).status_code == 404


def test_a_stored_config_that_no_longer_validates_is_reported(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage(client, "Next")

    def invalid(*_a: Any, **_k: Any) -> int:
        raise ValueError("no longer valid")

    monkeypatch.setattr(config_module, "import_yaml_to_db", invalid)
    r = client.post("/config/activate/2", headers=HEADERS)
    assert r.status_code == 500
    assert r.get_json() == {
        "error": "Failed to import config from version 2 as it is no longer valid."
    }


def test_with_no_active_version_a_stored_one_lists_as_staged(
    client: FlaskClient, conn: Any
) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE triage_config_versions SET is_active = FALSE")
    conn.commit()
    assert _statuses(client) == {1: "staged"}


def test_staging_an_edited_export_round_trips_the_mappings(client: FlaskClient) -> None:
    exported = client.get("/config").get_data(as_text=True)
    edited = exported.replace("label: Live", "label: Edited", 1)
    r = client.put(
        "/config?activate=false", data=edited, headers=HEADERS, content_type="text/plain"
    )
    assert r.status_code == 201, r.get_json()


def test_mapping_changes_cannot_be_staged(client: FlaskClient) -> None:
    changed = _config("Live").replace("{label: VIP}", "{label: X}")
    r = client.put(
        "/config?activate=false", data=changed, headers=HEADERS, content_type="text/plain"
    )
    assert r.status_code == 400
    assert "/mappings" in r.get_json()["error"]
    assert _statuses(client) == {1: "active"}


def test_with_no_active_version_restaging_and_activating_work(
    client: FlaskClient, conn: Any
) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE triage_config_versions SET is_active = FALSE")
    conn.commit()
    assert _restage(client, "Live")["status"] == "staged"
    r = client.post("/config/activate/1", headers=HEADERS)
    assert r.status_code == 201, r.get_json()
    assert _statuses(client)[r.get_json()["new_version"]] == "active"


VIP = "vip@example.com: {label: VIP}"
NEWS = "news@example.com: {label: News}"
FALLBACK = "\nfallback_email_mappings:\n  "


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (VIP, "vip@example.com: {label: Other}"),
        (NEWS, "news@example.com: {label: Other}"),
        (VIP, "vip@example.com: {label: VIP, archive: true}"),
        (VIP, "vip@example.com: {label: VIP, mark_read: true}"),
        (NEWS, NEWS + "\n  x@example.com: {label: X}"),
        (VIP + FALLBACK + NEWS, NEWS + FALLBACK + VIP),
    ],
    ids=["priority-label", "fallback-label", "archive", "mark-read", "added", "kind-moved"],
)
def test_the_mapping_guard_sees_each_kind_of_difference(
    client: FlaskClient, old: str, new: str
) -> None:
    changed = _config("Live").replace(old, new)
    assert changed != _config("Live")
    r = client.put(
        "/config?activate=false", data=changed, headers=HEADERS, content_type="text/plain"
    )
    assert r.status_code == 400, r.get_json()


def test_the_mapping_guard_ignores_case_and_soft_deleted_rows(
    client: FlaskClient, conn: Any
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO triage_email_mappings"
            " (mapping_type, email_address, label, created_at, updated_at, deleted_at)"
            " VALUES ('priority', 'gone@example.com', 'Gone', now(), now(), now())"
        )
    conn.commit()
    staged = _config("Next").replace("vip@example.com", "VIP@Example.com")
    r = client.put(
        "/config?activate=false", data=staged, headers=HEADERS, content_type="text/plain"
    )
    assert r.status_code == 201, r.get_json()


def test_a_plain_put_may_change_mappings(client: FlaskClient) -> None:
    changed = _config("Next").replace("{label: VIP}", "{label: Other}")
    r = client.put("/config", data=changed, headers=HEADERS, content_type="text/plain")
    assert r.status_code == 201, r.get_json()
