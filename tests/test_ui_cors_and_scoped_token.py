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

from typing import Any

import pytest
from flask import Flask, jsonify

from gateway.auth import UI_ALLOWED_EXACT, UI_ALLOWED_PATTERNS, _ui_route_allowed, init_auth

API_TOKEN = "api-token-fixture"
UI_TOKEN = "ui-token-fixture"
ORIGIN = "chrome-extension://jkgpflheabfjgmikaagejgnmopiknooj"
OTHER_ORIGIN = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TRUSTED = "172.26.0.0/16"

# Routes the app under test serves. The gate runs before dispatch, so the
# handlers only need to exist for the allowed cases to come back 200.
# One concrete path per UI_ALLOWED_PATTERNS entry, with a path that the
# pattern must NOT match. test_every_pattern_has_a_sample asserts this list
# stays as long as the pattern tuple, so adding a pattern without a sample
# fails rather than going silently unasserted.
PATTERN_SAMPLES = (
    ("POST", "/queue/failed/5/retry", "/queue/failed/5/retry/extra"),
    ("POST", "/triage/questions/q-3f8a/answer", "/triage/questions/a/b/answer"),
    ("GET", "/config/diff/5/6", "/config/diff/5/6/7"),
    ("GET", "/mappings/history/a@b.com", "/mappings/history/"),
    ("GET", "/emails/by-label/Uncategorized", "/emails/by-label/"),
    ("GET", "/emails/19ae00", "/emails/19ae00/body"),
    ("GET", "/emails/19ae00/text", "/emails/19ae00/text/extra"),
)

# Routes the app under test serves. The gate runs before dispatch, so the
# handlers only need to exist for the allowed cases to come back 200.
DENIED_ROUTES = (
    ("/queue/failed/5", ("DELETE",)),
    ("/queue/failed/retry-all", ("POST",)),
    # Allowed only once a read-only preview mode exists; until then the UI
    # token must not reach it at all.
    ("/triage/rerun", ("POST",)),
    ("/config", ("GET", "PUT")),
    ("/mappings/851", ("PUT", "DELETE")),
    ("/oauth/status", ("GET",)),
    ("/sync/backfill", ("POST",)),
)

ROUTES = (
    *((path, (method,)) for method, path in sorted(UI_ALLOWED_EXACT)),
    *((path, (method,)) for method, path, _ in PATTERN_SAMPLES),
    *DENIED_ROUTES,
)


