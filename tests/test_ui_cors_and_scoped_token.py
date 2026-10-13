"""The browser extension gets a SCOPED token, and CORS is narrow.

The extension calls the gateway directly, so a cortex credential now lives in
chrome.storage. `CORTEX_API_TOKEN` is omnipotent -- one request to PUT /config
rewrites every triage rule, and /oauth/status returns a server filesystem path
-- so the UI gets `CORTEX_UI_TOKEN`, accepted on an allowlist and 403
everywhere else.

Two things here are easy to get wrong and both fail CLOSED in a way that looks
like something else:

1. A CORS preflight carries NO Authorization header -- it cannot -- and
   `_is_exempt` matches on PATH only. Without the OPTIONS branch every
   preflight 401s and the extension cannot make a single successful call,
   which presents as "CORS is broken" rather than "auth rejected it".
2. `Vary: Origin` must be on every response, not just allowlisted ones, or a
   cache can hand an allowlisted response to another origin.
"""

from __future__ import annotations

import pytest
from flask import Flask, jsonify

from gateway.auth import _is_preview, _ui_route_allowed, init_auth

API_TOKEN = "api-token-fixture"
UI_TOKEN = "ui-token-fixture"
ORIGIN = "chrome-extension://jkgpflheabfjgmikaagejgnmopiknooj"
OTHER_ORIGIN = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TRUSTED = "172.26.0.0/16"

# Routes the app under test serves. The gate runs before dispatch, so the
# handlers only need to exist for the allowed cases to come back 200.
ROUTES = (
    ("/health", ("GET",)),
    ("/queue/health", ("GET",)),
    ("/queue/dead", ("GET",)),
    ("/queue/dead/resubmit", ("POST",)),
    ("/queue/failed/5/retry", ("POST",)),
    ("/queue/failed/5", ("DELETE",)),
    ("/queue/failed/retry-all", ("POST",)),
    ("/triage/questions", ("GET",)),
    ("/triage/questions/q-3f8a/answer", ("POST",)),
    ("/triage/rerun", ("POST",)),
    ("/config", ("GET", "PUT")),
    ("/config/versions", ("GET",)),
    ("/config/diff/5/6", ("GET",)),
    ("/mappings", ("GET", "POST")),
    ("/mappings/851", ("PUT", "DELETE")),
    ("/mappings/history/a@b.com", ("GET",)),
    ("/emails/19ae00", ("GET",)),
    ("/oauth/status", ("GET",)),
    ("/sync/backfill", ("POST",)),
)


@pytest.fixture
def client():
    """A real Flask app with the real gate installed.

    init_auth IS the thing under test, so it is wired for real rather than
    stubbed; only the route handlers are fixtures.
    """
    app = Flask(__name__)
    for path, methods in ROUTES:
        app.add_url_rule(
            path,
            endpoint=f"{path}-{'-'.join(methods)}",
            view_func=lambda: jsonify({"ok": True}),
            methods=list(methods),
        )
    init_auth(
        app,
        API_TOKEN,
        trusted_subnets=TRUSTED,
        internal_port=8080,
        ui_token=UI_TOKEN,
        ui_origins=ORIGIN,
    )
    return app.test_client()


def _ui(client, method, path, **kw):
    return client.open(
        path,
        method=method,
        headers={"Authorization": f"Bearer {UI_TOKEN}", "Origin": ORIGIN},
        **kw,
    )


class TestPreflight:
    def test_preflight_succeeds_with_no_credential(self, client):
        """The one that breaks everything if missed: preflight cannot carry a token."""
        r = client.open("/queue/dead", method="OPTIONS", headers={"Origin": ORIGIN})
        assert r.status_code == 204
        assert r.headers["Access-Control-Allow-Origin"] == ORIGIN
        assert "Authorization" in r.headers["Access-Control-Allow-Headers"]
        assert r.data == b""

    def test_preflight_for_an_unknown_path_looks_identical(self, client):
        """So preflight cannot be used to enumerate the route table."""
        known = client.open("/queue/dead", method="OPTIONS", headers={"Origin": ORIGIN})
        unknown = client.open("/does/not/exist", method="OPTIONS", headers={"Origin": ORIGIN})
        assert unknown.status_code == known.status_code == 204
        assert unknown.data == known.data == b""

    def test_preflight_from_another_origin_is_not_served(self, client):
        r = client.open("/queue/dead", method="OPTIONS", headers={"Origin": OTHER_ORIGIN})
        assert r.status_code != 204
        assert "Access-Control-Allow-Origin" not in r.headers


