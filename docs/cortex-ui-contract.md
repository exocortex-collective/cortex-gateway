# Cortex UI contract v1

*cortex-improvements → cryo-frontend, 2026-10-13. Build against this. Base: `http://<gateway-host>:8097` (host 8097 → container 8098, the token-required socket). All responses `application/json` unless stated. All timestamps are emitted by `flask.jsonify`, which with no custom provider serialises datetimes as **RFC-822** (`"Tue, 13 Oct 2026 10:00:00 GMT"`), not ISO — parse accordingly. I did not change that; say the word and I will add an ISO provider instead.*

## 0. One thing to settle before I build it: the token in the browser

Devon's call is the extension calls cortex directly, and I am building that. But "make that safe" cannot be satisfied with the token that exists today, so the contract specifies **two** tokens.

The gateway currently holds exactly one: `config.py:41 api_token = os.environ["CORTEX_API_TOKEN"]`, compared with `hmac.compare_digest` (`auth.py:245-249`). It is omnipotent. A copy of it in `chrome.storage` can `PUT /config` (rewrite every triage rule in one call), `DELETE /mappings/<id>`, `POST /sync/backfill`, `POST /oauth/refresh`, and `GET /oauth/status`, which returns a **server filesystem path**. Browser-extension storage is readable by anything with the profile and by any extension holding `management`/`debugger` permission.

So: **`CORTEX_UI_TOKEN`**, a second token accepted **only** for the route allowlist in §3. Any other route presented with it returns **403**, distinct from 401 so the UI can tell "wrong token" from "not allowed". `CORTEX_API_TOKEN` keeps working everywhere and stays out of the browser. This is one branch in `auth.py:_require_token` and widens nothing.

## 1. CORS

| Item | Value |
|---|---|
| Env | `CORTEX_UI_ORIGINS` — comma-separated exact origins. Send me the extension ID; it becomes `chrome-extension://<id>` |
| `Access-Control-Allow-Origin` | the **exact** matched origin, echoed. Never `*` |
| `Vary` | `Origin`, always, on every response |
| `Access-Control-Allow-Headers` | `Authorization, Content-Type, X-Created-By, X-Updated-By` |
| `Access-Control-Allow-Methods` | `GET, POST, DELETE, OPTIONS` |
| `Access-Control-Max-Age` | `600` |
| `Access-Control-Allow-Credentials` | **not sent.** We authenticate with a bearer token, and the gateway sets an OAuth *session cookie* (`oauth.py:219`) that must never be rideable from a page |
| Non-allowlisted origin | no CORS headers at all; the browser blocks it. No new 403 path, no origin reflection |

**Preflight.** `OPTIONS` currently **401s**: `_require_token` is a `before_request` over every route and `_is_exempt` matches on *path only* (`auth.py:99-104, 227`) — so without a change every single request from the extension fails at preflight. `OPTIONS` will short-circuit **before** the token check and return **204** with CORS headers and **no body**. It reveals nothing: the same 204 comes back for a path that does not exist.

## 2. Status codes, uniform across every route below

| Code | Meaning |
|---|---|
| 200 | OK. 201 where noted |
| 204 | preflight only |
| 400 | malformed body or parameter, `{"error":"..."}` |
| 401 | no/invalid token, `{"error":"authentication required","detail":"..."}` |
| 403 | valid `CORTEX_UI_TOKEN` on a route outside its allowlist, `{"error":"token not permitted for this route","path":"..."}` |
| 404 | no such id |
| 409 | conflict (duplicate mapping) |
| 500 | `{"error":"..."}` |

Bulk actions return **200 with per-item results**, never a partial-failure code — mirroring cryo's `/api/queue/resubmit`.

## 3. Routes

### 3.1 Queues — phase 2

`GET /queue/health` → cryo `/api/queue`'s shape, so `renderQueue()` works unchanged.
```json
{"queues":[{"queue":"triage","ready":0,"deferred":0,"processing":1,"failed":2,"oldest_ready_age_s":0}],
 "dead_letter":7,"is_healthy":true,"server_time":"..."}
```
Wraps `cortex_utils.queue.inspect.health()`. **New.**

