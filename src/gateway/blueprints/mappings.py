"""Email mapping management API endpoints.

Provides CRUD operations for priority and fallback email mappings.
Changes to mappings automatically trigger targeted re-enqueue by sender.
"""

import os
from typing import Any

from cortex_utils.logging import get_logger
from flask import Blueprint, Response, jsonify, request
from psycopg2.extras import RealDictCursor

from gateway.services.postgres import ConnectionContext, execute_query

logger = get_logger()

mappings_bp = Blueprint("mappings", __name__)

# Re-triage Devon starts (a mapping change or a question answer): below live
# mail (0) so it never delays it, above backfill (-100) so his answer shows up
# within minutes instead of after the whole backfill drains.
MAPPING_CHANGE_REPROCESS_PRIORITY = -50

# Delay before the re-enqueued jobs become claimable. A mapping change enqueues
# the sender's mail AND signals the triage worker to reload mappings in one
# transaction; without this delay the worker can claim the jobs before it
# processes the reload signal and re-triages against stale mappings (the sender
# falls through to the default label). Holding the jobs a short while guarantees
# the worker's mappings_reload (polled once per batch, seconds) lands first.
# NOTE: this relies on a single triage worker — signals are single-consumer
# (FOR UPDATE SKIP LOCKED), so a second replica could claim the delayed jobs
# without having reloaded. Horizontal scaling would need the deterministic
# "worker re-enqueues on reload" approach instead. See cortex-tczh.
MAPPING_CHANGE_REPROCESS_DELAY_SECONDS = int(
    os.environ.get("MAPPING_CHANGE_REPROCESS_DELAY_SECONDS", "60")
)


@mappings_bp.route("", methods=["GET"])
def list_mappings() -> Response | tuple[Response, int]:
    """List all email mappings.

    Query params:
        type: priority | fallback (optional, filter by type)
        limit: Max results (default: 100, max: 1000)
        offset: Pagination offset (default: 0)

    Returns:
        200: List of mappings with metadata
    """
    mapping_type = request.args.get("type")
    limit_param = request.args.get("limit", 100, type=int)
    offset = request.args.get("offset", 0, type=int)

    # Validate parameters
    if limit_param <= 0 or offset < 0:
        return jsonify({"error": "limit must be positive and offset must be non-negative"}), 400

    limit = min(limit_param, 1000)

    query = """
        SELECT
            id,
            mapping_type,
            email_address,
            label,
            archive,
            mark_read,
            created_at,
            updated_at,
            created_by,
            updated_by,
            COUNT(*) OVER() as total
        FROM triage_email_mappings
        WHERE deleted_at IS NULL
    """
    params: list[str | int] = []

    if mapping_type:
        if mapping_type not in ("priority", "fallback"):
            return jsonify({"error": "type must be 'priority' or 'fallback'"}), 400
        query += " AND mapping_type = %s"
        params.append(mapping_type)

    query += " ORDER BY mapping_type, email_address LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    rows = execute_query(query, tuple(params))

    # Extract total and remove it from mapping records
    total = rows[0]["total"] if rows else 0
    mappings = [{k: v for k, v in row.items() if k != "total"} for row in rows]

    return jsonify(
        {
            "mappings": mappings,
            "limit": limit,
            "offset": offset,
            "total": total,
        }
    )


