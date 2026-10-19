"""Tests for queue management API endpoints."""

from unittest.mock import patch

import pytest
from flask import Flask

from gateway.blueprints.queue import queue_bp


@pytest.fixture
def app() -> Flask:
    """Create a Flask app for testing."""
    test_app = Flask("test")
    test_app.register_blueprint(queue_bp, url_prefix="/queue")
    return test_app


@pytest.fixture
def client(app: Flask):
    """Create a test client."""
    return app.test_client()


class TestQueueStats:
    """Tests for GET /queue/stats endpoint."""

    @patch("gateway.blueprints.queue.postgres.execute_query")
    def test_returns_nested_stats(self, mock_query, client):
        """Should reshape flat query results into nested structure."""
        mock_query.return_value = [
            {"queue_name": "parse", "status": "pending", "count": 10},
            {"queue_name": "parse", "status": "processing", "count": 2},
            {"queue_name": "triage", "status": "pending", "count": 50},
            {"queue_name": "triage", "status": "failed", "count": 3},
        ]

        response = client.get("/queue/stats")

        assert response.status_code == 200
        data = response.get_json()
        assert "queues" in data
        assert data["queues"]["parse"]["pending"] == 10
        assert data["queues"]["parse"]["processing"] == 2
        assert data["queues"]["triage"]["pending"] == 50
        assert data["queues"]["triage"]["failed"] == 3

    @patch("gateway.blueprints.queue.postgres.execute_query")
    def test_handles_empty_results(self, mock_query, client):
        """Should handle empty queue results."""
        mock_query.return_value = []

        response = client.get("/queue/stats")

        assert response.status_code == 200
        data = response.get_json()
        assert data["queues"] == {}


class TestListFailed:
    """Tests for GET /queue/failed endpoint."""

    @patch("gateway.blueprints.queue.postgres.execute_query")
    def test_returns_failed_jobs(self, mock_query, client):
        """Should return list of failed jobs."""
        mock_query.return_value = [
            {
                "id": 1,
                "queue_name": "parse",
                "gmail_id": "abc123",
                "payload": {"gmail_id": "abc123"},
                "error": "Parse error",
                "attempts": 3,
                "created_at": "2025-01-09T00:00:00",
                "updated_at": "2025-01-09T00:05:00",
            },
            {
                "id": 2,
                "queue_name": "triage",
                "gmail_id": "xyz789",
                "payload": {"gmail_id": "xyz789"},
                "error": "Triage failed",
                "attempts": 5,
                "created_at": "2025-01-09T00:10:00",
                "updated_at": "2025-01-09T00:15:00",
            },
        ]

        response = client.get("/queue/failed")

        assert response.status_code == 200
        data = response.get_json()
        assert len(data["failed_jobs"]) == 2
        assert data["limit"] == 50
        assert data["offset"] == 0
        assert data["count"] == 2

    @patch("gateway.blueprints.queue.postgres.execute_query")
    def test_respects_limit_and_offset(self, mock_query, client):
        """Should apply limit and offset parameters."""
        mock_query.return_value = []

        response = client.get("/queue/failed?limit=10&offset=20")

        assert response.status_code == 200
        data = response.get_json()
        assert data["limit"] == 10
        assert data["offset"] == 20
        # Verify query was called with correct params
        args = mock_query.call_args
        assert "10" in args[0][1]
        assert "20" in args[0][1]

    @patch("gateway.blueprints.queue.postgres.execute_query")
    def test_enforces_max_limit(self, mock_query, client):
        """Should cap limit at 100."""
        mock_query.return_value = []

        response = client.get("/queue/failed?limit=500")

        assert response.status_code == 200
        data = response.get_json()
        assert data["limit"] == 100

    @patch("gateway.blueprints.queue.postgres.execute_query")
    def test_filters_by_queue(self, mock_query, client):
        """Should filter by queue_name when provided."""
        mock_query.return_value = []

        response = client.get("/queue/failed?queue=parse")

        assert response.status_code == 200
        # Verify query includes queue filter
        args = mock_query.call_args
        assert "queue_name = %s" in args[0][0]
        assert "parse" in args[0][1]