`GET /queue/dead?limit=50&include_resolved=false` → `{"dead":[{...}]}`, row:
`id, original_id, queue, ref, attempts, error, created_at, failed_at, dismissed_at, retried_at, retried_as`.
Wraps `DeadLetterManager.list_jobs`. `limit` clamps to 100. **New.**

`GET /queue/stuck?limit=50` → `{"stuck":[...]}` — claimed, never completed. Wraps `inspect.stuck()`. **New.**

`POST /queue/dead/resubmit` body `{"ids":[int,...]}` (1–100) → `{"results":[{"id":1,"ok":true,"new_id":42}|{"id":2,"ok":false,"why":"..."}],"resubmitted":1}`. Wraps `retry_jobs`; `why` comes from `why_not_retryable`. **New.**

`POST /queue/dead/dismiss` same body → `{"results":[...],"dismissed":N}`. **New.**

Already live, unchanged: `GET /queue/stats` → `{"queues":{"<name>":{"<status>":int}}}`; `GET /queue/failed?limit=&offset=&queue=`; `POST /queue/failed/<id>/retry`; `POST /queue/failed/retry-all?queue=`; `DELETE /queue/failed/<id>`.

**`ref`, not payload.** cryo deliberately forwards only a `ref` because payloads hold private URLs. Cortex payloads hold `gmail_id`, so `ref` = the `gmail_id`. The subject is **not** included; fetch it from `GET /emails/<gmail_id>` if you want it.

### 3.2 Rerun preview — phase 2

`POST /triage/rerun?preview=1` — identical body to the live route (`gmail_ids` | `senders` | `label`, plus `days`, `force`, `priority`), but **enqueues nothing** and returns the count it would have enqueued:
```json
{"preview":true,"count":412,"senders":["*@example.com"],"days":7,"force":false}
```
Without `?preview=1` the route behaves exactly as today. Also fixing while I am in there: `days` and `priority` are currently unvalidated and a string `days` 500s inside `timedelta` — both become 400.

### 3.3 Questions — phase 3

**A question is about a SENDER, not an email, and that is forced by measurement.** Measured on Hades today:

| | |
|---|---|
| classifications total | 94,819 |
| fell through to `default_label` | **38,442** (40%) |
| distinct senders behind those | **10,664** |
| senders with ≥10 such emails | **617**, covering **25,039** emails (65% of the backlog) |
| `llm_confidence IS NOT NULL` | **0** |

Per-email questions would be 38,442 items. Per-sender at ≥10 is 617, each answer settling ~40 emails at once — and it matches the answer path, since `/mappings` is keyed by email address.

**On Devon's confidence trigger: it currently fires on nothing.** `llm_confidence` is NULL for all 94,819 rows, because no active rule uses the `llm_config` path that records it — the 13 `email_category` rules call `categorize_email`, which returns a bare category and no confidence. I am building the trigger anyway so it works the moment such a rule exists; it is inert today, which is worth knowing before you build a UI expecting it to populate.

| Param | Default | Env |
|---|---|---|
| min uncategorized volume per sender | **10** | `CORTEX_QUESTION_MIN_VOLUME` |
| confidence threshold | **0.70** | `CORTEX_QUESTION_CONFIDENCE` |

`GET /triage/questions?status=open&limit=50&offset=0` → ordered by `volume` desc:
```json
{"questions":[{
  "id":"q-3f8a","kind":"uncategorized_sender","status":"open","asked_at":"...",
  "from_addr":"noreply@example.com","volume":412,
  "sample":[{"gmail_id":"19ae...","subject":"...","date_header":"..."}],
  "proposed":{"label":null,"archive":false,"mark_read":false},
  "llm_category":null,"confidence":null,"reasoning":null
}],"limit":50,"offset":0,"total":617}
```
`kind` ∈ `uncategorized_sender` | `low_confidence`. `proposed` is advisory and may be all-null; the model only proposes and nothing acts on it unanswered.

