"""Queue management API endpoints."""

from typing import Any

from cortex_utils.queue import DeadLetterManager
from cortex_utils.queue import inspect as queue_inspect
from flask import Blueprint, Response, jsonify, request

from gateway.services import postgres
from gateway.services.postgres import ConnectionContext

queue_bp = Blueprint("queue", __name__)

# A bulk action's id list is capped so one request cannot walk the whole
# dead-letter table. Matches cryo's QUEUE_PAGE_MAX, because the extension
# renders both tables with the same component.
BULK_MAX = 100


def _ref(payload: Any) -> str | None:
    """The one payload field that may cross the HTTP boundary.

    cryo forwards a `ref` rather than the payload because a cryo payload holds
    captured URLs, including from private sessions. Cortex payloads are less
    sensitive but the same rule is kept: a dead-letter row is read by a
    browser extension, and `gmail_id` is all the UI needs to fetch the rest
    through /emails/<gmail_id>. Forwarding the payload would also leak
    whatever a future producer decides to put in it.
    """
    if isinstance(payload, dict):
        ref = payload.get("gmail_id")
        return ref if isinstance(ref, str) else None
    return None


def _requested_ids() -> tuple[list[int] | None, str | None]:
    """The `ids` list from a bulk body, or (None, error message)."""
    body = request.get_json(silent=True) or {}
    raw = body.get("ids")
    if not isinstance(raw, list) or not raw:
        return None, "ids must be a non-empty list of integers"
    if len(raw) > BULK_MAX:
        return None, f"ids may hold at most {BULK_MAX} entries"
    if not all(isinstance(i, int) and not isinstance(i, bool) for i in raw):
        # isinstance(True, int) is True, and `ids: [true]` would otherwise
        # become dead-letter id 1 -- an unrelated payload resurrected.
        return None, "ids must all be integers"
    return list(raw), None


@queue_bp.route("/health")
def queue_health() -> Response:
    """Queue depths, dead-letter count and partition headroom in one call.

    Shaped to match cryo's GET /api/queue so the extension renders cortex's
    queues with the component it already has. Both sides are thin wrappers
    over the same cortex_utils.queue.inspect.health().
    """
    with ConnectionContext() as conn:
        health = queue_inspect.health(conn)
    return jsonify(
        {
            "queues": [
                {
                    "queue": d.queue_name,
                    "ready": d.ready,
                    "deferred": d.deferred,
                    "processing": d.processing,
                    "failed": d.failed,
                    "oldest_ready_age_s": d.oldest_ready_age_s,
                }
                for d in health.depths
            ],
            "dead_letter": health.dead_letter,
            "partitioned": health.partitioned,
            "partition_headroom_days": health.partition_headroom_days,
            "self_healed_partitions": health.self_healed_partitions,
            "partition_count": health.partition_count,
            "oldest_partition_age_days": health.oldest_partition_age_days,
            "is_healthy": health.is_healthy,
            "server_time": health.server_time,
        }
    )


@queue_bp.route("/dead")
def dead_letters() -> Response:
    """Jobs that exhausted their attempts.

    Invisible over HTTP until now, which is where cortex loses mail quietly:
    a truncated LLM response is retried to max_attempts and archived here,
    and nothing surfaced it.
    """
    limit = min(request.args.get("limit", 50, type=int), BULK_MAX)
    include_resolved = request.args.get("include_resolved", "").lower() in (
        "1",
        "true",
        "yes",
    )
    with ConnectionContext() as conn:
        jobs = DeadLetterManager(conn).list_jobs(limit=limit, include_resolved=include_resolved)
    return jsonify(
        {
            "dead": [
                {
                    "id": j["id"],
                    "original_id": j["original_id"],
                    "queue": j["queue_name"],
                    "ref": _ref(j.get("payload")),
                    "attempts": j["attempts"],
                    "error": j["last_error"],
                    "created_at": j["created_at"],
                    "failed_at": j["failed_at"],
                    "dismissed_at": j["dismissed_at"],
                    "retried_at": j["retried_at"],
                    "retried_as": j["retried_as"],
                }
                for j in jobs
            ],
            "limit": limit,
            "count": len(jobs),
        }
    )


@queue_bp.route("/stuck")
def stuck_jobs() -> Response:
    """Claimed and never completed -- a worker died holding the claim."""
    limit = min(request.args.get("limit", 50, type=int), BULK_MAX)
    with ConnectionContext() as conn:
        jobs = queue_inspect.stuck(conn, limit=limit)
    return jsonify(
        {
            "stuck": [
                {
                    "id": j.id,
                    "queue": j.queue_name,
                    "claimed_by": j.claimed_by,
                    "claimed_at": j.claimed_at,
                    "stuck_for_s": j.stuck_for_s,
                    "attempts": j.attempts,
                }
                for j in jobs
            ],
            "limit": limit,
            "count": len(jobs),
        }
    )


@queue_bp.route("/dead/resubmit", methods=["POST"])
def resubmit_dead() -> Response | tuple[Response, int]:
    """Re-enqueue dead letters by id, keeping the archive rows.

    200 with per-item results even when some fail, mirroring cryo: a mixed
    outcome is the normal case, and a 4xx for the whole call would tell the
    UI nothing about which ids moved.
    """
    ids, error = _requested_ids()
    if error:
        return jsonify({"error": error}), 400
    assert ids is not None

    results = []
    with ConnectionContext() as conn:
        manager = DeadLetterManager(conn)
        for dead_id in ids:
            if manager.retry_job(dead_id):
                job = manager.get_job(dead_id)
                results.append(
                    {
                        "id": dead_id,
                        "ok": True,
                        "new_id": job["retried_as"] if job else None,
                    }
                )
            else:
                results.append(
                    {
                        "id": dead_id,
                        "ok": False,
                        "why": manager.why_not_retryable(dead_id),
                    }
                )
        conn.commit()
    return jsonify({"results": results, "resubmitted": sum(1 for r in results if r["ok"])})


