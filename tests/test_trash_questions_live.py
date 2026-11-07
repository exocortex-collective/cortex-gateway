"""Live-Postgres tests for the trash_sender question routes (cortex-gco6).

The answer route's writes are the behaviour, so they run against real tables.
Skipped unless CORTEX_TEST_DSN points at a throwaway Postgres; CI starts one.
"""

from __future__ import annotations

import json
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


def test_an_unknown_kind_of_question_is_not_answered_here(client: FlaskClient, conn: Any) -> None:
    _ask(conn, "q-u", "u@x.com", 0, kind="something_new")
    r = client.post("/triage/questions/q-u/answer", json={"scope": "ignore"}, headers=BY)
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


# --- uncategorized_sender / low_confidence (cortex-kaij.4) ---------------------

CONFIG_SCHEMA = (Path(__file__).parent / "fixtures" / "triage_config_schema.sql").read_text()


@pytest.fixture
def sender_q(conn: Any) -> Any:
    """An open uncategorized_sender question for s@x.com with two sampled mails."""
    with conn.cursor() as cur:
        cur.execute(CONFIG_SCHEMA)
        cur.execute(
            "INSERT INTO triage_config_versions (version, config_hash, is_active, created_by)"
            " VALUES (1, 'h', true, 'test')"
        )
        cur.execute("ALTER TABLE emails_parsed ADD COLUMN from_addr TEXT")
        cur.execute("CREATE TABLE emails_raw (id BIGSERIAL, gmail_id TEXT, label_ids TEXT[])")
        cur.execute("CREATE TABLE gmail_labels (id TEXT, name TEXT)")
        cur.execute(
            "INSERT INTO gmail_labels VALUES ('Label_1', 'Cortex/Commercial'),"
            " ('Label_2', 'Cortex/Commercial/Retail'), ('Label_3', 'Family')"
        )
        cur.execute(
            "INSERT INTO emails_parsed VALUES ('s1', 'Hi', now(), 's@x.com'),"
            " ('s2', 'Hi 2', now(), 's@x.com'), ('s3', 'Hi 3', now(), 's@x.com')"
        )
        cur.execute("INSERT INTO emails_raw (gmail_id) VALUES ('s1'), ('s2'), ('s3')")
        cur.execute(
            "INSERT INTO triage_questions (id, kind, from_addr, volume, sample, proposed,"
            " llm_category, confidence, reasoning) VALUES ('q-s', 'uncategorized_sender',"
            " 's@x.com', 3, %s, %s, 'news', 0.4, 'looks like a newsletter')",
            (
                json.dumps([{"gmail_id": "s1"}, {"gmail_id": "s2"}]),
                json.dumps({"label": "Cortex/News", "archive": True, "mark_read": False}),
            ),
        )
    conn.commit()
    return conn


def _answer(client: FlaskClient, body: dict[str, Any]) -> Any:
    return client.post("/triage/questions/q-s/answer", json=body, headers=BY)


def _triage_jobs(conn: Any) -> list[tuple]:
    return _rows(conn, "SELECT payload->>'gmail_id' FROM queue WHERE queue_name = 'triage'")


def test_a_sender_question_lists_its_stored_fields(client: FlaskClient, sender_q: Any) -> None:
    q = next(q for q in client.get("/triage/questions").get_json()["questions"] if q["id"] == "q-s")
    assert (q["kind"], q["from_addr"], q["volume"]) == ("uncategorized_sender", "s@x.com", 3)
    assert [s["gmail_id"] for s in q["sample"]] == ["s1", "s2"]
    assert q["proposed"] == {"label": "Cortex/News", "archive": True, "mark_read": False}
    assert (q["llm_category"], q["reasoning"]) == ("news", "looks like a newsletter")
    assert q["confidence"] == pytest.approx(0.4)