@pytest.fixture
def client() -> Any:
    """A real Flask app with the real gate installed.

    init_auth IS the thing under test, so it is wired for real rather than
    stubbed; only the route handlers are fixtures.
    """
    app = Flask(__name__)
    for i, (path, methods) in enumerate(ROUTES):
        app.add_url_rule(
            path,
            endpoint=f"r{i}-{path}-{'-'.join(methods)}",
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


def _ui(client: Any, method: str, path: str, **kw: Any) -> Any:
    return client.open(
        path,
        method=method,
        headers={"Authorization": f"Bearer {UI_TOKEN}", "Origin": ORIGIN},
        **kw,
    )


class TestPreflight:
    def test_preflight_succeeds_with_no_credential(self, client) -> None:
        """The one that breaks everything if missed: preflight cannot carry a token."""
        r = client.open("/queue/dead", method="OPTIONS", headers={"Origin": ORIGIN})
        assert r.status_code == 204
        assert r.headers["Access-Control-Allow-Origin"] == ORIGIN
        assert r.data == b""

    def test_preflight_advertises_the_methods_and_headers_the_ui_sends(self, client) -> None:
        """Values, not just presence. Each was mutable with the suite green.

        A missing POST or Authorization here fails every write the extension
        makes, and the browser reports it as CORS rather than as a method or
        header it was not allowed to send.
        """
        r = client.open("/queue/dead", method="OPTIONS", headers={"Origin": ORIGIN})
        methods = r.headers["Access-Control-Allow-Methods"]
        for method in ("GET", "POST", "DELETE", "OPTIONS"):
            assert method in methods
        headers = r.headers["Access-Control-Allow-Headers"]
        for header in ("Authorization", "Content-Type", "X-Created-By", "X-Updated-By"):
            assert header in headers
        assert r.headers["Access-Control-Max-Age"] == "600"

    def test_preflight_for_an_unknown_path_looks_identical(self, client) -> None:
        """So preflight cannot be used to enumerate the route table."""
        known = client.open("/queue/dead", method="OPTIONS", headers={"Origin": ORIGIN})
        unknown = client.open("/does/not/exist", method="OPTIONS", headers={"Origin": ORIGIN})
        assert unknown.status_code == known.status_code == 204
        assert unknown.data == known.data == b""

    def test_preflight_from_another_origin_is_not_served(self, client) -> None:
        r = client.open("/queue/dead", method="OPTIONS", headers={"Origin": OTHER_ORIGIN})
        assert r.status_code != 204
        assert "Access-Control-Allow-Origin" not in r.headers


class TestCorsHeaders:
    def test_allow_origin_echoes_the_exact_origin_never_a_wildcard(self, client) -> None:
        r = _ui(client, "GET", "/queue/dead")
        assert r.headers["Access-Control-Allow-Origin"] == ORIGIN
        assert r.headers["Access-Control-Allow-Origin"] != "*"

    def test_another_origin_gets_no_cors_header(self, client) -> None:
        r = client.get(
            "/queue/dead",
            headers={"Authorization": f"Bearer {UI_TOKEN}", "Origin": OTHER_ORIGIN},
        )
        assert "Access-Control-Allow-Origin" not in r.headers

    @pytest.mark.parametrize("origin", [None, ORIGIN, OTHER_ORIGIN])
    def test_vary_origin_is_present_whatever_the_origin(self, client, origin) -> None:
        """A cache must never reuse an allowlisted response for another origin.

        Testing only the no-origin case left the allowlisted and preflight
        responses -- the ones a cache could actually mis-serve -- unchecked.
        """
        headers = {"Origin": origin} if origin else {}
        assert "Origin" in client.get("/health", headers=headers).headers.get("Vary", "")

    def test_vary_origin_is_present_on_a_preflight(self, client) -> None:
        r = client.open("/queue/dead", method="OPTIONS", headers={"Origin": ORIGIN})
        assert "Origin" in r.headers.get("Vary", "")

    def test_credentials_are_never_allowed(self, client) -> None:
        """/oauth/start sets a Flask session cookie; it must not be rideable."""
        r = _ui(client, "GET", "/queue/dead")
        assert "Access-Control-Allow-Credentials" not in r.headers

    def test_a_wildcard_origin_is_refused_at_startup(self) -> None:
        app = Flask(__name__)
        with pytest.raises(ValueError, match="never '\\*'"):
            init_auth(app, API_TOKEN, TRUSTED, 8080, ui_token=UI_TOKEN, ui_origins="*")


class TestTheUiTokenIsScoped:
    @pytest.mark.parametrize("method,path", sorted(UI_ALLOWED_EXACT))
    def test_every_exact_entry_is_reachable(self, client, method, path) -> None:
        """Parametrized over the allowlist itself.

        A hand-copied subset is what let six entries be deleted with the suite
        green; deriving the cases means a new entry is asserted the moment it
        is added, and an entry removed from the allowlist fails here.
        """
        assert _ui(client, method, path).status_code == 200

    @pytest.mark.parametrize("method,path,_over", PATTERN_SAMPLES)
    def test_every_pattern_entry_is_reachable(self, client, method, path, _over) -> None:
        assert _ui(client, method, path).status_code == 200

    @pytest.mark.parametrize("method,_path,over", PATTERN_SAMPLES)
    def test_no_pattern_over_matches(self, client, method, _path, over) -> None:
        """403 is the gate denying; 404 would mean the pattern matched.

        The over-match paths are deliberately unregistered, so reaching
        routing at all proves the regex was too loose.
        """
        assert _ui(client, method, over).status_code == 403

    def test_every_pattern_has_a_sample(self) -> None:
        """So a pattern added without a sample fails instead of going unasserted."""
        assert len(PATTERN_SAMPLES) == len(UI_ALLOWED_PATTERNS)

    def test_the_allowlist_is_the_size_it_is_meant_to_be(self) -> None:
        """Pins the COUNT, because the cases above are derived from the list.

        Deriving them means an entry deleted from the allowlist deletes its own
        test case: measured, removing ("GET", "/queue/stats") took the file from
        65 passed to 64 passed with nothing red. Only a count notices. Changing
        either number is the deliberate act of widening or narrowing what the
        browser may reach, so it should take an edit here.
        """
        assert len(UI_ALLOWED_EXACT) == 15
        assert len(UI_ALLOWED_PATTERNS) == 7

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
    def test_everything_else_is_403_not_401(self, client, method, path, why) -> None:
        """403, because the credential is valid and the ROUTE is not allowed.

        A 401 here would send the user to re-enter a token that was fine.
        """
        r = _ui(client, method, path)
        assert r.status_code == 403, why
        assert r.get_json()["error"] == "token not permitted for this route"

    @pytest.mark.parametrize("query", ["", "?preview=1", "?preview=true", "?preview=0"])
    def test_rerun_is_denied_whatever_the_query_string(self, client, query) -> None:
        """No query string buys access to a bulk enqueue.

        An earlier revision allowed ?preview=1 on the strength of a preview
        mode the route did not have, so the UI token could enqueue thousands
        of rows at a chosen priority -- the exact action the allowlist comment
        said it withheld. The permission comes back with the preview
        implementation, not before.
        """
        assert _ui(client, "POST", f"/triage/rerun{query}").status_code == 403

    def test_the_full_token_still_reaches_everything(self, client) -> None:
        """The scoped token must not have narrowed the real one."""
        r = client.put("/config", headers={"Authorization": f"Bearer {API_TOKEN}"})
        assert r.status_code == 200

    def test_a_wrong_token_is_401(self, client) -> None:
        r = client.get("/queue/dead", headers={"Authorization": "Bearer nope", "Origin": ORIGIN})
        assert r.status_code == 401

    def test_no_ui_token_configured_means_the_ui_cannot_authenticate(self) -> None:
        app = Flask(__name__)
        app.add_url_rule("/queue/dead", view_func=lambda: jsonify({"ok": True}))
        init_auth(app, API_TOKEN, TRUSTED, 8080, ui_token="", ui_origins=ORIGIN)
        r = app.test_client().get("/queue/dead", headers={"Authorization": f"Bearer {UI_TOKEN}"})
        assert r.status_code == 401


class TestTokenAndOriginParsing:
    """Stripping and splitting, each of which survived mutation before."""

    def test_a_ui_token_with_a_trailing_newline_still_authenticates(self) -> None:
        """`CORTEX_UI_TOKEN=$(cat secret)` is the usual way, newline included."""
        app = Flask(__name__)
        app.add_url_rule("/queue/dead", view_func=lambda: jsonify({"ok": True}))
        init_auth(app, API_TOKEN, TRUSTED, 8080, ui_token=f"{UI_TOKEN}\n", ui_origins=ORIGIN)
        r = app.test_client().get(
            "/queue/dead",
            headers={"Authorization": f"Bearer {UI_TOKEN}", "Origin": ORIGIN},
        )
        assert r.status_code == 200

    def test_a_whitespace_only_ui_token_refuses_to_start(self) -> None:
        """Mirrors the api-token guard: stripping must not mean "unconfigured".

        An empty secret file gives "\n". Without this, the extension 401s
        forever and the startup log says ui_token_set=False, which is true but
        points at the wrong problem.
        """
        app = Flask(__name__)
        with pytest.raises(ValueError, match="only whitespace"):
            init_auth(app, API_TOKEN, TRUSTED, 8080, ui_token="\n ", ui_origins=ORIGIN)

    def test_origins_are_stripped_and_empty_chunks_dropped(self) -> None:
        app = Flask(__name__)
        app.add_url_rule("/queue/dead", view_func=lambda: jsonify({"ok": True}))
        init_auth(
            app,
            API_TOKEN,
            TRUSTED,
            8080,
            ui_token=UI_TOKEN,
            ui_origins=f" {ORIGIN} , , {OTHER_ORIGIN}",
        )
        client = app.test_client()
        for origin in (ORIGIN, OTHER_ORIGIN):
            r = client.get(
                "/queue/dead",
                headers={"Authorization": f"Bearer {UI_TOKEN}", "Origin": origin},
            )
            assert r.headers["Access-Control-Allow-Origin"] == origin

    def test_a_wildcard_among_other_origins_is_still_refused(self) -> None:
        """The guard is `"*" in origins`, not `origins == {"*"}`.

        Mutating it to the equality form passed the whole suite, because the
        only wildcard case tested was a bare "*".
        """
        app = Flask(__name__)
        with pytest.raises(ValueError, match="never '\\*'"):
            init_auth(app, API_TOKEN, TRUSTED, 8080, ui_token=UI_TOKEN, ui_origins=f"{ORIGIN}, *")


class TestCorsOnRejections:
    """The 401/403 distinction is only usable if the browser can read it.

    Dropping Allow-Origin on status >= 400 passed the whole suite, which would
    hide from the extension exactly the difference the gate goes out of its
    way to express.
    """

    def test_a_403_carries_the_cors_header(self, client) -> None:
        r = _ui(client, "PUT", "/config")
        assert r.status_code == 403
        assert r.headers["Access-Control-Allow-Origin"] == ORIGIN

    def test_a_401_carries_the_cors_header(self, client) -> None:
        r = client.get("/queue/dead", headers={"Authorization": "Bearer nope", "Origin": ORIGIN})
        assert r.status_code == 401
        assert r.headers["Access-Control-Allow-Origin"] == ORIGIN


class TestTheAllowlistItself:
    def test_an_unlisted_route_is_closed_by_default(self) -> None:
        """A route added later must be denied until someone lists it."""
        assert not _ui_route_allowed("GET", "/some/future/route")

    def test_pattern_routes_do_not_over_match(self) -> None:
        assert _ui_route_allowed("POST", "/queue/failed/5/retry")
        assert not _ui_route_allowed("POST", "/queue/failed/5/retry/extra")
        assert not _ui_route_allowed("DELETE", "/queue/failed/5")

    def test_method_is_part_of_the_match(self) -> None:
        assert _ui_route_allowed("GET", "/mappings")
        assert not _ui_route_allowed("DELETE", "/mappings")
