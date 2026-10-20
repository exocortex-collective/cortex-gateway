"""Triage-related API endpoints."""

from datetime import UTC, datetime, timedelta
from typing import Any

from cortex_utils.logging import get_logger
from flask import Blueprint, jsonify, request

from gateway.services import postgres
from gateway.services.postgres import ConnectionContext

triage_bp = Blueprint("triage", __name__)
logger = get_logger()


def is_preview(args: Any) -> bool:
    """A truthy ?preview= value. "0" and "false" do not count."""
    raw = (args.get("preview") or "").strip().lower()
    return raw in ("1", "true", "yes")


@triage_bp.route("/stats")
def triage_stats():
    """Get classification statistics."""
    # Overall counts by rule/category
    query = """
        SELECT
            COALESCE(matched_chain, 'llm') as classifier,
            action_taken->>'label' as label,
            action_taken->>'action' as action,
            COUNT(*) as count
        FROM classifications
        GROUP BY matched_chain, action_taken->>'label', action_taken->>'action'
        ORDER BY count DESC
        LIMIT 50
    """
    by_rule = postgres.execute_query(query)

    # Recent activity (last 24h)
    recent_query = """
        SELECT
            date_trunc('hour', classified_at) as hour,
            COUNT(*) as count
        FROM classifications
        WHERE classified_at >= NOW() - INTERVAL '24 hours'
        GROUP BY hour
        ORDER BY hour
    """
    recent = postgres.execute_query(recent_query)

    # Method breakdown (rule vs llm)
    method_query = """
        SELECT
            CASE WHEN matched_chain IS NOT NULL THEN 'rule' ELSE 'llm' END as method,
            COUNT(*) as count
        FROM classifications
        GROUP BY CASE WHEN matched_chain IS NOT NULL THEN 'rule' ELSE 'llm' END
    """
    methods = postgres.execute_query(method_query)

    return jsonify(
        {
            "by_classifier": by_rule,
            "recent_hourly": recent,
            "methods": {row["method"]: row["count"] for row in methods},
        }
    )


