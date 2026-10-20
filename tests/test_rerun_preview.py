"""`?preview=1` answers "how many would this touch" without touching anything."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from gateway.blueprints.triage import triage_bp


@pytest.fixture
def client():
    app = Flask("test")
    app.register_blueprint(triage_bp, url_prefix="/triage")
    return app.test_client()


@pytest.fixture
def conn():
    with patch("gateway.blueprints.triage.ConnectionContext") as ctx:
        fake = MagicMock()
        fake.cursor.return_value.__enter__.return_value.rowcount = 412
        ctx.return_value.__enter__.return_value = fake
        ctx.return_value.__exit__.return_value = None
        yield fake


class TestPreview:
    def test_preview_rolls_back_and_reports_the_count(self, conn, client):
        r = client.post("/triage/rerun?preview=1", json={"senders": ["*@example.com"]})
        assert r.status_code == 200
        data = r.get_json()
        assert data["preview"] is True
        assert data["count"] == 412
        assert "Would enqueue" in data["message"]
        assert conn.rollback.called
        assert not conn.commit.called

    def test_preview_executes_the_same_statement_the_real_path_would(self, conn, client):
        """The count is exact only if the preview runs the REAL statement.

        Substring checks are not enough on their own -- a dummy statement
        carrying the same two substrings, reversed parameters, a truncated
        tuple and a doubled execute all satisfy them. So the statement and
        its parameters are compared against what the non-preview path builds
        for the same request, and against literal expected parameters, since
        parity alone cannot catch a defect in the shared builder.
        """
        body = {"senders": ["*@example.com"], "days": 3, "priority": -50}
        # The senders branch derives a cutoff from now(), so the two requests
        # would otherwise differ by the microseconds between them.
        frozen = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)
        with patch("gateway.blueprints.triage.datetime") as dt:
            dt.now.return_value = frozen
            client.post("/triage/rerun?preview=1", json=body)
            cur = conn.cursor.return_value.__enter__.return_value
            assert cur.execute.call_count == 1, "exactly once: twice would double the count"
            preview_sql, preview_params = cur.execute.call_args[0]

            with patch("gateway.blueprints.triage.postgres.execute_update") as real:
                real.return_value = 0
                client.post("/triage/rerun", json=body)
            real_sql, real_params = real.call_args[0]

        assert preview_sql == real_sql
        assert preview_params == real_params

        # Parity alone cannot catch a defect in the shared builder: both sides
        # come from it, so timedelta(days + 1) or a mangled label param passes
        # an equality check. The expected params are therefore spelled out.
        assert preview_params == (
            -50,
            (frozen - timedelta(days=3)).isoformat(),
            "%@example.com",
        ), "priority, the days cutoff, and the sender glob as a LIKE pattern"
        assert "FROM emails_raw er" in preview_sql
        assert "JOIN emails_parsed ep" in preview_sql
        assert "DISTINCT ON (er.gmail_id)" in preview_sql
        assert "NOT EXISTS" in preview_sql, "force is false, so the dedup applies"

    def test_the_label_branch_binds_priority_cutoff_and_label(self, conn, client):
        """Each branch builds its own parameters, so each needs pinning.

        Parity with the non-preview path cannot catch these: both sides come
        from the same builder, so a mangled label or an off-by-one cutoff
        satisfies an equality check while the preview reports a count for
        mail the real call would not touch.
        """
        frozen = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)
        with patch("gateway.blueprints.triage.datetime") as dt:
            dt.now.return_value = frozen
            client.post(
                "/triage/rerun?preview=1",
                json={"label": "Cortex/Uncategorized", "days": 3, "priority": -50},
            )
        cur = conn.cursor.return_value.__enter__.return_value
        sql, params = cur.execute.call_args[0]
        assert params == (
            -50,
            (frozen - timedelta(days=3)).isoformat(),
            "Cortex/Uncategorized",
        ), "priority, the days cutoff, and the label exactly as given"
        assert "JOIN classifications c" in sql

    def test_the_gmail_ids_branch_binds_priority_then_every_id(self, conn, client):
        """A truncated id list would preview a count for mail it never queues."""
        client.post(
            "/triage/rerun?preview=1",
            json={"gmail_ids": ["19ae00", "19ae01", "19ae02"], "priority": -50},
        )
        cur = conn.cursor.return_value.__enter__.return_value
        sql, params = cur.execute.call_args[0]
        assert params == (-50, "19ae00", "19ae01", "19ae02")
        assert sql.count("%s") == 4, "one for priority, one per id"

    def test_a_sender_is_escaped_before_it_becomes_a_like_pattern(self, conn, client):
        """Only `*` is a wildcard; LIKE's own metacharacters are escaped.

        Unescaped, `a_b@example.com` matches `aXb@example.com` and a `%`
        matches any run of characters, so a rerun aimed at one sender would
        re-triage mail from others.
        """
        client.post(
            "/triage/rerun?preview=1",
            json={"senders": ["a_b%c@example.com", "*@spam.example"]},
        )
        cur = conn.cursor.return_value.__enter__.return_value
        params = cur.execute.call_args[0][1]
        assert params[-2:] == (r"a\_b\%c@example.com", "%@spam.example")

    @patch("gateway.blueprints.triage.logger")
    def test_preview_is_distinguishable_from_a_real_enqueue_in_the_log(self, log, conn, client):
        """An operator reading a count needs to know whether rows landed."""
        client.post("/triage/rerun?preview=1", json={"senders": ["*@example.com"]})
        events = [c for c in log.info.call_args_list if c.args[0] == "triage_rerun"]
        assert len(events) == 1
        # Every field, not just preview and count: an operator reading this
        # line is reconstructing which mail a rerun would touch, so a wrong
        # priority or an inverted force is a wrong answer.
        assert events[0].kwargs == {
            "preview": True,
            "count": 412,
            "label": None,
            "senders": ["*@example.com"],
            "gmail_ids": None,
            "force": False,
            "priority": -100,
        }

    @patch("gateway.blueprints.triage.logger")
    @patch("gateway.blueprints.triage.postgres.execute_update")
    def test_a_real_enqueue_is_logged_as_not_a_preview(self, mock_update, log, client):
        mock_update.return_value = 9
        client.post("/triage/rerun", json={"senders": ["*@example.com"]})
        events = [c for c in log.info.call_args_list if c.args[0] == "triage_rerun"]
        assert len(events) == 1
        assert events[0].kwargs == {
            "preview": False,
            "count": 9,
            "label": None,
            "senders": ["*@example.com"],
            "gmail_ids": None,
            "force": False,
            "priority": -100,
        }

    @patch("gateway.blueprints.triage.postgres.execute_update")
    def test_preview_never_takes_the_committing_path(self, mock_update, conn, client):
        """execute_update is the path that writes for real; preview must not use it."""
        client.post("/triage/rerun?preview=1", json={"senders": ["*@example.com"]})
        assert not mock_update.called

    @patch("gateway.blueprints.triage.postgres.execute_update")
    def test_without_preview_it_really_enqueues(self, mock_update, client):
        mock_update.return_value = 412
        data = client.post("/triage/rerun", json={"senders": ["*@example.com"]}).get_json()
        assert data["preview"] is False
        assert data["count"] == 412
        assert "Enqueued" in data["message"]
        assert mock_update.called

    @pytest.mark.parametrize("raw", ["0", "false", "no", "", "maybe"])
    @patch("gateway.blueprints.triage.postgres.execute_update")
    def test_a_falsey_preview_enqueues_for_real(self, mock_update, raw, client):
        """Only "1", "true" and "yes" withhold the write.

        Reading an unrecognised value as a preview would be the dangerous
        default: the caller asked for a rerun, saw a count, and got nothing.
        """
        mock_update.return_value = 1
        data = client.post(
            f"/triage/rerun?preview={raw}", json={"gmail_ids": ["19ae00"]}
        ).get_json()
        assert data["preview"] is False

    def test_auth_no_longer_carries_a_preview_predicate(self):
        """The gate stopped reading the query string; nothing there should.

        A second definition in auth.py is how the gate and the handler drifted
        apart once before (#40), so assert the module has none at all rather
        than that the two agree.
        """
        import gateway.auth as auth

        assert not hasattr(auth, "is_preview")


class TestParameterValidation:
    @pytest.mark.parametrize(
        "days,why",
        [
            ("7", "a string reached timedelta(days='7') and 500'd"),
            (True, "bool is an int in python; days=True meant one day"),
            (0, "zero days is not a window"),
            (-1, "negative"),
            (1.5, "float"),
        ],
    )
    def test_bad_days_is_400_not_500(self, days, why, client):
        r = client.post("/triage/rerun", json={"senders": ["a@b.com"], "days": days})
        assert r.status_code == 400, why
        assert "days" in r.get_json()["error"]

    @pytest.mark.parametrize("priority", ["high", True, 1.5, None])
    def test_bad_priority_is_400(self, priority, client):
        r = client.post("/triage/rerun", json={"senders": ["a@b.com"], "priority": priority})
        assert r.status_code == 400
        assert "priority" in r.get_json()["error"]

    @patch("gateway.blueprints.triage.postgres.execute_update")
    def test_valid_values_still_pass(self, mock_update, client):
        mock_update.return_value = 0
        r = client.post(
            "/triage/rerun",
            json={"senders": ["a@b.com"], "days": 30, "priority": -200},
        )
        assert r.status_code == 200