@mappings_bp.route("", methods=["POST"])
def add_mapping() -> Response | tuple[Response, int]:
    """Add a new email mapping.

    Request body:
        {
            "type": "priority" | "fallback",
            "email": "foo@example.com",
            "label": "Label/Name",
            "archive": true | false | null (optional),
            "mark_read": true | false | null (optional)
        }

    Request headers:
        X-Created-By: user identifier (required)

    Returns:
        201: Mapping created, re-enqueue triggered
        400: Invalid input
        409: Mapping already exists
        500: Internal error
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    created_by = request.headers.get("X-Created-By")
    if not created_by:
        return jsonify({"error": "Missing X-Created-By header"}), 400

    # Validate required fields
    if not all(k in data for k in ["type", "email", "label"]):
        return jsonify({"error": "Missing required fields: type, email, label"}), 400

    mapping_type = data["type"]
    if not isinstance(mapping_type, str) or mapping_type.strip() not in ("priority", "fallback"):
        return jsonify({"error": "type must be a string: 'priority' or 'fallback'"}), 400

    email_raw = data["email"]
    if not isinstance(email_raw, str) or not email_raw.strip():
        return jsonify({"error": "email must be a non-empty string"}), 400
    email = email_raw.lower().strip()

    label_raw = data["label"]
    if not isinstance(label_raw, str) or not label_raw.strip():
        return jsonify({"error": "label must be a non-empty string"}), 400
    label = label_raw.strip()

    archive = data.get("archive")
    if "archive" in data and not (isinstance(archive, bool) or archive is None):
        return jsonify({"error": "archive must be a boolean or null"}), 400

    mark_read = data.get("mark_read")
    if "mark_read" in data and not (isinstance(mark_read, bool) or mark_read is None):
        return jsonify({"error": "mark_read must be a boolean or null"}), 400

    try:
        with ConnectionContext() as conn:
            with conn.cursor() as cursor:
                created = create_mapping(
                    cursor, mapping_type, email, label, archive, mark_read, created_by
                )
                if created is None:
                    return jsonify({"error": "Mapping for this email and type already exists"}), 409
                mapping_id, rows_enqueued = created

            conn.commit()

        logger.info(
            "Mapping created",
            mapping_id=mapping_id,
            email=email,
            mapping_type=mapping_type,
            label=label,
            rows_enqueued=rows_enqueued,
            created_by=created_by,
        )

        return (
            jsonify(
                {
                    "message": "Mapping created and emails enqueued for re-triage",
                    "mapping_id": mapping_id,
                    "email": email,
                    "type": mapping_type,
                    "rows_enqueued": rows_enqueued,
                }
            ),
            201,
        )

    except Exception as e:
        logger.error("Failed to create mapping", error=str(e), exc_info=True)
        return jsonify({"error": "Failed to create mapping"}), 500


@mappings_bp.route("/<int:mapping_id>", methods=["PUT"])
def update_mapping(mapping_id: int) -> Response | tuple[Response, int]:
    """Update an existing mapping.

    Request body (all fields optional, only provide fields to change):
        {
            "type": "priority" | "fallback",  # Move between types
            "label": "New/Label",
            "archive": true | false | null,
            "mark_read": true | false | null
        }

    Request headers:
        X-Updated-By: user identifier (required)

    Returns:
        200: Mapping updated, re-enqueue triggered
        404: Mapping not found
        400: Invalid input
        409: Conflict (email already exists in target type)
        500: Internal error
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    updated_by = request.headers.get("X-Updated-By")
    if not updated_by:
        return jsonify({"error": "Missing X-Updated-By header"}), 400

    # Build UPDATE query dynamically
    updates = []
    params: list[str | int | bool | None] = []

    if "type" in data:
        mapping_type = data["type"]
        if not isinstance(mapping_type, str) or mapping_type.strip() not in (
            "priority",
            "fallback",
        ):
            return jsonify({"error": "type must be 'priority' or 'fallback'"}), 400
        updates.append("mapping_type = %s")
        params.append(mapping_type.strip())

    if "label" in data:
        label = data["label"]
        if not isinstance(label, str) or not label.strip():
            return jsonify({"error": "label must be a non-empty string"}), 400
        updates.append("label = %s")
        params.append(label.strip())

    if "archive" in data:
        archive = data["archive"]
        if not (isinstance(archive, bool) or archive is None):
            return jsonify({"error": "archive must be a boolean or null"}), 400
        updates.append("archive = %s")
        params.append(archive)

    if "mark_read" in data:
        mark_read = data["mark_read"]
        if not (isinstance(mark_read, bool) or mark_read is None):
            return jsonify({"error": "mark_read must be a boolean or null"}), 400
        updates.append("mark_read = %s")
        params.append(mark_read)

    if not updates:
        return jsonify({"error": "No fields to update"}), 400

    updates.append("updated_by = %s")
    params.append(updated_by)
    updates.append("updated_at = NOW()")

    params.append(mapping_id)

    try:
        with ConnectionContext() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                # Update mapping and get data for side-effects
                query = f"""
                    UPDATE triage_email_mappings
                    SET {", ".join(updates)}
                    WHERE id = %s AND deleted_at IS NULL
                    RETURNING *
                """
                cursor.execute(query, tuple(params))
                updated = cursor.fetchone()

                if not updated:
                    return jsonify({"error": "Mapping not found"}), 404

                email = updated["email_address"]

                # Trigger side-effects for mapping change
                rows_enqueued = _trigger_mapping_change_side_effects(cursor, email)

            conn.commit()

        logger.info(
            "Mapping updated",
            mapping_id=mapping_id,
            email=email,
            rows_enqueued=rows_enqueued,
            updated_by=updated_by,
        )

        return jsonify(
            {
                "message": "Mapping updated and emails enqueued for re-triage",
                "mapping": updated,
                "rows_enqueued": rows_enqueued,
            }
        )

    except Exception as e:
        # Check for unique constraint violation (changing type to one that already exists)
        if hasattr(e, "pgcode") and e.pgcode == "23505":
            return jsonify({"error": "Mapping with this email and type already exists"}), 409
        logger.error("Failed to update mapping", error=str(e), exc_info=True)
        return jsonify({"error": "Failed to update mapping"}), 500


