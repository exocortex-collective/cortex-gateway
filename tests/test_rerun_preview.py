"""`?preview=1` answers "how many would this touch" without touching anything.

Before this, a UI could not ask that question: the only way to find out what
POST /triage/rerun matched was to run it and read the count afterwards, by
which point thousands of rows were queued at a caller-chosen priority.

PREVIEW EXECUTES THE REAL STATEMENT AND ROLLS BACK rather than rewriting it
into a COUNT. The statement carries DISTINCT ON, a NOT EXISTS de-duplication
and ON CONFLICT DO NOTHING; a separate counting query would have to restate
all three and would drift from them on the next edit, reporting a number the
real call does not produce. So the tests below assert the ROLLBACK, not a
query string.
"""

from __future__ import annotations

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
        """It must agree with auth.is_preview, which gates the scoped token.

        If this accepted a value the allowlist rejects -- or vice versa -- the
        UI token could reach the enqueueing form of the route.
        """
        mock_update.return_value = 1
        data = client.post(
            f"/triage/rerun?preview={raw}", json={"gmail_ids": ["19ae00"]}
        ).get_json()
        assert data["preview"] is False

    def test_the_predicate_is_shared_with_auth_not_copied(self):
        """One definition, so the two cannot drift apart."""
        from gateway.auth import is_preview as auth_version
        from gateway.blueprints.triage import is_preview as triage_version

        assert auth_version is triage_version


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