`POST /triage/questions/<id>/answer` body:
```json
{"scope":"sender","label":"Cortex/Commercial/Newsletter","archive":true,"mark_read":true,"mapping_type":"fallback"}
```
- `scope:"sender"` → performs the existing **`POST /mappings`** write. That is the code-enforced precedent: transactional, audited to `triage_email_mappings_history`, inserts a `worker_signals` reload, and re-enqueues that sender's mail at priority −200 so it applies retroactively.
- `scope:"once"` → re-enqueues only the sampled `gmail_id`s, writes no mapping.
- `scope:"ignore"` → closes the question, writes nothing, suppresses it from `status=open`.

→ `200 {"answered":"q-3f8a","scope":"sender","mapping_id":851,"rows_enqueued":412}`
→ `404` unknown or already-answered id; `400` missing `label` when scope is `sender`; `409` a mapping already exists for that address (the UI should then offer `PUT /mappings/<id>`).

`X-Created-By` is required on the answer and is passed through to the mapping, so the audit row names the UI. Note it is caller-asserted — the gateway does not tie it to the token.

### 3.4 Read-only, already live, in the UI allowlist

`GET /triage/convergence/status` → `{current_version, emails_behind, priority_senders_count, non_priority_count, oldest_version}`
`GET /triage/stats`, `GET /triage/classifications?limit=&offset=&label=&action=`
`GET /config/versions?limit=&offset=`, `GET /config/diff/<v1>/<v2>`
`GET /mappings?type=&limit=&offset=`, `GET /mappings/history/<email>`
`GET /emails/<gmail_id>`, `GET /emails/<gmail_id>/text`
`GET /health` (unauthenticated)

**Do not use `GET /emails/?label=`** — it filters `label_ids @> %s::jsonb` while the column is `TEXT[]` (`blueprints/emails.py:179`). I have not confirmed it against a live DB, but the operator looks wrong. Use `GET /emails/by-label/<label_id>`, which correctly uses `= ANY(label_ids)`.

### 3.5 `CORTEX_UI_TOKEN` allowlist — everything else is 403

```
GET    /queue/health  /queue/stats  /queue/failed  /queue/dead  /queue/stuck
POST   /queue/dead/resubmit  /queue/dead/dismiss  /queue/failed/<id>/retry
GET    /triage/questions
POST   /triage/questions/<id>/answer
POST   /triage/rerun            (preview=1 ONLY; without it -> 403)
GET    /triage/convergence/status  /triage/stats  /triage/classifications
GET    /config/versions  /config/diff/<v1>/<v2>
GET    /mappings  /mappings/history/<email>
POST   /mappings                 (the answer path needs it)
GET    /emails/<gmail_id>  /emails/<gmail_id>/text  /emails/by-label/<id>
GET    /health
```
Deliberately excluded: all of `PUT/POST /config`, `/config/rollback`, `DELETE /mappings/<id>`, `PUT /mappings/<id>`, every `/sync/*`, every `/backfill/*`, every `/oauth/*`, `POST /queue/failed/retry-all`, `DELETE /queue/failed/<id>`. Those stay with `CORTEX_API_TOKEN`.

## 4. Build order

1. **CORS + `CORTEX_UI_TOKEN`** (`auth.py`, one branch + an OPTIONS short-circuit). Nothing else can be tested until preflight passes.
2. **`/queue/health`, `/queue/dead`, `/queue/stuck`, `dead/resubmit`, `dead/dismiss`** — thin HTTP over `cortex_utils.queue`; the library functions all exist.
3. **`?preview=1`** plus the `days`/`priority` validation.
4. **Question queue** — new table `triage_questions`, a generator, two routes.

Each ships as its own PR on `cortex-gateway` (plus one on `cortex-utils` if the shaping layer moves there, per the library rule). Merges go to Devon.

## 5. What I need from you

1. **The extension ID**, for `CORTEX_UI_ORIGINS`.
2. Confirm you are happy with **two tokens** — the extension stores `CORTEX_UI_TOKEN`, not `CORTEX_API_TOKEN`.
3. RFC-822 timestamps: parse them, or shall I add an ISO provider? Your call, it is a one-line change but it alters every existing response.

---

## v1.1 — what shipped, and three deltas from the above

Built on `cortex-gateway`: **PR #40** (CORS + scoped token) → main, and **PR #41** (queue routes + preview) stacked on #40. 164 tests, ruff and mypy clean. Merges are Devon's.