@mappings_bp.route("/<int:mapping_id>", methods=["DELETE"])
def delete_mapping(mapping_id: int) -> Response | tuple[Response, int]:
    """Delete (soft-delete) a mapping.

    Request headers:
        X-Updated-By: user identifier (required)

    Returns:
        200: Mapping deleted, re-enqueue triggered
        404: Mapping not found
        500: Internal error
    """
    updated_by = request.headers.get("X-Updated-By")
    if not updated_by:
        return jsonify({"error": "Missing X-Updated-By header"}), 400

    try:
        with ConnectionContext() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                # Soft delete
                cursor.execute(
                    """
                    UPDATE triage_email_mappings
                    SET deleted_at = NOW(), updated_by = %s
                    WHERE id = %s AND deleted_at IS NULL
                    RETURNING email_address
                    """,
                    (updated_by, mapping_id),
                )

                row = cursor.fetchone()
                if not row:
                    return jsonify({"error": "Mapping not found"}), 404

                email = row["email_address"]

                # Trigger side-effects for mapping change
                rows_enqueued = _trigger_mapping_change_side_effects(cursor, email)

            conn.commit()

        logger.info(
            "Mapping deleted",
            mapping_id=mapping_id,
            email=email,
            rows_enqueued=rows_enqueued,
            updated_by=updated_by,
        )

        return jsonify(
            {
                "message": "Mapping deleted and emails enqueued for re-triage",
                "email": email,
                "rows_enqueued": rows_enqueued,
            }
        )

    except Exception as e:
        logger.error("Failed to delete mapping", error=str(e), exc_info=True)
        return jsonify({"error": "Failed to delete mapping"}), 500


@mappings_bp.route("/history/<email_address>", methods=["GET"])
def get_mapping_history(email_address: str) -> Response | tuple[Response, int]:
    """Get change history for a specific email address.

    Path params:
        email_address: Email address to get history for

    Query params:
        limit: Max results (default: 50, max: 500)
        offset: Pagination offset (default: 0)

    Returns:
        200: History of changes for this email address
        404: No history found
    """
    limit_param = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)

    # Validate parameters
    if limit_param <= 0 or offset < 0:
        return jsonify({"error": "limit must be positive and offset must be non-negative"}), 400

    limit = min(limit_param, 500)

    query = """
        SELECT
            id,
            mapping_id,
            mapping_type,
            email_address,
            label,
            archive,
            mark_read,
            change_type,
            changed_at,
            changed_by,
            previous_label,
            previous_archive,
            previous_mark_read,
            COUNT(*) OVER() as total
        FROM triage_email_mappings_history
        WHERE LOWER(email_address) = LOWER(%s)
        ORDER BY changed_at DESC
        LIMIT %s OFFSET %s
    """

    rows = execute_query(query, (email_address, limit, offset))

    if not rows:
        return jsonify({"error": "No history found for this email address"}), 404

    # Extract total and remove it from history records
    total = rows[0]["total"] if rows else 0
    history = [{k: v for k, v in row.items() if k != "total"} for row in rows]

    return jsonify(
        {
            "email_address": email_address,
            "history": history,
            "limit": limit,
            "offset": offset,
            "total": total,
        }
    )


