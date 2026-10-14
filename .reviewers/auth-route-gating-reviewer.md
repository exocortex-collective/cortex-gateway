# auth-route-gating-reviewer
model: sonnet
verification: mixed
**Checks:** New or changed Flask routes and auth allowlist edits that open the API to unauthenticated or UI-scoped callers, or leave a route unclassified.
**Spawn when:** diff adds or changes an `@bp.route` / `@app.route` decorator, or edits `src/gateway/auth.py` (EXEMPT_PATHS, UI_ALLOWED_EXACT, UI_ALLOWED_PATTERNS, token comparison, CORS); skip otherwise.

**Proof:** route classification and exempt-path matching are evidence-queries (enumerate url_map or grep decorators and compare to the allowlists); threat-model objections are judgement.

**Your focus:** The gateway's two-lock gate (port, then source address, then bearer token) and the scoped UI token allowlist. A route is closed unless deliberately opened; the burden is on the diff to show it should be.

**What to flag:**
1. (P1) A new path added to `EXEMPT_PATHS` or matched by a bare `startswith` / prefix test without a segment boundary.
2. (P1) A route that mutates state, spends Gmail quota, rewrites config/mappings, or exposes server paths added to `UI_ALLOWED_*`. The comment in auth.py lists the deliberately excluded classes; follow it.
3. (P2) A new route whose method+path matches neither allowlist and whose intended caller (UI vs internal peers vs token holder) the PR does not state, or an allowlist entry whose regex is unanchored or admits `/` in an id segment.
4. (P1) Token or header comparison that is not constant-time bytes compare, or an auth failure path that can raise instead of 401/403.
5. (P2) CORS origins widened beyond the configured UI origin, or `*` with credentials.

**Proof:** list routes in the diff (decorator + methods), then show each one's classification by quoting the allowlist entries it matches or does not match; for regexes, give an input that matches unintentionally.

**Do not flag:** internal-port-only routes that are documented as peer traffic; allowlist entries for routes not yet served (documented inert); reading `request.headers` for audit attribution (`X-Created-By`).