@triage_bp.route("/rerun", methods=["POST"])
def rerun_triage():
    """Re-run triage on emails.

    Request body:
        gmail_ids: List of specific Gmail IDs to rerun
        label: Label filter (e.g., "Cortex/Uncategorized")
        senders: List of sender email addresses (supports glob patterns with *)
        days: Number of days to look back (default: 7)
        force: If true, rerun even if already classified
        priority: Queue priority (default: -100)
    """
    data = request.get_json() or {}

    # Validate input types
    gmail_ids_raw = data.get("gmail_ids")
    if gmail_ids_raw is not None:
        if not isinstance(gmail_ids_raw, list) or not all(
            isinstance(s, str) for s in gmail_ids_raw
        ):
            return jsonify({"error": "'gmail_ids' must be a list of strings"}), 400
    gmail_ids = gmail_ids_raw or []

    senders_raw = data.get("senders")
    if senders_raw is not None:
        if not isinstance(senders_raw, list) or not all(isinstance(s, str) for s in senders_raw):
            return jsonify({"error": "'senders' must be a list of strings"}), 400
    senders = [s for s in senders_raw or [] if s]

    label = data.get("label")
    force = data.get("force", False)

    # bool is excluded because isinstance(True, int) is True, so `days: true`
    # would otherwise mean one day.
    days = data.get("days", 7)
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        return jsonify({"error": "'days' must be an integer >= 1"}), 400
    priority = data.get("priority", -100)
    if isinstance(priority, bool) or not isinstance(priority, int):
        return jsonify({"error": "'priority' must be an integer"}), 400

    if not gmail_ids and not label and not senders:
        return jsonify({"error": "Must specify gmail_ids, label, or senders filter"}), 400

    # Ensure filters are mutually exclusive
    filters_provided = sum(map(bool, (gmail_ids, label, senders)))
    if filters_provided > 1:
        return (
            jsonify({"error": "Only one filter type allowed (gmail_ids, label, or senders)"}),
            400,
        )

    # Build insert query
    if gmail_ids:
        # Specific IDs
        placeholders = ",".join(["%s"] * len(gmail_ids))
        query = f"""
            INSERT INTO queue (queue_name, payload, priority, status, created_at)
            SELECT
                'triage',
                jsonb_build_object('email_id', er.id, 'gmail_id', er.gmail_id, 'rerun', true),
                %s,
                'pending',
                NOW()
            FROM emails_raw er
            WHERE er.gmail_id IN ({placeholders})
        """
        # list[Any], not list[str | int]: a list is INVARIANT, so the
        # narrower annotation rejects both this assignment (list[int]) and
        # the senders branch below (list[object]).
        #
        # Only the pre-commit mypy pin (1.5.1) rejects them; the newer mypy
        # in this venv accepts the narrower type, so `uv run mypy` alone
        # shows nothing and makes this look like dead weight. Narrowing it
        # blocks every commit.
        params: list[Any] = [priority] + gmail_ids
    elif senders:
        # Sender filter with date range - join with emails_parsed to filter by from_addr
        cutoff = datetime.now(UTC) - timedelta(days=days)

        # Build LIKE conditions for each sender (supports glob patterns)
        # Always use LIKE with ESCAPE to properly handle _ in email addresses
        sender_conditions = ["ep.from_addr LIKE %s ESCAPE '\\'" for _ in senders]
        sender_clause = " OR ".join(sender_conditions)

        query = f"""
            INSERT INTO queue (queue_name, payload, priority, status, created_at)
            SELECT DISTINCT ON (er.gmail_id)
                'triage',
                jsonb_build_object('email_id', er.id, 'gmail_id', er.gmail_id, 'rerun', true),
                %s,
                'pending',
                NOW()
            FROM emails_raw er
            JOIN emails_parsed ep ON ep.gmail_id = er.gmail_id
            WHERE er.created_at >= %s
            AND ({sender_clause})
        """

        # Escape SQL LIKE special characters before converting glob * to SQL %
        sender_params = [
            s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("*", "%")
            for s in senders
        ]
        params = [priority, cutoff.isoformat()] + sender_params
    else:
        # Label filter with date range - join with classifications to filter by Cortex label
        cutoff = datetime.now(UTC) - timedelta(days=days)
        query = """
            INSERT INTO queue (queue_name, payload, priority, status, created_at)
            SELECT DISTINCT ON (er.gmail_id)
                'triage',
                jsonb_build_object('email_id', er.id, 'gmail_id', er.gmail_id, 'rerun', true),
                %s,
                'pending',
                NOW()
            FROM emails_raw er
            JOIN classifications c ON c.email_id = er.id
            WHERE er.created_at >= %s
            AND c.action_taken->>'label' = %s
        """
        # label is guaranteed to be non-None due to earlier check
        params = [priority, cutoff.isoformat(), str(label)]

    # Avoid duplicates unless force
    if not force:
        query += """
            AND NOT EXISTS (
                SELECT 1 FROM queue q
                WHERE q.queue_name = 'triage'
                AND q.payload->>'gmail_id' = er.gmail_id
                AND q.status IN ('pending', 'processing')
            )
        """

    # Add ORDER BY for DISTINCT ON queries (senders and label filters)
    if senders or label:
        query += " ORDER BY er.gmail_id, er.created_at DESC"

    # Handle any race conditions with existing queue entries
    query += " ON CONFLICT DO NOTHING"

    # PREVIEW RUNS THE REAL STATEMENT AND ROLLS IT BACK, rather than
    # rewriting it into a COUNT. Which clauses are in play depends on the
    # branch -- DISTINCT ON only for the senders and label filters, NOT EXISTS
    # only when force is false, ON CONFLICT DO NOTHING always -- so a
    # hand-built count would have to reproduce that branching and would drift
    # from it on the next edit, reporting a number the real call does not
    # produce. Executing and discarding is exact by construction.
    #
    # It consumes sequence values, which are not rolled back. Harmless: the
    # queue's id is a surrogate and gaps mean nothing.
    if is_preview(request.args):
        with ConnectionContext() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                count = cur.rowcount
            conn.rollback()
        # An operator reading "would enqueue N" needs to know no rows landed;
        # the real path below logs the same shape with preview=False.
        logger.info(
            "triage_rerun",
            preview=True,
            count=count,
            label=label,
            senders=senders or None,
            gmail_ids=len(gmail_ids) or None,
            force=force,
            priority=priority,
        )
        return jsonify(
            {
                "preview": True,
                "message": f"Would enqueue {count} emails for triage rerun",
                "gmail_ids": gmail_ids if gmail_ids else None,
                "label": label,
                "senders": senders if senders else None,
                "days": days if not gmail_ids else None,
                "force": force,
                "priority": priority,
                "count": count,
            }
        )

    count = postgres.execute_update(query, tuple(params))
    logger.info(
        "triage_rerun",
        preview=False,
        count=count,
        label=label,
        senders=senders or None,
        gmail_ids=len(gmail_ids) or None,
        force=force,
        priority=priority,
    )

    return jsonify(
        {
            "preview": False,
            "message": f"Enqueued {count} emails for triage rerun",
            "gmail_ids": gmail_ids if gmail_ids else None,
            "label": label,
            "senders": senders if senders else None,
            "days": days if not gmail_ids else None,
            "force": force,
            "priority": priority,
            "count": count,
        }
    )