def create_mapping(
    cursor: Any,
    mapping_type: str,
    email: str,
    label: str,
    archive: bool | None,
    mark_read: bool | None,
    created_by: str,
) -> tuple[int, int] | None:
    """Insert a mapping and trigger its side effects, uncommitted.

    Returns (mapping_id, rows_enqueued), or None if one already exists. Also
    used by the triage question answer route.
    """
    cursor.execute(
        """
        INSERT INTO triage_email_mappings (
            mapping_type, email_address, label, archive, mark_read,
            created_by, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (mapping_type, email_address) WHERE deleted_at IS NULL
        DO NOTHING
        RETURNING id
        """,
        (mapping_type, email, label, archive, mark_read, created_by),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return row[0], _trigger_mapping_change_side_effects(cursor, email)


def _trigger_mapping_change_side_effects(cursor: Any, email_address: str) -> int:
    """Enqueue emails for reprocessing and signal workers to reload mappings.

    Args:
        cursor: Database cursor
        email_address: Email address to trigger side-effects for

    Returns:
        Number of rows enqueued
    """
    rows_enqueued = _enqueue_sender_for_reprocess(
        cursor, email_address, priority=MAPPING_CHANGE_REPROCESS_PRIORITY
    )
    cursor.execute(
        """
        INSERT INTO worker_signals (signal_type, target_worker, created_at)
        VALUES ('mappings_reload', 'triage', NOW())
        ON CONFLICT DO NOTHING
        """
    )
    return rows_enqueued


def _enqueue_sender_for_reprocess(
    cursor: Any, email_address: str, priority: int = MAPPING_CHANGE_REPROCESS_PRIORITY
) -> int:
    """Re-enqueue all emails from a specific sender for triage.

    Args:
        cursor: Database cursor
        email_address: Email address to filter by (exact match, case-insensitive)
        priority: Queue priority (default: MAPPING_CHANGE_REPROCESS_PRIORITY)

    Returns:
        Number of rows enqueued
    """
    # next_attempt_at holds the jobs until after the worker's mappings_reload
    # (which the caller signals in the same transaction) so re-triage sees the
    # fresh mapping, not the stale one. See MAPPING_CHANGE_REPROCESS_DELAY_SECONDS.
    cursor.execute(
        """
        INSERT INTO queue (
            queue_name, payload, priority, status, created_at, next_attempt_at
        )
        SELECT DISTINCT ON (er.gmail_id)
            'triage',
            jsonb_build_object(
                'email_id', er.id,
                'gmail_id', er.gmail_id,
                'rerun', true,
                'reason', 'mapping_change'
            ),
            %s,
            'pending',
            NOW(),
            NOW() + (INTERVAL '1 second' * %s)
        FROM emails_raw er
        JOIN emails_parsed ep ON ep.gmail_id = er.gmail_id
        WHERE LOWER(ep.from_addr) = LOWER(%s)
        AND NOT EXISTS (
            SELECT 1 FROM queue q
            WHERE q.queue_name = 'triage'
              AND q.payload->>'gmail_id' = er.gmail_id
              AND q.status IN ('pending', 'processing')
        )
        """,
        (priority, MAPPING_CHANGE_REPROCESS_DELAY_SECONDS, email_address),
    )

    return cursor.rowcount
