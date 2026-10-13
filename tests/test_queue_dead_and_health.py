"""Dead letters, stuck jobs and health, which were invisible over HTTP.

This is where cortex loses mail quietly. A job that exhausts its attempts is
archived to `dead_letter` and nothing surfaced it: cortex-b6hd found 1,614
messages carrying no Cortex label, and `action_executed` on a classification
only ever meant "a labeling job was enqueued", never "a label was applied".

The routes are thin shaping over cortex_utils.queue, which is the SAME library
cryo's daemon wraps for its own queue views -- so these are mocked at the
library boundary, and what is asserted is the HTTP contract the extension
builds against: key names, the ref-not-payload rule, clamping, and the
per-item result shape for bulk actions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from flask import Flask

from gateway.blueprints.queue import BULK_MAX, queue_bp

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def client():
    app = Flask("test")
    app.register_blueprint(queue_bp, url_prefix="/queue")
    return app.test_client()


@pytest.fixture
def conn():
    """Patch ConnectionContext so no route touches a real database."""
    with patch("gateway.blueprints.queue.ConnectionContext") as ctx:
        fake = MagicMock()
        ctx.return_value.__enter__.return_value = fake
        ctx.return_value.__exit__.return_value = None
        yield fake


def _depth(name="triage", ready=4, deferred=1, processing=2, failed=3, age=90.0):
    return SimpleNamespace(
        queue_name=name,
        ready=ready,
        deferred=deferred,
        processing=processing,
        failed=failed,
        oldest_ready_age_s=age,
    )


def _health(**kw):
    base = dict(
        depths=[_depth()],
        dead_letter=7,
        partitioned=True,
        partition_headroom_days=5,
        self_healed_partitions=0,
        partition_count=30,
        oldest_partition_age_days=29,
        is_healthy=True,
        server_time=NOW,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _dead_row(dead_id=1, **kw):
    row = {
        "id": dead_id,
        "original_id": 900 + dead_id,
        "queue_name": "triage",
        "payload": {"gmail_id": "19ae00", "email_id": 5},
        "attempts": 3,
        "last_error": "model stopped at the token budget",
        "created_at": NOW,
        "failed_at": NOW,
        "dismissed_at": None,
        "retried_at": None,
        "retried_as": None,
    }
    row.update(kw)
    return row


class TestQueueHealth:
    @patch("gateway.blueprints.queue.queue_inspect.health")
    def test_shape_matches_cryos_api_queue(self, mock_health, conn, client):
        """The extension renders both tabs with one component, so the keys match."""
        mock_health.return_value = _health()
        data = client.get("/queue/health").get_json()

        assert data["queues"] == [
            {
                "queue": "triage",
                "ready": 4,
                "deferred": 1,
                "processing": 2,
                "failed": 3,
                "oldest_ready_age_s": 90.0,
            }
        ]
        assert data["dead_letter"] == 7
        assert data["is_healthy"] is True

    @patch("gateway.blueprints.queue.queue_inspect.health")
    def test_ready_and_deferred_stay_separate(self, mock_health, conn, client):
        """Collapsing them hides a retry storm as an ordinary backlog."""
        mock_health.return_value = _health(depths=[_depth(ready=0, deferred=6)])
        q = client.get("/queue/health").get_json()["queues"][0]
        assert (q["ready"], q["deferred"]) == (0, 6)
        assert "pending" not in q


class TestDeadLetters:
    @patch("gateway.blueprints.queue.DeadLetterManager")
    def test_returns_a_ref_and_never_the_payload(self, mgr, conn, client):
        """A browser extension reads this. gmail_id is all the UI needs."""
        mgr.return_value.list_jobs.return_value = [_dead_row()]
        row = client.get("/queue/dead").get_json()["dead"][0]

        assert row["ref"] == "19ae00"
        assert "payload" not in row
        assert "email_id" not in row
        assert row["error"] == "model stopped at the token budget"
        assert row["queue"] == "triage"

    @patch("gateway.blueprints.queue.DeadLetterManager")
    def test_a_payload_without_gmail_id_gives_a_null_ref(self, mgr, conn, client):
        """Not every producer puts gmail_id there; it must not 500."""
        mgr.return_value.list_jobs.return_value = [_dead_row(payload={"other": 1})]
        assert client.get("/queue/dead").get_json()["dead"][0]["ref"] is None

    @patch("gateway.blueprints.queue.DeadLetterManager")
    def test_a_non_dict_payload_gives_a_null_ref(self, mgr, conn, client):
        mgr.return_value.list_jobs.return_value = [_dead_row(payload=None)]
        assert client.get("/queue/dead").get_json()["dead"][0]["ref"] is None

    @patch("gateway.blueprints.queue.DeadLetterManager")
    def test_limit_is_clamped(self, mgr, conn, client):
        mgr.return_value.list_jobs.return_value = []
        client.get("/queue/dead?limit=100000")
        assert mgr.return_value.list_jobs.call_args.kwargs["limit"] == BULK_MAX

    @patch("gateway.blueprints.queue.DeadLetterManager")
    def test_include_resolved_defaults_off(self, mgr, conn, client):
        mgr.return_value.list_jobs.return_value = []
        client.get("/queue/dead")
        assert mgr.return_value.list_jobs.call_args.kwargs["include_resolved"] is False
        client.get("/queue/dead?include_resolved=true")
        assert mgr.return_value.list_jobs.call_args.kwargs["include_resolved"] is True


class TestStuck:
    @patch("gateway.blueprints.queue.queue_inspect.stuck")
    def test_shape(self, mock_stuck, conn, client):
        mock_stuck.return_value = [
            SimpleNamespace(
                id=12,
                queue_name="triage",
                claimed_by="worker-1",
                claimed_at=NOW,
                stuck_for_s=3600.0,
                attempts=1,
            )
        ]
        row = client.get("/queue/stuck").get_json()["stuck"][0]
        assert row == {
            "id": 12,
            "queue": "triage",
            "claimed_by": "worker-1",
            "claimed_at": "Tue, 13 Oct 2026 12:00:00 GMT",
            "stuck_for_s": 3600.0,
            "attempts": 1,
        }

    @patch("gateway.blueprints.queue.queue_inspect.stuck")
    def test_limit_is_clamped_here_too(self, mock_stuck, conn, client):
        """Both list routes clamp. Pinning only one leaves the other open."""
        mock_stuck.return_value = []
        client.get("/queue/stuck?limit=100000")
        assert mock_stuck.call_args.kwargs["limit"] == BULK_MAX


class TestResponseShapes:
    """The extension reads these keys, so a dropped one breaks the UI silently.

    Measured before this: removing dismissed_at or retried_as from a dead row,
    or limit from the /queue/stuck body, left the whole suite green. The
    contract names them, so they are pinned as a set rather than individually.
    """

    DEAD_KEYS = {
        "id",
        "original_id",
        "queue",
        "ref",
        "attempts",
        "error",
        "created_at",
        "failed_at",
        "dismissed_at",
        "retried_at",
        "retried_as",
    }
    STUCK_KEYS = {"id", "queue", "claimed_by", "claimed_at", "stuck_for_s", "attempts"}

    @patch("gateway.blueprints.queue.DeadLetterManager")
    def test_a_dead_row_carries_exactly_the_contract_keys(self, mgr, conn, client):
        mgr.return_value.list_jobs.return_value = [_dead_row()]
        body = client.get("/queue/dead").get_json()
        assert set(body) == {"dead", "limit", "count"}
        assert set(body["dead"][0]) == self.DEAD_KEYS

    @patch("gateway.blueprints.queue.queue_inspect")
    def test_a_stuck_row_carries_exactly_the_contract_keys(self, qi, conn, client):
        qi.stuck.return_value = [
            SimpleNamespace(
                id=1,
                queue_name="triage",
                claimed_by="worker-1",
                claimed_at=datetime(2026, 10, 13, tzinfo=UTC),
                stuck_for_s=900,
                attempts=1,
            )
        ]
        body = client.get("/queue/stuck").get_json()
        assert set(body) == {"stuck", "limit", "count"}
        assert set(body["stuck"][0]) == self.STUCK_KEYS


class TestPageLimits:
    """Both bounds. Only the upper one was clamped, and only it was tested.

    ?limit=0 and ?limit=-1 reached Postgres as LIMIT 0 / LIMIT -1, the second
    of which it rejects -- a 500 from a query string.
    """

    LIMITS = [("0", 1), ("-1", 1), ("7", 7), ("500", 100)]

    @pytest.mark.parametrize("raw,expected", LIMITS)
    @patch("gateway.blueprints.queue.DeadLetterManager")
    def test_dead_limit_is_clamped_at_both_ends(self, mgr, conn, client, raw, expected):
        mgr.return_value.list_jobs.return_value = []
        client.get(f"/queue/dead?limit={raw}")
        assert mgr.return_value.list_jobs.call_args.kwargs["limit"] == expected

    @pytest.mark.parametrize("raw,expected", LIMITS)
    @patch("gateway.blueprints.queue.queue_inspect")
    def test_stuck_limit_is_clamped_at_both_ends(self, qi, conn, client, raw, expected):
        qi.stuck.return_value = []
        client.get(f"/queue/stuck?limit={raw}")
        assert qi.stuck.call_args.kwargs["limit"] == expected


class TestBulkActions:
    @patch("gateway.blueprints.queue.DeadLetterManager")
    def test_a_mixed_outcome_is_200_with_per_item_results(self, mgr, conn, client):
        """Mixed is the normal case; a 4xx would say nothing about which moved."""
        m = mgr.return_value
        m.retry_job.side_effect = [True, False]
        m.get_job.return_value = {"retried_as": 4242}
        m.why_not_retryable.return_value = "already retried as queue job 99"

        r = client.post("/queue/dead/resubmit", json={"ids": [1, 2]})
        assert r.status_code == 200
        data = r.get_json()
        assert data["resubmitted"] == 1
        assert data["results"][0] == {"id": 1, "ok": True, "new_id": 4242}
        assert data["results"][1]["ok"] is False
        assert "already retried" in data["results"][1]["why"]

    @patch("gateway.blueprints.queue.DeadLetterManager")
    def test_dismiss_counts_and_explains(self, mgr, conn, client):
        mgr.return_value.dismiss.side_effect = [True, False]
        data = client.post("/queue/dead/dismiss", json={"ids": [1, 2]}).get_json()
        assert data["dismissed"] == 1
        assert "no dead letter job with id 2" in data["results"][1]["why"]

    @patch("gateway.blueprints.queue.DeadLetterManager")
    def test_each_requested_id_reaches_the_manager(self, mgr, conn, client):
        """WHICH id, not just how many.

        The other tests drive the manager with side_effect lists, so results
        depend on call order and not on the argument: measured, replacing
        retry_job(dead_id) with retry_job(ids[0]) left the suite at 206. On a
        destructive bulk route the browser token can call, acting on an id the
        caller did not name is the whole risk.
        """
        m = mgr.return_value
        m.retry_job.return_value = True
        m.get_job.return_value = {"retried_as": 1}

        client.post("/queue/dead/resubmit", json={"ids": [7, 8, 9]})
        assert m.retry_job.call_args_list == [call(7), call(8), call(9)]
        assert m.get_job.call_args_list == [call(7), call(8), call(9)]

    @patch("gateway.blueprints.queue.DeadLetterManager")
    def test_a_failed_id_is_explained_for_that_id(self, mgr, conn, client):
        """why_not_retryable must be asked about the id that failed."""
        m = mgr.return_value
        m.retry_job.side_effect = [False, False]
        m.why_not_retryable.return_value = "nope"

        client.post("/queue/dead/resubmit", json={"ids": [7, 8]})
        assert m.why_not_retryable.call_args_list == [call(7), call(8)]

    @patch("gateway.blueprints.queue.DeadLetterManager")
    def test_each_dismissed_id_reaches_the_manager(self, mgr, conn, client):
        mgr.return_value.dismiss.return_value = True
        client.post("/queue/dead/dismiss", json={"ids": [7, 8, 9]})
        assert mgr.return_value.dismiss.call_args_list == [call(7), call(8), call(9)]

    @patch("gateway.blueprints.queue.DeadLetterManager")
    def test_one_raising_id_does_not_discard_the_others(self, mgr, conn, client):
        """The route promises per-item results, so one bad id must cost one id.

        In a single transaction a raise discarded every result and rolled back
        the ids that had already succeeded, turning a partial success into a
        500 with no information.
        """
        m = mgr.return_value
        m.retry_job.side_effect = [True, RuntimeError("boom"), True]
        m.get_job.return_value = {"retried_as": 1}

        r = client.post("/queue/dead/resubmit", json={"ids": [1, 2, 3]})
        assert r.status_code == 200
        data = r.get_json()
        assert [x["ok"] for x in data["results"]] == [True, False, True]
        assert data["resubmitted"] == 2
        assert conn.rollback.called, "the failed id must not poison the transaction"

    @patch("gateway.blueprints.queue.DeadLetterManager")
    def test_both_bulk_routes_commit(self, mgr, conn, client):
        """Without the commit the retry is rolled back by the pool teardown."""
        mgr.return_value.retry_job.return_value = True
        mgr.return_value.get_job.return_value = {"retried_as": 1}
        client.post("/queue/dead/resubmit", json={"ids": [1]})
        assert conn.commit.called
        conn.commit.reset_mock()
        mgr.return_value.dismiss.return_value = True
        client.post("/queue/dead/dismiss", json={"ids": [1]})
        assert conn.commit.called

    @pytest.mark.parametrize(
        "body,why",
        [
            ({}, "no ids"),
            ({"ids": []}, "empty"),
            ({"ids": "1"}, "not a list"),
            ({"ids": ["1"]}, "not integers"),
            ({"ids": [True]}, "bool is an int in python; id 1 is unrelated"),
            ({"ids": list(range(BULK_MAX + 1))}, "over the cap"),
        ],
    )
    @patch("gateway.blueprints.queue.DeadLetterManager")
    def test_a_bad_id_list_is_400_and_touches_nothing(self, mgr, body, why, conn, client):
        for path in ("/queue/dead/resubmit", "/queue/dead/dismiss"):
            r = client.post(path, json=body)
            assert r.status_code == 400, why
            assert "error" in r.get_json()
        assert not mgr.return_value.retry_job.called
        assert not mgr.return_value.dismiss.called