class TestCorsHeaders:
    def test_allow_origin_echoes_the_exact_origin_never_a_wildcard(self, client):
        r = _ui(client, "GET", "/queue/dead")
        assert r.headers["Access-Control-Allow-Origin"] == ORIGIN
        assert r.headers["Access-Control-Allow-Origin"] != "*"

    def test_another_origin_gets_no_cors_header(self, client):
        r = client.get(
            "/queue/dead",
            headers={"Authorization": f"Bearer {UI_TOKEN}", "Origin": OTHER_ORIGIN},
        )
        assert "Access-Control-Allow-Origin" not in r.headers

    def test_vary_origin_is_present_even_without_an_allowed_origin(self, client):
        """A cache must never reuse an allowlisted response for another origin."""
        r = client.get("/health")
        assert "Origin" in r.headers.get("Vary", "")

    def test_credentials_are_never_allowed(self, client):
        """/oauth/start sets a Flask session cookie; it must not be rideable."""
        r = _ui(client, "GET", "/queue/dead")
        assert "Access-Control-Allow-Credentials" not in r.headers

    def test_a_wildcard_origin_is_refused_at_startup(self):
        app = Flask(__name__)
        with pytest.raises(ValueError, match="never '\\*'"):
            init_auth(app, API_TOKEN, TRUSTED, 8080, UI_TOKEN, "*")


class TestTheUiTokenIsScoped:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/queue/health"),
            ("GET", "/queue/dead"),
            ("POST", "/queue/dead/resubmit"),
            ("POST", "/queue/failed/5/retry"),
            ("GET", "/triage/questions"),
            ("POST", "/triage/questions/q-3f8a/answer"),
            ("GET", "/config/versions"),
            ("GET", "/config/diff/5/6"),
            ("GET", "/mappings"),
            ("POST", "/mappings"),
            ("GET", "/mappings/history/a@b.com"),
            ("GET", "/emails/19ae00"),
        ],
    )
    def test_allowed_routes_pass(self, client, method, path):
        assert _ui(client, method, path).status_code == 200

    @pytest.mark.parametrize(
        "method,path,why",
        [
            ("PUT", "/config", "one request rewrites every triage rule"),
            ("GET", "/oauth/status", "returns a server filesystem path"),
            ("POST", "/sync/backfill", "spends Gmail API quota"),
            ("DELETE", "/mappings/851", "removes a precedent"),
            ("PUT", "/mappings/851", "edits a precedent"),
            ("POST", "/queue/failed/retry-all", "bulk, unscoped by id"),
            ("DELETE", "/queue/failed/5", "destructive"),
        ],
    )
    def test_everything_else_is_403_not_401(self, client, method, path, why):
        """403, because the credential is valid and the ROUTE is not allowed.

        A 401 here would send the user to re-enter a token that was fine.
        """
        r = _ui(client, method, path)
        assert r.status_code == 403, why
        assert r.get_json()["error"] == "token not permitted for this route"

    def test_rerun_is_denied_without_preview(self, client):
        """Without preview it enqueues thousands of rows at a chosen priority."""
        assert _ui(client, "POST", "/triage/rerun").status_code == 403

    def test_rerun_is_allowed_with_preview(self, client):
        assert _ui(client, "POST", "/triage/rerun?preview=1").status_code == 200

    @pytest.mark.parametrize("raw", ["0", "false", "no", "", "maybe"])
    def test_a_falsey_preview_does_not_unlock_rerun(self, client, raw):
        assert _ui(client, "POST", f"/triage/rerun?preview={raw}").status_code == 403

    def test_the_full_token_still_reaches_everything(self, client):
        """The scoped token must not have narrowed the real one."""
        r = client.put("/config", headers={"Authorization": f"Bearer {API_TOKEN}"})
        assert r.status_code == 200

    def test_a_wrong_token_is_401(self, client):
        r = client.get("/queue/dead", headers={"Authorization": "Bearer nope", "Origin": ORIGIN})
        assert r.status_code == 401

    def test_no_ui_token_configured_means_the_ui_cannot_authenticate(self):
        app = Flask(__name__)
        app.add_url_rule("/queue/dead", view_func=lambda: jsonify({"ok": True}))
        init_auth(app, API_TOKEN, TRUSTED, 8080, ui_token="", ui_origins=ORIGIN)
        r = app.test_client().get("/queue/dead", headers={"Authorization": f"Bearer {UI_TOKEN}"})
        assert r.status_code == 401


class TestTheAllowlistItself:
    def test_an_unlisted_route_is_closed_by_default(self):
        """A route added later must be denied until someone lists it."""
        assert not _ui_route_allowed("GET", "/some/future/route", {})

    def test_pattern_routes_do_not_over_match(self):
        assert _ui_route_allowed("POST", "/queue/failed/5/retry", {})
        assert not _ui_route_allowed("POST", "/queue/failed/5/retry/extra", {})
        assert not _ui_route_allowed("DELETE", "/queue/failed/5", {})

    def test_method_is_part_of_the_match(self):
        assert _ui_route_allowed("GET", "/mappings", {})
        assert not _ui_route_allowed("DELETE", "/mappings", {})

    @pytest.mark.parametrize(
        "raw,expected",
        [("1", True), ("true", True), ("YES", True), ("0", False), (None, False)],
    )
    def test_preview_parsing(self, raw, expected):
        assert _is_preview({"preview": raw} if raw is not None else {}) is expected