def test_scope_sender_writes_the_mapping_unprefixed(client: FlaskClient, sender_q: Any) -> None:
    r = _answer(
        client,
        {
            "scope": "sender",
            "label": "Cortex/Commercial/Newsletter",
            "archive": True,
            "mark_read": True,
            "mapping_type": "fallback",
        },
    )
    assert r.status_code == 200
    body = r.get_json()
    assert (body["answered"], body["scope"], body["rows_enqueued"]) == ("q-s", "sender", 3)
    assert _rows(
        sender_q,
        "SELECT id, mapping_type, email_address, label, archive, mark_read, created_by"
        " FROM triage_email_mappings",
    ) == [
        (
            body["mapping_id"],
            "fallback",
            "s@x.com",
            "Commercial/Newsletter",
            True,
            True,
            "test@trash",
        )
    ]
    assert _triage_jobs(sender_q) == [("s1",), ("s2",), ("s3",)]
    assert _rows(sender_q, "SELECT signal_type FROM worker_signals") == [("mappings_reload",)]
    assert _rows(
        sender_q, "SELECT status, answer->>'label' FROM triage_questions WHERE id = 'q-s'"
    ) == [("answered", "Commercial/Newsletter")]


def test_scope_sender_needs_a_label_and_valid_fields(client: FlaskClient, sender_q: Any) -> None:
    for body in (
        {"scope": "sender"},
        {"scope": "sender", "label": " "},
        {"scope": "sender", "label": "X", "mapping_type": "other"},
        {"scope": "sender", "label": "X", "archive": "yes"},
        {"scope": "sender", "label": "Cortex/ "},
    ):
        assert _answer(client, body).status_code == 400, body
    assert _rows(sender_q, "SELECT count(*) FROM triage_email_mappings") == [(0,)]
    assert _rows(sender_q, "SELECT status FROM triage_questions WHERE id = 'q-s'") == [("open",)]


def test_an_existing_mapping_is_a_409_and_the_question_stays_open(
    client: FlaskClient, sender_q: Any
) -> None:
    with sender_q.cursor() as cur:
        cur.execute(
            "INSERT INTO triage_email_mappings (mapping_type, email_address, label)"
            " VALUES ('fallback', 's@x.com', 'Family')"
        )
    sender_q.commit()
    assert _answer(client, {"scope": "sender", "label": "X"}).status_code == 409
    assert _rows(sender_q, "SELECT status FROM triage_questions WHERE id = 'q-s'") == [("open",)]
    assert _triage_jobs(sender_q) == []
    # the 409 released its locks: the same question can still be answered
    assert _answer(client, {"scope": "ignore"}).status_code == 200


def test_an_unprefixed_label_is_stored_as_given(client: FlaskClient, sender_q: Any) -> None:
    assert _answer(client, {"scope": "sender", "label": "Family"}).status_code == 200
    assert _rows(sender_q, "SELECT label FROM triage_email_mappings") == [("Family",)]


def test_scope_once_retriages_only_the_sample(client: FlaskClient, sender_q: Any) -> None:
    with sender_q.cursor() as cur:  # s1 is already waiting to be triaged
        cur.execute(
            "INSERT INTO queue (queue_name, payload, status)"
            " VALUES ('triage', '{\"gmail_id\": \"s1\"}', 'pending')"
        )
    sender_q.commit()
    r = _answer(client, {"scope": "once"})
    assert r.status_code == 200
    assert r.get_json()["rows_enqueued"] == 1
    assert _triage_jobs(sender_q) == [("s1",), ("s2",)]
    assert _rows(sender_q, "SELECT count(*) FROM triage_email_mappings") == [(0,)]


def test_a_low_confidence_question_answers_the_same_way(client: FlaskClient, sender_q: Any) -> None:
    with sender_q.cursor() as cur:
        cur.execute("UPDATE triage_questions SET kind = 'low_confidence' WHERE id = 'q-s'")
    sender_q.commit()
    r = _answer(client, {"scope": "sender", "label": "News", "mapping_type": "priority"})
    assert r.status_code == 200
    assert _rows(sender_q, "SELECT mapping_type, label FROM triage_email_mappings") == [
        ("priority", "News")
    ]


