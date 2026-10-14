# postgres-write-commit-reviewer
model: sonnet
verification: mutation
**Checks:** SQL INSERT/UPDATE/DELETE (including `... RETURNING`) routed through a helper that does not commit, so the endpoint reports success for a discarded write.
**Spawn when:** diff adds or changes `INSERT`, `UPDATE`, `DELETE`, or `RETURNING` SQL, or any call to `execute_query`/`execute_one`/`execute_update*`, in `src/gateway/`; skip tests-only and docs-only diffs.

**Your focus:** Whether every write in gateway code actually commits. In this repo `execute_query` and `execute_one` never commit and `ConnectionContext` only rolls back on exceptions, so psycopg2 discards the write when the connection returns to the pool while `RETURNING` still yields a row (the #37 incident: 201 + job id for a job never stored).

**What to flag (P1):**
1. A statement beginning INSERT/UPDATE/DELETE (or a CTE containing one) passed to `execute_query` or `execute_one`, instead of `execute_update` / `execute_update_returning`.
2. A new helper or direct `conn.cursor()` write path with no `conn.commit()` on the success path.
3. A handler that does a read-after-write using separate pooled connections without committing the first.
4. Multi-statement writes (e.g. mapping row plus history row) split across separate helper calls, so one can commit and the other fail.

**Proof:** list the write statements in the diff, name the helper each uses, and quote that helper's body showing whether it calls commit. For a mutation proof, remove the commit in a scratch copy and show the test still passes (a test blind to commits); label anything else judgement.

**Do not flag:** SELECT-only use of `execute_query`; writes through `execute_update*` that commit; test doubles; transaction scope choices that are consistent within a handler.