@triage_bp.route("/classifications")
def list_classifications():
    """List recent classifications."""
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    label = request.args.get("label")
    action = request.args.get("action")

    limit = min(limit, 100)

    query = """
        SELECT
            c.gmail_id,
            c.matched_chain as matched_rule,
            c.action_taken->>'label' as label,
            c.action_taken->>'action' as action,
            c.llm_category,
            c.llm_confidence as confidence,
            c.classified_at as created_at,
            ep.subject,
            ep.from_addr
        FROM classifications c
        LEFT JOIN emails_parsed ep ON c.gmail_id = ep.gmail_id
        WHERE 1=1
    """
    params: list[str] = []

    if label:
        query += " AND c.action_taken->>'label' = %s"
        params.append(label)

    if action:
        query += " AND c.action_taken->>'action' = %s"
        params.append(action)

    query += " ORDER BY c.classified_at DESC LIMIT %s OFFSET %s"
    params.extend([str(limit), str(offset)])

    results = postgres.execute_query(query, tuple(params))

    return jsonify(
        {
            "classifications": results,
            "limit": limit,
            "offset": offset,
            "count": len(results),
        }
    )


@triage_bp.route("/convergence/status")
def convergence_status():
    """Get convergence status - how many emails have outdated config versions.

    Returns:
        {
            "current_version": Active config version,
            "emails_behind": Total emails needing convergence,
            "priority_senders_count": Emails from priority senders (bulk updateable),
            "non_priority_count": Emails needing re-triage,
            "oldest_version": Oldest config version still in use
        }
    """
    # Get current active config version
    version_query = "SELECT version FROM triage_config_versions WHERE is_active = TRUE LIMIT 1"
    version_rows = postgres.execute_query(version_query)

    if not version_rows:
        return jsonify({"error": "No active config version found"}), 500

    current_version = version_rows[0]["version"]

    # Count priority sender emails (can be bulk-updated)
    priority_count_query = """
        SELECT COUNT(*) as count
        FROM classifications c
        WHERE c.config_version_id < %s
          AND EXISTS (
            SELECT 1
            FROM emails_parsed ep
            JOIN triage_email_mappings tem
              ON LOWER(ep.from_addr) = LOWER(tem.email_address)
            WHERE ep.gmail_id = c.gmail_id
              AND tem.mapping_type = 'priority'
              AND tem.deleted_at IS NULL
          )
    """
    priority_rows = postgres.execute_query(priority_count_query, (current_version,))
    priority_count = priority_rows[0]["count"] if priority_rows else 0

    # Count total emails behind
    total_behind_query = """
        SELECT COUNT(*) as count, MIN(config_version_id) as oldest_version
        FROM classifications
        WHERE config_version_id < %s
    """
    total_rows = postgres.execute_query(total_behind_query, (current_version,))
    total_behind = total_rows[0]["count"] if total_rows else 0
    oldest_version = total_rows[0]["oldest_version"] if total_rows else current_version

    # Non-priority count (emails needing actual re-triage)
    non_priority_count = total_behind - priority_count

    return jsonify(
        {
            "current_version": current_version,
            "emails_behind": total_behind,
            "priority_senders_count": priority_count,
            "non_priority_count": non_priority_count,
            "oldest_version": oldest_version,
        }
    )


# Answering a trash_sender question (cortex-gco6). The triage labeling worker
# owns these tables and opens the questions; an answer records Devon's decision
# and re-enqueues the mail it releases, which the labeling worker then trashes.
TRASH_ANSWERS = {
    "sender": ("approved", "approved"),  # sender decision, pending rows become
    "once": (None, "approved"),
    "ignore": ("rejected", "kept"),
}