class TestRetryFailed:
    """Tests for POST /queue/failed/<id>/retry endpoint."""

    @patch("gateway.blueprints.queue.postgres.execute_update")
    @patch("gateway.blueprints.queue.postgres.execute_one")
    def test_retries_failed_job(self, mock_execute_one, mock_execute_update, client):
        """Should reset failed job to pending."""
        mock_execute_one.return_value = {
            "id": 1,
            "queue_name": "parse",
            "gmail_id": "abc123",
            "status": "failed",
        }

        mock_execute_update.return_value = 1

        response = client.post("/queue/failed/1/retry")

        assert response.status_code == 200
        data = response.get_json()
        assert data["message"] == "Job queued for retry"
        assert data["job_id"] == 1
        assert data["queue_name"] == "parse"
        # Verify update query resets status, error, and attempts
        update_args = mock_execute_update.call_args
        assert "status = 'pending'" in update_args[0][0]
        assert "last_error = NULL" in update_args[0][0]
        assert "attempts = 0" in update_args[0][0]
        # The row must still be failed at write time, not just at SELECT time.
        assert "status = 'failed'" in update_args[0][0]

    @patch("gateway.blueprints.queue.postgres.execute_update")
    @patch("gateway.blueprints.queue.postgres.execute_one")
    def test_returns_409_if_claimed_between_check_and_write(
        self, mock_execute_one, mock_execute_update, client
    ):
        """The SELECT cannot hold the row, so the UPDATE can match nothing.

        Without the predicate this reset a job another worker was already
        processing, which runs it twice.
        """
        mock_execute_one.return_value = {
            "id": 1,
            "queue_name": "parse",
            "gmail_id": "abc123",
            "status": "failed",
        }
        mock_execute_update.return_value = 0

        response = client.post("/queue/failed/1/retry")

        assert response.status_code == 409
        assert "no longer failed" in response.get_json()["error"]

    @patch("gateway.blueprints.queue.postgres.execute_one")
    def test_returns_404_if_not_found(self, mock_execute_one, client):
        """Should return 404 if job not found."""
        mock_execute_one.return_value = None

        response = client.post("/queue/failed/999/retry")

        assert response.status_code == 404
        data = response.get_json()
        assert "Job not found" in data["error"]

    @patch("gateway.blueprints.queue.postgres.execute_one")
    def test_returns_400_if_not_failed(self, mock_execute_one, client):
        """Should return 400 if job is not in failed status."""
        mock_execute_one.return_value = {
            "id": 1,
            "queue_name": "parse",
            "gmail_id": "abc123",
            "status": "pending",
        }

        response = client.post("/queue/failed/1/retry")

        assert response.status_code == 400
        data = response.get_json()
        assert "not failed" in data["error"]
        assert "pending" in data["error"]


class TestDeleteFailed:
    """Tests for DELETE /queue/failed/<id> endpoint."""

    @patch("gateway.blueprints.queue.postgres.execute_update")
    @patch("gateway.blueprints.queue.postgres.execute_one")
    def test_deletes_failed_job(self, mock_execute_one, mock_execute_update, client):
        """Should delete a failed job."""
        mock_execute_one.return_value = {
            "id": 1,
            "queue_name": "parse",
            "status": "failed",
        }

        mock_execute_update.return_value = 1

        response = client.delete("/queue/failed/1")

        assert response.status_code == 200
        data = response.get_json()
        assert data["message"] == "Job deleted"
        assert data["job_id"] == 1
        # Verify DELETE query was called
        delete_args = mock_execute_update.call_args
        assert "DELETE FROM queue" in delete_args[0][0]
        assert delete_args[0][1] == (1,)
        # The predicate is what keeps this from deleting live work.
        assert "status = 'failed'" in delete_args[0][0]

    @patch("gateway.blueprints.queue.postgres.execute_update")
    @patch("gateway.blueprints.queue.postgres.execute_one")
    def test_returns_409_if_claimed_between_check_and_delete(
        self, mock_execute_one, mock_execute_update, client
    ):
        """Deleting a row that stopped being failed would lose live work."""
        mock_execute_one.return_value = {
            "id": 1,
            "queue_name": "parse",
            "status": "failed",
        }
        mock_execute_update.return_value = 0

        response = client.delete("/queue/failed/1")

        assert response.status_code == 409
        assert "no longer failed" in response.get_json()["error"]

    @patch("gateway.blueprints.queue.postgres.execute_one")
    def test_returns_404_if_not_found(self, mock_execute_one, client):
        """Should return 404 if job not found."""
        mock_execute_one.return_value = None

        response = client.delete("/queue/failed/999")

        assert response.status_code == 404
        data = response.get_json()
        assert "Job not found" in data["error"]

    @patch("gateway.blueprints.queue.postgres.execute_one")
    def test_returns_400_if_not_failed(self, mock_execute_one, client):
        """Should return 400 if trying to delete non-failed job."""
        mock_execute_one.return_value = {
            "id": 1,
            "queue_name": "parse",
            "status": "pending",
        }

        response = client.delete("/queue/failed/1")

        assert response.status_code == 400
        data = response.get_json()
        assert "Can only delete failed jobs" in data["error"]


class TestRetryAllFailed:
    """Tests for POST /queue/failed/retry-all endpoint."""

    @patch("gateway.blueprints.queue.postgres.execute_update")
    def test_retries_all_failed_for_queue(self, mock_execute_update, client):
        """Should retry all failed jobs for a given queue."""
        mock_execute_update.return_value = 5  # 5 rows updated

        response = client.post("/queue/failed/retry-all?queue=parse")

        assert response.status_code == 200
        data = response.get_json()
        assert data["message"] == "Retried 5 failed jobs"
        assert data["queue_name"] == "parse"
        assert data["count"] == 5
        # Verify update query targets failed status and queue_name
        update_args = mock_execute_update.call_args
        assert "status = 'failed'" in update_args[0][0]
        assert "queue_name = %s" in update_args[0][0]
        assert update_args[0][1] == ("parse",)

    def test_requires_queue_parameter(self, client):
        """Should return 400 if queue parameter missing."""
        response = client.post("/queue/failed/retry-all")

        assert response.status_code == 400
        data = response.get_json()
        assert "queue parameter required" in data["error"]

    @patch("gateway.blueprints.queue.postgres.execute_update")
    def test_handles_zero_failed_jobs(self, mock_execute_update, client):
        """Should handle case where no failed jobs exist."""
        mock_execute_update.return_value = 0

        response = client.post("/queue/failed/retry-all?queue=parse")

        assert response.status_code == 200
        data = response.get_json()
        assert data["count"] == 0