def test_a_sender_question_without_a_proposal_lists_the_empty_one(
    client: FlaskClient, sender_q: Any
) -> None:
    with sender_q.cursor() as cur:
        cur.execute("UPDATE triage_questions SET proposed = NULL WHERE id = 'q-s'")
    sender_q.commit()
    questions = client.get("/triage/questions").get_json()["questions"]
    by_id = {q["id"]: q["proposed"] for q in questions}
    assert by_id["q-s"] == {"label": None, "archive": False, "mark_read": False}
    assert by_id["q-a"] is None  # trash_sender keeps no proposal


def test_scope_ignore_writes_nothing(client: FlaskClient, sender_q: Any) -> None:
    r = _answer(client, {"scope": "ignore", "label": "ignored"})
    assert r.get_json() == {
        "answered": "q-s",
        "scope": "ignore",
        "mapping_id": None,
        "rows_enqueued": 0,
    }
    assert _triage_jobs(sender_q) == []
    assert _rows(sender_q, "SELECT count(*) FROM triage_email_mappings") == [(0,)]
    assert _rows(
        sender_q, "SELECT status, answer->>'scope' FROM triage_questions WHERE id = 'q-s'"
    ) == [("answered", "ignore")]
    assert _answer(client, {"scope": "ignore"}).status_code == 404


# --- scope "later" (gateway-b874) ------------------------------------------


def _open_ids(client: FlaskClient) -> tuple[list[str], int, int]:
    body = client.get("/triage/questions").get_json()
    return [q["id"] for q in body["questions"]], body["total"], body["snoozed"]


def test_later_snoozes_without_writing_anything(client: FlaskClient, sender_q: Any) -> None:
    r = _answer(client, {"scope": "later"})
    assert r.status_code == 200
    body = r.get_json()
    assert (body["answered"], body["scope"], body["mapping_id"], body["rows_enqueued"]) == (
        "q-s",
        "later",
        None,
        0,
    )
    days = _rows(
        sender_q,
        "SELECT status, answer->>'by', round(extract(epoch FROM"
        " (answer->>'snoozed_until')::timestamptz - now()) / 86400)"
        " FROM triage_questions WHERE id = 'q-s'",
    )
    assert days == [("open", "test@trash", 7)]
    assert _rows(sender_q, "SELECT count(*) FROM triage_email_mappings") == [(0,)]
    assert _triage_jobs(sender_q) == []
    ids, total, snoozed = _open_ids(client)
    assert "q-s" not in ids and snoozed == 1 and total == len(ids)


@pytest.mark.parametrize(("offset", "hidden"), [("-1 minute", False), ("1 minute", True)])
def test_a_snooze_ends_exactly_when_it_says(
    client: FlaskClient, sender_q: Any, offset: str, hidden: bool
) -> None:
    _answer(client, {"scope": "later"})
    with sender_q.cursor() as cur:
        cur.execute(
            "UPDATE triage_questions SET answer = jsonb_set(answer, '{snoozed_until}',"
            " to_jsonb(now() + %s::interval)) WHERE id = 'q-s'",
            (offset,),
        )
    sender_q.commit()
    ids, _, snoozed = _open_ids(client)
    assert ("q-s" not in ids) is hidden and snoozed == int(hidden)


def test_a_snoozed_question_can_still_be_answered(client: FlaskClient, sender_q: Any) -> None:
    _answer(client, {"scope": "later"})
    assert _answer(client, {"scope": "sender", "label": "Family"}).status_code == 200
    assert _rows(
        sender_q, "SELECT status, answer->>'scope' FROM triage_questions WHERE id = 'q-s'"
    ) == [("answered", "sender")]


def test_later_on_a_trash_question_keeps_the_mail_held(client: FlaskClient, conn: Any) -> None:
    r = client.post("/triage/questions/q-a/answer", json={"scope": "later"}, headers=BY)
    assert r.status_code == 200
    assert _rows(conn, "SELECT status FROM triage_trash_queue WHERE from_addr = 'a@x.com'") == [
        ("pending",),
        ("pending",),
    ]
    assert _labeling(conn) == []
    assert "q-a" not in _open_ids(client)[0]