@triage_bp.route("/questions")
def list_questions():
    """Open questions, largest first. Only kind trash_sender exists so far."""
    status = request.args.get("status", "open")
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = int(request.args.get("offset", 0))
    except ValueError:
        limit = offset = -1
    if limit < 0 or not 0 <= offset <= 1_000_000:
        return jsonify({"error": "limit and offset must be integers in range"}), 400
    rows = postgres.execute_query(
        """
        SELECT q.id, q.kind, q.status, q.asked_at, q.from_addr,
               COUNT(t.gmail_id) AS volume,
               COALESCE(json_agg(json_build_object(
                   'gmail_id', t.gmail_id, 'subject', p.subject,
                   'date_header', p.date_header)
                 ORDER BY t.queued_at DESC) FILTER (WHERE t.gmail_id IS NOT NULL),
                 '[]') AS sample,
               COUNT(*) OVER () AS total
        FROM triage_questions q
        LEFT JOIN triage_trash_queue t
          ON t.from_addr = q.from_addr AND t.status = 'pending'
        LEFT JOIN emails_parsed p ON p.gmail_id = t.gmail_id
        WHERE q.status = %s
        GROUP BY q.id
        ORDER BY volume DESC, q.asked_at
        LIMIT %s OFFSET %s
        """,
        (status, limit, offset),
    )
    questions = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "status": r["status"],
            "asked_at": r["asked_at"].isoformat(),
            "from_addr": r["from_addr"],
            "volume": r["volume"],
            "sample": r["sample"][:5],
            "proposed": None,
            "llm_category": None,
            "confidence": None,
            "reasoning": None,
        }
        for r in rows
    ]
    total = rows[0]["total"] if rows else 0
    return jsonify({"questions": questions, "limit": limit, "offset": offset, "total": total})


@triage_bp.route("/questions/<question_id>/answer", methods=["POST"])
def answer_question(question_id: str) -> Any:
    """Answer a trash_sender question: scope sender, once or ignore."""
    created_by = request.headers.get("X-Created-By")
    if not created_by:
        return jsonify({"error": "Missing X-Created-By header"}), 400
    scope = (request.get_json(silent=True) or {}).get("scope")
    if scope not in TRASH_ANSWERS:
        return jsonify({"error": f"scope must be one of {sorted(TRASH_ANSWERS)}"}), 400
    decision, row_status = TRASH_ANSWERS[scope]

    with ConnectionContext() as conn, conn.cursor() as cur:
        # The labeling worker's trash gate takes the same lock, so this decision
        # never lands between its read and its write.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('triage_trash_gate'))")
        cur.execute(
            "SELECT kind, from_addr FROM triage_questions"
            " WHERE id = %s AND status = 'open' FOR UPDATE",
            (question_id,),
        )
        row = cur.fetchone()
        if row is None or row[0] != "trash_sender":
            conn.rollback()
            return jsonify({"error": "no open trash_sender question with that id"}), 404
        from_addr = row[1]
        if decision:
            cur.execute(
                """
                INSERT INTO triage_trash_senders (email_address, decision, decided_by)
                VALUES (%s, %s, %s)
                ON CONFLICT (email_address) DO UPDATE
                   SET decision = EXCLUDED.decision, decided_by = EXCLUDED.decided_by,
                       decided_at = now()
                """,
                (from_addr, decision, created_by),
            )
        cur.execute(
            "UPDATE triage_trash_queue SET status = %s, updated_at = now()"
            " WHERE from_addr = %s AND status = 'pending' RETURNING gmail_id",
            (row_status, from_addr),
        )
        released = [r[0] for r in cur.fetchall()] if row_status == "approved" else []
        if released:
            cur.execute(
                "INSERT INTO queue (queue_name, payload, status, priority)"
                " SELECT 'labeling', jsonb_build_object('gmail_id', g), 'pending', 0"
                " FROM unnest(%s::text[]) AS g",
                (released,),
            )
        cur.execute(
            "UPDATE triage_questions SET status = 'answered', answered_at = now(),"
            " answer = jsonb_build_object('scope', %s::text, 'by', %s::text)"
            " WHERE id = %s",
            (scope, created_by, question_id),
        )
        conn.commit()
    logger.info(f"Trash question {question_id} ({from_addr}) answered {scope}")
    return jsonify({"answered": question_id, "scope": scope, "rows_enqueued": len(released)})
