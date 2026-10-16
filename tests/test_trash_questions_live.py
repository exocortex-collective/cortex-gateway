"""Live-Postgres tests for the trash_sender question routes (cortex-gco6).

The answer route's writes are the behaviour, so they run against real tables.
Skipped unless CORTEX_TEST_DSN points at a throwaway Postgres; CI starts one.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from cortex_utils.queue import ensure_queue_schema
from flask import Flask
from flask.testing import FlaskClient

import gateway.services.postgres as pg
from gateway.blueprints.triage import triage_bp

DSN = os.environ.get("CORTEX_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CORTEX_TEST_DSN to a throwaway Postgres to run these"
)

SCHEMA = (Path(__file__).parent / "fixtures" / "trash_schema.sql").read_text()
BY = {"X-Created-By": "test@trash"}
SCHEMA_NAME = f"t_gw_trash_{uuid.uuid4().hex[:8]}"  # reviewers share one Postgres


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    c = psycopg2.connect(DSN, options=f"-c search_path={SCHEMA_NAME}")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA_NAME}")
        cur.execute(SCHEMA)
        cur.execute(
            "CREATE TABLE emails_parsed (gmail_id TEXT, subject TEXT, date_header TIMESTAMPTZ)"
        )
        cur.execute(
            "INSERT INTO emails_parsed VALUES"
            " ('g1', 'Sale', '2026-10-01'), ('g2', 'Sale 2', '2026-10-02')"
        )
        cur.execute(
            "INSERT INTO triage_trash_queue (gmail_id, from_addr, status) VALUES"
            " ('g1', 'a@x.com', 'pending'), ('g2', 'a@x.com', 'pending'),"
            " ('g3', 'b@x.com', 'cleared')"
        )
        cur.execute(
            "INSERT INTO triage_questions (id, kind, from_addr) VALUES"
            " ('q-a', 'trash_sender', 'a@x.com'), ('q-b', 'trash_sender', 'b@x.com')"
        )
    c.autocommit = False
    ensure_queue_schema(c)
    c.commit()
    monkeypatch.setattr(pg, "get_connection", lambda: c)
    # The real pool rolls back an open transaction on putconn, so anything a
    # route did not commit is gone before the test looks.
    monkeypatch.setattr(pg, "put_connection", lambda c_: c_.rollback())
    try:
        yield c
    finally:
        c.rollback()
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE")
        c.close()


@pytest.fixture
def client(conn: Any) -> FlaskClient:
    app = Flask("test")
    app.register_blueprint(triage_bp, url_prefix="/triage")
    return app.test_client()


def _rows(conn: Any, sql: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql)
        return sorted(cur.fetchall())


def _labeling(conn: Any) -> list[tuple]:
    return _rows(conn, "SELECT payload->>'gmail_id' FROM queue WHERE queue_name = 'labeling'")


def test_open_questions_list_the_held_mail_largest_first(client: FlaskClient) -> None:
    body = client.get("/triage/questions").get_json()
    assert [q["id"] for q in body["questions"]] == ["q-a", "q-b"]
    assert body["total"] == 2
    first = body["questions"][0]
    assert (first["kind"], first["from_addr"], first["volume"]) == (
        "trash_sender",
        "a@x.com",
        2,
    )
    assert {s["gmail_id"] for s in first["sample"]} == {"g1", "g2"}
    assert body["questions"][1]["volume"] == 0


def test_approving_the_sender_releases_its_mail(client: FlaskClient, conn: Any) -> None:
    r = client.post("/triage/questions/q-a/answer", json={"scope": "sender"}, headers=BY)
    assert r.status_code == 200
    assert r.get_json() == {"answered": "q-a", "scope": "sender", "rows_enqueued": 2}
    assert _rows(conn, "SELECT email_address, decision, decided_by FROM triage_trash_senders") == [
        ("a@x.com", "approved", "test@trash")
    ]
    assert _rows(
        conn, "SELECT gmail_id, status FROM triage_trash_queue WHERE from_addr = 'a@x.com'"
    ) == [("g1", "approved"), ("g2", "approved")]
    assert _labeling(conn) == [("g1",), ("g2",)]
    assert _rows(
        conn, "SELECT status, answer->>'scope' FROM triage_questions WHERE id = 'q-a'"
    ) == [("answered", "sender")]


def test_once_releases_the_mail_but_decides_nothing(client: FlaskClient, conn: Any) -> None:
    r = client.post("/triage/questions/q-a/answer", json={"scope": "once"}, headers=BY)
    assert r.get_json()["rows_enqueued"] == 2
    assert _rows(conn, "SELECT * FROM triage_trash_senders") == []
    assert _labeling(conn) == [("g1",), ("g2",)]


def test_ignore_keeps_the_mail_and_rejects_the_sender(client: FlaskClient, conn: Any) -> None:
    r = client.post("/triage/questions/q-a/answer", json={"scope": "ignore"}, headers=BY)
    assert r.get_json()["rows_enqueued"] == 0
    assert _rows(conn, "SELECT decision FROM triage_trash_senders") == [("rejected",)]
    assert _rows(conn, "SELECT status FROM triage_trash_queue WHERE from_addr = 'a@x.com'") == [
        ("kept",),
        ("kept",),
    ]
    assert _labeling(conn) == []


def test_a_question_is_answered_once(client: FlaskClient, conn: Any) -> None:
    client.post("/triage/questions/q-a/answer", json={"scope": "sender"}, headers=BY)
    r = client.post("/triage/questions/q-a/answer", json={"scope": "sender"}, headers=BY)
    assert r.status_code == 404
    assert _labeling(conn) == [("g1",), ("g2",)]


def test_an_answer_never_touches_another_senders_mail(client: FlaskClient, conn: Any) -> None:
    client.post("/triage/questions/q-b/answer", json={"scope": "ignore"}, headers=BY)
    assert _rows(conn, "SELECT gmail_id, status FROM triage_trash_queue") == [
        ("g1", "pending"),
        ("g2", "pending"),
        ("g3", "cleared"),
    ]


@pytest.mark.parametrize(
    ("body", "headers", "status"),
    [
        ({"scope": "sender"}, {}, 400),
        ({"scope": "all"}, BY, 400),
        ({}, BY, 400),
    ],
)
def test_a_bad_answer_changes_nothing(
    client: FlaskClient, conn: Any, body: dict, headers: dict, status: int
) -> None:
    r = client.post("/triage/questions/q-a/answer", json=body, headers=headers)
    assert r.status_code == status
    assert _rows(conn, "SELECT status FROM triage_questions") == [("open",), ("open",)]
    assert _labeling(conn) == []


def test_an_answer_waits_for_a_gate_in_progress(client: FlaskClient) -> None:
    other = psycopg2.connect(DSN)
    try:
        with other.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('triage_trash_gate'))")
        done = threading.Event()
        answer = threading.Thread(
            target=lambda: (
                client.post("/triage/questions/q-a/answer", json={"scope": "once"}, headers=BY),
                done.set(),
            )
        )
        answer.start()
        assert not done.wait(0.5)
        other.commit()
        assert done.wait(5)
        answer.join()
    finally:
        other.close()


def _ask(conn: Any, qid: str, addr: str, held: int, kind: str = "trash_sender") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO triage_questions (id, kind, from_addr) VALUES (%s, %s, %s)",
            (qid, kind, addr),
        )
        for i in range(held):
            cur.execute(
                "INSERT INTO triage_trash_queue (gmail_id, from_addr, status)"
                " VALUES (%s, %s, 'pending')",
                (f"{qid}-{i}", addr),
            )
    conn.commit()


def test_listing_pages_and_caps_the_sample(client: FlaskClient, conn: Any) -> None:
    _ask(conn, "q-c", "c@x.com", 7)
    body = client.get("/triage/questions?limit=1&offset=0").get_json()
    assert [q["id"] for q in body["questions"]] == ["q-c"]
    assert body["questions"][0]["volume"] == 7
    assert len(body["questions"][0]["sample"]) == 5
    assert body["total"] == 3
    page2 = client.get("/triage/questions?limit=1&offset=1").get_json()
    assert [q["id"] for q in page2["questions"]] == ["q-a"]
    assert client.get("/triage/questions?limit=999").get_json()["limit"] == 200


def test_listing_filters_by_status(client: FlaskClient) -> None:
    client.post("/triage/questions/q-a/answer", json={"scope": "once"}, headers=BY)
    answered = client.get("/triage/questions?status=answered").get_json()
    assert [q["id"] for q in answered["questions"]] == ["q-a"]
    assert [q["id"] for q in client.get("/triage/questions").get_json()["questions"]] == ["q-b"]


@pytest.mark.parametrize(
    "query", ["limit=x", "limit=-1", "offset=-1", "offset=99999999999999999999"]
)
def test_a_bad_page_is_a_400(client: FlaskClient, query: str) -> None:
    assert client.get(f"/triage/questions?{query}").status_code == 400


def test_another_kind_of_question_is_not_answered_here(client: FlaskClient, conn: Any) -> None:
    _ask(conn, "q-u", "u@x.com", 0, kind="uncategorized_sender")
    r = client.post("/triage/questions/q-u/answer", json={"scope": "sender"}, headers=BY)
    assert r.status_code == 404
    assert _rows(conn, "SELECT status FROM triage_questions WHERE id = 'q-u'") == [("open",)]


def test_a_sender_can_be_decided_again(client: FlaskClient, conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO triage_trash_senders (email_address, decision, decided_by)"
            " VALUES ('a@x.com', 'rejected', 'earlier')"
        )
    conn.commit()
    client.post("/triage/questions/q-a/answer", json={"scope": "sender"}, headers=BY)
    assert _rows(conn, "SELECT decision, decided_by FROM triage_trash_senders") == [
        ("approved", "test@trash")
    ]


def test_released_mail_is_queued_like_live_mail(client: FlaskClient, conn: Any) -> None:
    client.post("/triage/questions/q-a/answer", json={"scope": "sender"}, headers=BY)
    assert _rows(conn, "SELECT status, priority FROM queue WHERE queue_name = 'labeling'") == [
        ("pending", 0),
        ("pending", 0),
    ]


def test_a_question_has_the_contract_shape(client: FlaskClient, conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE triage_trash_queue SET queued_at = now() - interval '1 day'"
            " WHERE gmail_id = 'g1'"
        )
    conn.commit()
    q = client.get("/triage/questions").get_json()["questions"][0]
    assert [(s["gmail_id"], s["subject"]) for s in q["sample"]] == [
        ("g2", "Sale 2"),
        ("g1", "Sale"),
    ]
    assert q["sample"][0]["date_header"].startswith("2026-10-02")
    assert q["asked_at"][:4].isdigit() and "T" in q["asked_at"]
    assert [q[k] for k in ("proposed", "llm_category", "confidence", "reasoning")] == [None] * 4


def test_equal_volume_asks_oldest_first(client: FlaskClient, conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM triage_trash_queue")
        cur.execute(
            "UPDATE triage_questions SET asked_at = now() - interval '1 day' WHERE id = 'q-b'"
        )
    conn.commit()
    ids = [q["id"] for q in client.get("/triage/questions").get_json()["questions"]]
    assert ids == ["q-b", "q-a"]