@queue_bp.route("/dead/dismiss", methods=["POST"])
def dismiss_dead() -> Response | tuple[Response, int]:
    """Write dead letters off: terminal, not destructive.

    Without this, a retry-only list never clears and the real failures end up
    buried in things nobody intends to act on.
    """
    ids, error = _requested_ids()
    if error:
        return jsonify({"error": error}), 400
    assert ids is not None

    results = []
    with ConnectionContext() as conn:
        manager = DeadLetterManager(conn)
        for dead_id in ids:
            ok = manager.dismiss(dead_id)
            entry: dict[str, Any] = {"id": dead_id, "ok": ok}
            if not ok:
                entry["why"] = f"no dead letter job with id {dead_id}"
            results.append(entry)
        conn.commit()
    return jsonify({"results": results, "dismissed": sum(1 for r in results if r["ok"])})


@queue_bp.route("/stats")
def queue_stats():
    """Get queue depths by name and status."""
    query = """
        SELECT
            queue_name,
            status,
            COUNT(*) as count
        FROM queue
        GROUP BY queue_name, status
        ORDER BY queue_name, status
    """
    results = postgres.execute_query(query)

    # Reshape into nested structure
    stats: dict[str, dict[str, int]] = {}
    for row in results:
        queue_name = row["queue_name"]
        status = row["status"]
        count = row["count"]
        if queue_name not in stats:
            stats[queue_name] = {}
        stats[queue_name][status] = count

    return jsonify({"queues": stats})


@queue_bp.route("/failed")
def list_failed():
    """List failed jobs (dead letters)."""
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    queue_name = request.args.get("queue")

    limit = min(limit, 100)

    query = """
        SELECT
            id,
            queue_name,
            payload->>'gmail_id' as gmail_id,
            payload,
            last_error as error,
            attempts,
            created_at,
            claimed_at as updated_at
        FROM queue
        WHERE status = 'failed'
    """
    params: list[str] = []

    if queue_name:
        query += " AND queue_name = %s"
        params.append(queue_name)

    query += " ORDER BY updated_at DESC LIMIT %s OFFSET %s"
    params.extend([str(limit), str(offset)])

    results = postgres.execute_query(query, tuple(params))

    return jsonify(
        {
            "failed_jobs": results,
            "limit": limit,
            "offset": offset,
            "count": len(results),
        }
    )


@queue_bp.route("/failed/<int:job_id>/retry", methods=["POST"])
def retry_failed(job_id: int) -> Response | tuple[Response, int]:
    """Retry a failed job by resetting its status to pending."""
    # First verify it exists and is failed
    check_query = """
        SELECT id, queue_name, payload->>'gmail_id' as gmail_id, status
        FROM queue
        WHERE id = %s
    """
    job = postgres.execute_one(check_query, (job_id,))

    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job["status"] != "failed":
        return jsonify({"error": f"Job is not failed (current status: {job['status']})"}), 400

    # Reset to pending, releasing the claim with it.
    #
    # Not because a stalled worker can complete() a pending row -- it cannot;
    # that statement requires status='processing' as well as a matching token,
    # and ops.fail_or_retry() clears both columns when it retires a row, so a
    # canonically-failed row carries no token to inherit in the first place.
    #
    # It matters when the next claimant sets status='processing' WITHOUT
    # writing its own token, because then the stale token survives into the
    # processing state and the previous worker's complete() matches. That is
    # not hypothetical: every cortex worker hand-writes its claim today and
    # none of them set claimed_by. So this guard is for the partially-ported
    # fleet -- one service claiming through ops.claim(), another not -- which
    # is the window the port opens and closes.
    #
    # The same fix landed at six sites in postmark and triage; these two were
    # missed because the audit enumerated those repos and cortex-utils, and not
    # this one.
    update_query = """
        UPDATE queue
        SET status = 'pending', last_error = NULL, attempts = 0,
            claimed_at = NULL, claimed_by = NULL
        WHERE id = %s
    """
    postgres.execute_update(update_query, (job_id,))

    return jsonify(
        {
            "message": "Job queued for retry",
            "job_id": job_id,
            "queue_name": job["queue_name"],
        }
    )


@queue_bp.route("/failed/<int:job_id>", methods=["DELETE"])
def delete_failed(job_id: int) -> Response | tuple[Response, int]:
    """Delete a failed job."""
    # First verify it exists and is failed
    check_query = """
        SELECT id, queue_name, status
        FROM queue
        WHERE id = %s
    """
    job = postgres.execute_one(check_query, (job_id,))

    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job["status"] != "failed":
        return (
            jsonify({"error": f"Can only delete failed jobs (current status: {job['status']})"}),
            400,
        )

    delete_query = "DELETE FROM queue WHERE id = %s"
    postgres.execute_update(delete_query, (job_id,))

    return jsonify(
        {
            "message": "Job deleted",
            "job_id": job_id,
        }
    )


@queue_bp.route("/failed/retry-all", methods=["POST"])
def retry_all_failed():
    """Retry all failed jobs for a queue."""
    queue_name = request.args.get("queue")
    if not queue_name:
        return jsonify({"error": "queue parameter required"}), 400

    update_query = """
        UPDATE queue
        SET status = 'pending', last_error = NULL, attempts = 0,
            claimed_at = NULL, claimed_by = NULL
        WHERE status = 'failed' AND queue_name = %s
    """
    count = postgres.execute_update(update_query, (queue_name,))

    return jsonify(
        {
            "message": f"Retried {count} failed jobs",
            "queue_name": queue_name,
            "count": count,
        }
    )
