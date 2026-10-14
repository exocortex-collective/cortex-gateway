"""Queue management API endpoints."""

from collections.abc import Callable
from typing import Any

import psycopg2
from cortex_utils.logging import get_logger
from cortex_utils.queue import DeadLetterManager
from cortex_utils.queue import inspect as queue_inspect
from flask import Blueprint, Response, jsonify, request
from psycopg2.extensions import connection

from gateway.services import postgres
from gateway.services.postgres import ConnectionContext

queue_bp = Blueprint("queue", __name__)
logger = get_logger()

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


def _page_limit() -> int:
    """A usable page size, clamped at both ends.

    Flask's type=int already returns the default for a non-numeric value, so
    there is no None case to handle; both bounds are clamped because LIMIT 0
    returns nothing and Postgres rejects a negative one outright.
    """
    return max(1, min(request.args.get("limit", 50, type=int), BULK_MAX))


def _requested_ids() -> tuple[list[int] | None, str | None]:
    """The `ids` list from a bulk body, or (None, error message)."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        # get_json(silent=True) returns whatever JSON was sent, so a bare
        # list or string would otherwise reach .get() and raise.
        return None, "body must be a JSON object with an 'ids' list"
    raw = body.get("ids")
    if not isinstance(raw, list) or not raw:
        return None, "ids must be a non-empty list of integers"
    if len(raw) > BULK_MAX:
        return None, f"ids may hold at most {BULK_MAX} entries"
    if not all(isinstance(i, int) and not isinstance(i, bool) for i in raw):
        # isinstance(True, int) is True, and `ids: [true]` would otherwise
        # become dead-letter id 1 -- an unrelated payload resurrected.
        return None, "ids must all be integers"
    # Deduplicated, order kept, so the audit line reports one outcome per id.
    return list(dict.fromkeys(raw)), None


def _rollback(conn: connection, action: str, dead_id: int) -> None:
    """Clear an aborted transaction so the ids behind this one still run.

    After an error the server refuses every later statement on the
    transaction, so without this one bad id fails all the ids behind it. Any
    caller that swallows a database error owes the batch this call.

    Guarded because a rollback on an already-closed connection raises, and
    that exception would escape past ConnectionContext.__exit__ -- which
    rolls back again, raises again, and never reaches put_connection, leaking
    the pool slot and skipping the audit line even though ids had been
    applied.
    """
    try:
        conn.rollback()
    except Exception as exc:
        logger.warning(
            "queue_bulk_rollback_failed",
            action=action,
            dead_id=dead_id,
            error_type=type(exc).__name__,
        )


def _from_database(exc: BaseException | None) -> bool:
    """True if a psycopg2 error is anywhere a traceback of exc would print.

    Both links are followed, not just the one traceback prints, so the answer
    errs toward withholding.
    """
    seen: set[int] = set()
    todo: list[BaseException | None] = [exc]
    while todo:
        cur = todo.pop()
        if cur is None or id(cur) in seen:
            continue
        if isinstance(cur, psycopg2.Error):
            return True
        seen.add(id(cur))
        todo += [cur.__cause__, cur.__context__, *getattr(cur, "exceptions", ())]
    return False


def _bulk(
    ids: list[int],
    act: Callable[[DeadLetterManager, int], dict[str, Any]],
    action: str,
) -> list[dict[str, Any]]:
    """Run a per-id action and report each id's outcome.

    Both routes promise per-item results: one bad id costs that id only.
    DeadLetterManager.retry_job and .dismiss each commit for themselves, so
    there is nothing to commit here.
    """
    results: list[dict[str, Any]] = []
    with ConnectionContext() as conn:
        manager = DeadLetterManager(conn)
        for dead_id in ids:
            try:
                results.append(act(manager, dead_id))
            except Exception as exc:
                _rollback(conn, action, dead_id)
                # The message is not logged: a psycopg2 error carries a DETAIL
                # line holding the offending row's values. Anything else gets
                # its traceback, so a programming error can be found.
                logger.warning(
                    "queue_bulk_item_failed",
                    action=action,
                    dead_id=dead_id,
                    error_type=type(exc).__name__,
                    **({} if _from_database(exc) else {"exc_info": True}),
                )
                results.append({"id": dead_id, "ok": False, "why": "error, see gateway logs"})
    moved = [r["id"] for r in results if r["ok"]]
    # The gateway's own record of a bulk mutation and its caller. The
    # dead-letter rows also show what moved; this shows who asked and when,
    # and is the only place a request that moved nothing appears at all.
    logger.info(
        "queue_bulk_action",
        action=action,
        requested=ids,
        moved=moved,
        failed=[r["id"] for r in results if not r["ok"]],
        remote_addr=request.remote_addr,
    )
    return results


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

    This is where cortex loses mail quietly: a truncated LLM response is
    retried to max_attempts and archived here.
    """
    limit = _page_limit()
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
    """Claimed and still unfinished past the visibility window.

    A dead worker is the usual cause but not the only one: the query is a
    status and an age, so a live worker slower than the window is in here
    too. Read it as "look at these", not as "these are abandoned".
    """
    limit = _page_limit()
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
    if ids is None:
        return jsonify({"error": error}), 400

    def _one(manager: DeadLetterManager, dead_id: int) -> dict[str, Any]:
        # retry_job's default priority of 0 is kept deliberately. Its
        # docstring reserves -100 for bulk or historical retries, meaning the
        # few-hundred-row sweeps that would otherwise drain ahead of live
        # mail; this route is an operator picking rows out of a UI list,
        # capped at BULK_MAX, and they want them back at normal priority.
        if not manager.retry_job(dead_id):
            return {"id": dead_id, "ok": False, "why": manager.why_not_retryable(dead_id)}
        # retry_job has COMMITTED by this point, so the id has moved whatever
        # happens next. Reading back the new queue id is a convenience for the
        # UI; letting it decide `ok` would report a durable re-enqueue as a
        # failure, leave it out of the audit line's `moved`, and invite the
        # operator to retry an id that had already gone.
        try:
            job = manager.get_job(dead_id)
        except Exception as exc:
            logger.warning(
                "queue_resubmit_readback_failed",
                dead_id=dead_id,
                error_type=type(exc).__name__,
            )
            # Swallowing a database error here would leave the transaction
            # aborted, and _bulk's own rollback never runs because this id
            # reports ok. The NEXT id would then fail in retry_job and be
            # reported as a failure without having been attempted.
            _rollback(manager.conn, "resubmit", dead_id)
            job = None
        return {"id": dead_id, "ok": True, "new_id": job["retried_as"] if job else None}

    results = _bulk(ids, _one, "resubmit")
    return jsonify({"results": results, "resubmitted": sum(1 for r in results if r["ok"])})


@queue_bp.route("/dead/dismiss", methods=["POST"])
def dismiss_dead() -> Response | tuple[Response, int]:
    """Write dead letters off: terminal, not destructive.

    Without this, a retry-only list never clears and the real failures end up
    buried in things nobody intends to act on.
    """
    ids, error = _requested_ids()
    if ids is None:
        return jsonify({"error": error}), 400

    def _one(manager: DeadLetterManager, dead_id: int) -> dict[str, Any]:
        if manager.dismiss(dead_id):
            return {"id": dead_id, "ok": True}
        return {"id": dead_id, "ok": False, "why": f"no dead letter job with id {dead_id}"}

    results = _bulk(ids, _one, "dismiss")
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
