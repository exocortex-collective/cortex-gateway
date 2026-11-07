"""The Cortex label tree, for picking an answer's label instead of typing one.

Only the managed tree is listed: the labeling worker applies a label only when
it sits under the config's label_prefix, so anything else would be dropped
silently (cortex-gvzf). Mappings store labels without the prefix.
"""

from typing import Any

from flask import Blueprint, jsonify

from gateway.services.postgres import ConnectionContext

labels_bp = Blueprint("labels", __name__)

# gmail_labels is refreshed by sync and lags new labels, so a label a live
# mapping already names counts as known before Gmail has it.
KNOWN = """
SELECT g.name, g.id FROM gmail_labels g WHERE g.name LIKE %(like)s
UNION
SELECT %(prefix)s || '/' || m.label, NULL FROM triage_email_mappings m
WHERE m.deleted_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM gmail_labels g
                  WHERE g.name = %(prefix)s || '/' || m.label)
"""

COUNTS = """
SELECT l, COUNT(*) FROM emails_raw, unnest(label_ids) AS l
WHERE l LIKE 'Label\\_%%' GROUP BY l
"""


def label_prefix(cur: Any) -> str:
    cur.execute("SELECT label_prefix FROM triage_config_versions WHERE is_active")
    row = cur.fetchone()
    return row[0] if row and row[0] else "Cortex"


def known_labels(cur: Any, prefix: str) -> dict[str, str | None]:
    """Full label name -> Gmail id (None until Gmail has it)."""
    cur.execute(KNOWN, {"like": f"{prefix}/%", "prefix": prefix})
    return dict(cur.fetchall())


def check_label(cur: Any, raw: str) -> str:
    """The label to store (unprefixed), or raise ValueError with the reason.

    Accepts "Cortex/A/B" or "A/B". Refuses stray whitespace, empty segments
    and leading or trailing slashes, and a new label whose parent does not
    exist, which is how a typo becomes a new top-level branch.
    """
    prefix = label_prefix(cur)
    label = raw[len(prefix) + 1 :] if raw.startswith(f"{prefix}/") else raw
    segments = label.split("/")
    if not label or any(not s or s != s.strip() for s in segments):
        raise ValueError(
            f"label {raw!r} is malformed: no empty parts, no leading or trailing"
            " slash, no surrounding spaces"
        )
    known = known_labels(cur, prefix)
    full = f"{prefix}/{label}"
    parent = full.rpartition("/")[0]
    if full not in known and parent != prefix and parent not in known:
        raise ValueError(f"{full} would be a new label under {parent}, which does not exist")
    return label


@labels_bp.route("", methods=["GET"])
def list_labels() -> Any:
    """Every label under the prefix, with its parent and message count."""
    with ConnectionContext() as conn, conn.cursor() as cur:
        prefix = label_prefix(cur)
        known = known_labels(cur, prefix)
        cur.execute(COUNTS)
        counts = dict(cur.fetchall())
        conn.rollback()
    labels = [
        {
            "name": name,
            "id": label_id,
            "parent": name.rpartition("/")[0],
            "messages": counts.get(label_id, 0),
            "applied": True,
        }
        for name, label_id in sorted(known.items())
    ]
    return jsonify({"prefix": prefix, "labels": labels, "total": len(labels)})