def test_later_needs_an_open_question(client: FlaskClient, conn: Any) -> None:
    assert (
        client.post("/triage/questions/q-x/answer", json={"scope": "later"}, headers=BY).status_code
        == 404
    )
    client.post("/triage/questions/q-b/answer", json={"scope": "ignore"}, headers=BY)
    assert (
        client.post("/triage/questions/q-b/answer", json={"scope": "later"}, headers=BY).status_code
        == 404
    )


# --- GET /labels and label checks on answers ---------------------------------


@pytest.fixture
def labels_client(sender_q: Any) -> FlaskClient:
    from gateway.blueprints.labels import labels_bp

    with sender_q.cursor() as cur:
        cur.execute(
            "INSERT INTO emails_raw (gmail_id, label_ids) VALUES"
            " ('m1', '{Label_2,INBOX}'), ('m2', '{Label_2}'), ('m3', '{Label_1}')"
        )
        cur.execute(
            "INSERT INTO triage_email_mappings (mapping_type, email_address, label)"
            " VALUES ('fallback', 'p@x.org', 'Social/Politics')"
        )
    sender_q.commit()
    app = Flask("test")
    app.register_blueprint(triage_bp, url_prefix="/triage")
    app.register_blueprint(labels_bp, url_prefix="/labels")
    return app.test_client()


def test_labels_lists_the_managed_tree_with_counts(labels_client: FlaskClient) -> None:
    body = labels_client.get("/labels").get_json()
    assert body["prefix"] == "Cortex"
    assert body["total"] == len(body["labels"]) == 3
    assert body["labels"] == [
        {
            "name": "Cortex/Commercial",
            "id": "Label_1",
            "parent": "Cortex",
            "messages": 1,
            "applied": True,
        },
        {
            "name": "Cortex/Commercial/Retail",
            "id": "Label_2",
            "parent": "Cortex/Commercial",
            "messages": 2,
            "applied": True,
        },
        # named by a live mapping, not yet in Gmail; "Family" is outside the tree
        {
            "name": "Cortex/Social/Politics",
            "id": None,
            "parent": "Cortex/Social",
            "messages": 0,
            "applied": True,
        },
    ]


@pytest.mark.parametrize(
    "label",
    [
        "Cortex/ News",
        "News ",
        " News",
        "A//B",
        "/News",
        "News/",
        "Commercial/Retail/",  # trailing slash under a label that exists
        "Cortex/",
        "Comercial/Retail",  # typo: no such parent
    ],
)
def test_a_malformed_or_orphan_label_is_refused(
    labels_client: FlaskClient, sender_q: Any, label: str
) -> None:
    r = _answer(labels_client, {"scope": "sender", "label": label})
    assert r.status_code == 400, label
    assert r.get_json()["error"]
    assert _rows(
        sender_q, "SELECT count(*) FROM triage_email_mappings WHERE email_address = 's@x.com'"
    ) == [(0,)]


@pytest.mark.parametrize(
    ("label", "stored"),
    [
        ("Cortex/Commercial/Retail", "Commercial/Retail"),  # exists in Gmail
        ("Commercial/Coupons", "Commercial/Coupons"),  # new, parent exists
        ("Social/Politics/Local", "Social/Politics/Local"),  # parent known from a mapping
        ("Politics", "Politics"),  # new directly under the prefix
    ],
)
def test_a_known_label_or_a_child_of_one_is_accepted(
    labels_client: FlaskClient, sender_q: Any, label: str, stored: str
) -> None:
    assert _answer(labels_client, {"scope": "sender", "label": label}).status_code == 200
    assert _rows(
        sender_q, "SELECT label FROM triage_email_mappings WHERE email_address = 's@x.com'"
    ) == [(stored,)]
