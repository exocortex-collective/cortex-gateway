-- Copied from cortex-triage src/triage/services/trash_gate.py SCHEMA, which owns
-- these tables (the labeling worker creates them). Keep in step with it.
CREATE TABLE IF NOT EXISTS triage_trash_senders (
    email_address TEXT PRIMARY KEY,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by TEXT
);
CREATE TABLE IF NOT EXISTS triage_trash_queue (
    gmail_id TEXT PRIMARY KEY,
    from_addr TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'approved', 'cleared', 'kept')),
    queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trash_queue_pending
    ON triage_trash_queue (from_addr) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS triage_questions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered')),
    from_addr TEXT NOT NULL,
    asked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    answered_at TIMESTAMPTZ,
    answer JSONB
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_triage_questions_open
    ON triage_questions (kind, from_addr) WHERE status = 'open';
