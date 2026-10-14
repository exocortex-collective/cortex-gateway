# queue-sql-contract-reviewer
model: sonnet
verification: evidence-query
**Checks:** Raw SQL in blueprints that names columns, statuses or state transitions the owning schema does not define, or returns queue rows to pending without releasing the claim.
**Spawn when:** diff adds or changes SQL string literals (SELECT/INSERT/UPDATE/DELETE) in `src/gateway/blueprints/` or `src/gateway/services/postgres.py`; skip tests-only and docs-only diffs.

**Proof:** for every table.column in changed SQL, grep cortex_utils (queue/schema.py REQUIRED_COLUMNS, triage_config) or the migration source and quote the defining line; any column with no definition is a finding.

**Your focus:** The gateway owns no DDL; it queries tables defined in cortex-utils and postmark. Past incidents: non-existent `updated_at`, `error` vs `last_error`, cancel endpoints targeting states that never exist.

**What to flag:**
1. (P1) A column referenced in a changed statement with no definition in the owning schema (`queue`, `backfill_jobs`, `emails_*`, `classifications`, `triage_*`).
2. (P1) An UPDATE on `queue` setting `status='pending'` that does not also null `claimed_at` and `claimed_by` (see tests/test_claim_token_reset.py for the rule and its scan).
3. (P2) Status literals (`'pending'`, `'processing'`, `'failed'`, `'dead'`) in a WHERE or SET that no worker ever writes, making an endpoint a silent no-op.
4. (P2) A dynamic `f"""` query whose interpolated fragments are not from a closed internal set (identifiers/orderings), with values not passed as parameters.
5. (P2) A new write with no accompanying test that asserts the SQL text or runs against a fixture schema.

**Proof:** for each flagged column or status, quote the grep over cortex_utils (or state that the dependency source was unavailable and mark judgement).

**Do not flag:** columns that resolve in the schema; parameterised values; read-only reporting queries over columns confirmed present; style of SQL formatting.