Deltas, all small, all in your favour:

1. **`/queue/dead` and `/queue/stuck` also return `limit` and `count`** alongside the array, matching the pagination shape the other gateway list routes already use.
2. **`POST /triage/rerun` now returns `"preview": false` on the normal path**, not just `true` on the preview path. That is a change to an *existing* response shape — harmless to ignore, but you can branch on one key instead of checking for its presence.
3. **`days` and `priority` are now validated**: non-integer, bool, or `days < 1` returns **400** instead of the previous 500 from inside `timedelta`. If you were relying on a 500 to detect bad input, it is a 400 now.

Confirmed by test, since you chose to keep it: timestamps really do serialise RFC-822 — `"Tue, 13 Oct 2026 12:00:00 GMT"` — and that is now pinned so it cannot drift under you.

**Not yet live on hades.** It needs: both PRs merged, the image built, stack 48 redeployed, and two env values set — `CORTEX_UI_ORIGINS=chrome-extension://<extension-id>` and `CORTEX_UI_TOKEN`, whose value Devon sets himself. It is not in any diff and has not passed through me.

One behaviour to know before you wire the UI: with `CORTEX_UI_TOKEN` unset the gateway logs `ui_token_without_origins` or simply never accepts a UI token, so the extension gets **401** rather than a confusing partial failure. A valid UI token on a non-allowlisted route gets **403** with `{"error":"token not permitted for this route","path":"..."}` — treat that as a bug in the UI, not a credential problem, and don't prompt the user to re-enter the token on a 403.

---

## v1.2: questions (§3.3) as shipped, cortex-kaij.4

The triage worker opens the questions (`cortex-triage` `services/questions.py`, every 15 minutes), and the gateway lists and answers them. The shapes match §3.3. These points are worth knowing:

1. **`label` may be sent prefixed or not.** Mappings store labels without the prefix and the matcher adds it, so `"Cortex/Commercial/Newsletter"` and `"Commercial/Newsletter"` both store `Commercial/Newsletter`.
2. **`proposed` is never null on these kinds.** With no proposal it is `{"label":null,"archive":false,"mark_read":false}`. Its `label` is non-null only when the config itself writes that label, because the model's own suggestion is not trusted.
3. **`reasoning` and `llm_category` are model text about outside mail.** They are stripped of control and format characters (including bidi overrides) and collapsed to one line, but they are still not trusted. Render them as text.
4. **A sender is asked about once.** After any answer, including `ignore`, the generator never re-opens a question for that sender and kind. A question disappears unanswered if the sender gets a mapping some other way or drops below the volume bar.
5. **`scope:"once"`** re-triages the question's sample, which is the 5 newest emails, not every email from the sender.
6. **`low_confidence` is inert** until a rule records a confidence (§3.3). On Hades on 2026-11-07, 673 senders qualified for `uncategorized_sender`, covering 32,289 emails.
7. **§3.5 is out of date on two routes.** `src/gateway/auth.py` is the source of truth for the allowlist, and it now allows two routes §3.5 excludes:
   - `POST /triage/rerun` without `preview=1`, because Devon decided the UI may start a real rerun (kp9.3).
   - `DELETE /queue/failed/<id>`, because it names a single id and refuses any row whose status is not `failed`.

---

## v1.3: `scope: "later"` (gateway-b874)

Devon wants to put off a question he isn't ready to answer, without `ignore`, which means "never ask again".

- **`POST /triage/questions/<id>/answer`** with body `{"scope":"later"}` (no other fields) returns `200 {"answered":"<id>","scope":"later","snoozed_until":"<ISO 8601>","mapping_id":null,"rows_enqueued":0}`.
  - It snoozes the question for 7 days. It writes no label, no mapping and no re-triage, and any mail a trash question is holding stays held.
  - It works on every kind. 404 if the id is unknown or already answered.
- **`GET /triage/questions?status=open`** leaves snoozed questions out until `snoozed_until` passes, then lists them again. `total` counts only the listed questions. A new top-level field, **`snoozed`**, counts the open questions currently hidden.
- A snoozed question is still open: it can be answered with any scope at any time, and a second `later` restarts the 7 days.
