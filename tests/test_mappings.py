"""Tests for mapping-change side effects (cortex-tczh: reprocess/reload race)."""

from unittest.mock import MagicMock

from gateway.blueprints.mappings import (
    MAPPING_CHANGE_REPROCESS_DELAY_SECONDS,
    MAPPING_CHANGE_REPROCESS_PRIORITY,
    _enqueue_sender_for_reprocess,
    _trigger_mapping_change_side_effects,
)


def test_reprocess_delays_jobs_past_reload() -> None:
    """Re-enqueued jobs get a next_attempt_at delay so the worker's
    mappings_reload lands before they're claimed (cortex-tczh)."""
    cur = MagicMock()
    cur.rowcount = 3
    assert _enqueue_sender_for_reprocess(cur, "bob@x.com") == 3
    sql, params = cur.execute.call_args.args
    assert "next_attempt_at" in sql
    assert "INTERVAL '1 second' * %s" in sql
    # (priority, delay_seconds, email) — delay is the middle bind.
    assert params == (
        MAPPING_CHANGE_REPROCESS_PRIORITY,
        MAPPING_CHANGE_REPROCESS_DELAY_SECONDS,
        "bob@x.com",
    )


def test_side_effects_enqueue_then_signal_reload() -> None:
    """The side-effect both re-enqueues (delayed) and signals a mappings_reload."""
    cur = MagicMock()
    cur.rowcount = 2
    assert _trigger_mapping_change_side_effects(cur, "bob@x.com") == 2
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert any("next_attempt_at" in s for s in sqls)  # the delayed re-enqueue
    assert any("worker_signals" in s and "mappings_reload" in s for s in sqls)


def test_devon_started_retriage_outranks_backfill_but_not_live_mail() -> None:
    """Live mail is 0 and backfill -100 (cortex CLAUDE.md); an answer Devon
    gives must land before the backfill drains, without delaying live mail."""
    assert -100 < MAPPING_CHANGE_REPROCESS_PRIORITY < 0
