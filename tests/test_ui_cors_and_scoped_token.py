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
from flask import Flask, Response, jsonify
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

from gateway.auth import UI_ALLOWED_EXACT, UI_ALLOWED_PATTERNS, _ui_route_allowed, init_auth

API_TOKEN = "api-token-fixture"
UI_TOKEN = "ui-token-fixture"
ORIGIN = "chrome-extension://jkgpflheabfjgmikaagejgnmopiknooj"
OTHER_ORIGIN = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TRUSTED = "172.26.0.0/16"

# Per UI_ALLOWED_PATTERNS entry: the method, a path it must match, the paths it
# must NOT match, and a method it must not accept on that same path.
# test_every_pattern_has_a_sample keeps this as long as the pattern tuple.
#
# The non-matching paths cover both ways a regex goes loose -- an extra
# segment, and a character class widened -- and the wrong method is what
# catches a match that ignores the method entirely.
PATTERN_SAMPLES = (
    (
        "POST",
        "/queue/failed/5/retry",
        ("/queue/failed/5/retry/extra", "/queue/failed/abc/retry"),
        "GET",
    ),
    (
        "POST",
        "/triage/questions/q-3f8a/answer",
        ("/triage/questions/a/b/answer", "/triage/questions/q.3f8a/answer"),
        "GET",
    ),
    ("GET", "/config/diff/5/6", ("/config/diff/5/6/7", "/config/diff/a/b"), "POST"),
    ("GET", "/mappings/history/a@b.com", ("/mappings/history/",), "DELETE"),
    ("GET", "/emails/by-label/Uncategorized", ("/emails/by-label/",), "POST"),
    ("GET", "/emails/19ae00", ("/emails/19ae00/body",), "DELETE"),
    (
        "GET",
        "/emails/19ae00/text",
        ("/emails/19ae00/text/extra", "/emails/a/b/text"),
        "POST",
    ),
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
    *((path, (method,)) for method, path, _over, _wrong in PATTERN_SAMPLES),
    *DENIED_ROUTES,
)


@pytest.fixture
def client() -> FlaskClient:
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


def _ui(client: FlaskClient, method: str, path: str, **kw: Any) -> TestResponse:
    return client.open(
        path,
        method=method,
        headers={"Authorization": f"Bearer {UI_TOKEN}", "Origin": ORIGIN},
        **kw,
    )


class TestPreflight:
    def test_preflight_succeeds_with_no_credential(self, client: FlaskClient) -> None:
        """The one that breaks everything if missed: preflight cannot carry a token."""
        r = client.open("/queue/dead", method="OPTIONS", headers={"Origin": ORIGIN})
        assert r.status_code == 204
        assert r.headers["Access-Control-Allow-Origin"] == ORIGIN
        assert r.data == b""

    def test_preflight_advertises_the_methods_and_headers_the_ui_sends(
        self, client: FlaskClient
    ) -> None:
        """Values, not just presence. Each was mutable with the suite green.

        A missing POST or Authorization here fails every write the extension
        makes, and the browser reports it as CORS rather than as a method or
        header it was not allowed to send.
        """
        r = client.open("/queue/dead", method="OPTIONS", headers={"Origin": ORIGIN})
        parse = lambda h: {v.strip() for v in r.headers[h].split(",")}  # noqa: E731
        # Exact sets, not membership: checking only that GET is present let a
        # widened list through, and PUT added to Allow-Methods survived.
        assert parse("Access-Control-Allow-Methods") == {
            "GET",
            "POST",
            "DELETE",
            "OPTIONS",
        }
        assert parse("Access-Control-Allow-Headers") == {
            "Authorization",
            "Content-Type",
            "X-Created-By",
            "X-Updated-By",
        }
        assert r.headers["Access-Control-Max-Age"] == "600"

    def test_preflight_for_an_unknown_path_looks_identical(self, client: FlaskClient) -> None:
        """So preflight cannot be used to enumerate the route table."""
        known = client.open("/queue/dead", method="OPTIONS", headers={"Origin": ORIGIN})
        unknown = client.open("/does/not/exist", method="OPTIONS", headers={"Origin": ORIGIN})
        assert unknown.status_code == known.status_code == 204
        assert unknown.data == known.data == b""

    def test_preflight_from_another_origin_is_not_served(self, client: FlaskClient) -> None:
        r = client.open("/queue/dead", method="OPTIONS", headers={"Origin": OTHER_ORIGIN})
        assert r.status_code != 204
        assert "Access-Control-Allow-Origin" not in r.headers


class TestCorsHeaders:
    def test_allow_origin_echoes_the_exact_origin_never_a_wildcard(
        self, client: FlaskClient
    ) -> None:
        r = _ui(client, "GET", "/queue/dead")
        assert r.headers["Access-Control-Allow-Origin"] == ORIGIN
        assert r.headers["Access-Control-Allow-Origin"] != "*"

    def test_another_origin_gets_no_cors_header(self, client: FlaskClient) -> None:
        r = client.get(
            "/queue/dead",
            headers={"Authorization": f"Bearer {UI_TOKEN}", "Origin": OTHER_ORIGIN},
        )
        assert "Access-Control-Allow-Origin" not in r.headers

    @pytest.mark.parametrize("origin", [None, ORIGIN, OTHER_ORIGIN])
    def test_vary_origin_is_present_whatever_the_origin(
        self, client: FlaskClient, origin: str | None
    ) -> None:
        """A cache must never reuse an allowlisted response for another origin.

        Testing only the no-origin case left the allowlisted and preflight
        responses -- the ones a cache could actually mis-serve -- unchecked.
        """
        headers = {"Origin": origin} if origin else {}
        assert "Origin" in client.get("/health", headers=headers).headers.get("Vary", "")

    def test_vary_origin_is_present_on_a_preflight(self, client: FlaskClient) -> None:
        r = client.open("/queue/dead", method="OPTIONS", headers={"Origin": ORIGIN})
        assert "Origin" in r.headers.get("Vary", "")

    def test_vary_is_added_to_an_existing_one_not_written_over(self) -> None:
        """headers.add, not assignment. Checking "Origin" in Vary misses this.

        A handler that varies on something else of its own -- content
        negotiation, compression -- loses that value if the CORS layer
        assigns rather than appends, and the cache then serves one encoding
        for all of them.
        """
        app = Flask(__name__)

        @app.route("/varies")
        def varies() -> Response:
            resp = jsonify({"ok": True})
            resp.headers["Vary"] = "Accept-Encoding"
            return resp

        init_auth(app, API_TOKEN, TRUSTED, 8080, ui_token=UI_TOKEN, ui_origins=ORIGIN)
        # The api token, not the UI one: /varies is not on the allowlist, so a
        # UI-token request is answered by the gate and never reaches the handler
        # that sets the other Vary value.
        r = app.test_client().get(
            "/varies", headers={"Authorization": f"Bearer {API_TOKEN}", "Origin": ORIGIN}
        )
        values = {v.strip() for part in r.headers.getlist("Vary") for v in part.split(",")}
        assert values == {"Accept-Encoding", "Origin"}

    def test_credentials_are_never_allowed(self, client: FlaskClient) -> None:
        """/oauth/start sets a Flask session cookie; it must not be rideable."""
        r = _ui(client, "GET", "/queue/dead")
        assert "Access-Control-Allow-Credentials" not in r.headers

    def test_a_wildcard_origin_is_refused_at_startup(self) -> None:
        app = Flask(__name__)
        with pytest.raises(ValueError, match="never '\\*'"):
            init_auth(app, API_TOKEN, TRUSTED, 8080, ui_token=UI_TOKEN, ui_origins="*")


class TestTheUiTokenIsScoped:
    @pytest.mark.parametrize("method,path", sorted(UI_ALLOWED_EXACT))
    def test_every_exact_entry_is_reachable(
        self, client: FlaskClient, method: str, path: str
    ) -> None:
        """Parametrized over the allowlist itself.

        A hand-copied subset is what let six entries be deleted with the suite
        green; deriving the cases means a new entry is asserted the moment it
        is added, and an entry removed from the allowlist fails here.
        """
        assert _ui(client, method, path).status_code == 200

    @pytest.mark.parametrize("method,path,_over,_wrong", PATTERN_SAMPLES)
    def test_every_pattern_entry_is_reachable(
        self,
        client: FlaskClient,
        method: str,
        path: str,
        _over: tuple[str, ...],
        _wrong: str,
    ) -> None:
        assert _ui(client, method, path).status_code == 200

    @pytest.mark.parametrize(
        "method,over", [(m, o) for m, _p, overs, _w in PATTERN_SAMPLES for o in overs]
    )
    def test_no_pattern_over_matches(self, client: FlaskClient, method: str, over: str) -> None:
        """403 is the gate denying; anything else means the pattern matched.

        The over-match paths are deliberately unregistered, so reaching routing
        at all proves the regex was too loose.
        """
        assert _ui(client, method, over).status_code == 403

    @pytest.mark.parametrize("wrong_method,path", [(w, p) for _m, p, _o, w in PATTERN_SAMPLES])
    def test_a_pattern_does_not_accept_another_method(
        self, client: FlaskClient, wrong_method: str, path: str
    ) -> None:
        """The method is half of each pattern entry and was unasserted.

        Dropping `m == method` from the any() in _ui_route_allowed left the
        whole file green, so every GET-only pattern silently took POST too.
        """
        assert _ui(client, wrong_method, path).status_code == 403

    @pytest.mark.parametrize("path", ["/mappings/", "/MAPPINGS", "/Mappings"])
    def test_the_gate_matches_the_path_exactly(self, client: FlaskClient, path: str) -> None:
        """No trailing-slash or case folding: ("GET", "/mappings") is allowed
        and these are not.

        Either folding turns a near-miss into a hit, which is the ordinary way
        an allowlist gets bypassed. A .lower() on the matched path passed the
        whole file before this test existed.
        """
        assert _ui(client, "GET", path).status_code == 403

    def test_a_withheld_method_is_advertised_so_its_403_is_reachable(
        self, client: FlaskClient
    ) -> None:
        """The preflight must advertise MORE than the allowlist permits.

        No allowed route accepts DELETE, so it would be tempting to drop it
        from Allow-Methods. That would make the browser block the request and
        hand the extension an opaque failed fetch, replacing the 403 whose
        whole purpose is telling "not permitted here" from "wrong credential".
        The gate, not the preflight, is the boundary.
        """
        advertised = {
            v.strip()
            for v in client.open("/queue/dead", method="OPTIONS", headers={"Origin": ORIGIN})
            .headers["Access-Control-Allow-Methods"]
            .split(",")
        }
        denied = _ui(client, "DELETE", "/queue/failed/5")
        assert denied.status_code == 403
        assert denied.get_json()["error"] == "token not permitted for this route"
        assert "DELETE" in advertised, "or the browser masks that 403"

    def test_every_pattern_has_a_sample(self) -> None:
        """So a pattern added without a sample fails instead of going unasserted."""
        assert len(PATTERN_SAMPLES) == len(UI_ALLOWED_PATTERNS)

    def test_the_allowlist_is_exactly_this(self) -> None:
        """A snapshot of the whole boundary, pinned by CONTENT.

        The cases above are derived from the allowlist, so the allowlist cannot
        check itself: an entry deleted from it deletes its own test case, and an
        entry changed IN PLACE just moves the case to the changed value. A count
        catches the deletion and not the change -- which is how changing
        ("POST", "/mappings") to PUT, a method the comment on UI_ALLOWED_EXACT
        explicitly withholds, passed a suite pinned only by length.

        So the snapshot is the assertion: widening, narrowing or re-pointing
        what the browser may reach has to be an edit here, in a diff a reviewer
        reads.

        No pass counts in here on purpose. Two earlier versions of this
        docstring quoted measurements that were correct when taken and wrong
        two commits later, because every test added to this file moves them.
        """
        assert UI_ALLOWED_EXACT == frozenset(
            {
                ("GET", "/health"),
                ("GET", "/queue/health"),
                ("GET", "/queue/stats"),
                ("GET", "/queue/failed"),
                ("GET", "/queue/dead"),
                ("GET", "/queue/stuck"),
                ("POST", "/queue/dead/resubmit"),
                ("POST", "/queue/dead/dismiss"),
                ("GET", "/triage/questions"),
                ("GET", "/triage/stats"),
                ("GET", "/triage/classifications"),
                ("GET", "/triage/convergence/status"),
                ("GET", "/config/versions"),
                ("GET", "/mappings"),
                ("POST", "/mappings"),
            }
        )

    def test_the_allowlist_patterns_are_exactly_these(self) -> None:
        """Snapshot of the regex half, for the same reason, source text and all.

        Pinning the pattern SOURCE is what catches a regex loosened in place.
        The over-match cases in PATTERN_SAMPLES catch it too, now that they
        include a non-digit and an extra-segment path per pattern; before those
        existed, widening a character class left every derived case passing,
        because the matching sample still matched.
        """
        assert tuple((m, pat.pattern) for m, pat in UI_ALLOWED_PATTERNS) == (
            ("POST", r"^/queue/failed/\d+/retry$"),
            ("POST", r"^/triage/questions/[A-Za-z0-9_-]+/answer$"),
            ("GET", r"^/config/diff/\d+/\d+$"),
            ("GET", r"^/mappings/history/.+$"),
            ("GET", r"^/emails/by-label/.+$"),
            ("GET", r"^/emails/[^/]+$"),
            ("GET", r"^/emails/[^/]+/text$"),
        )

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
    def test_everything_else_is_403_not_401(
        self, client: FlaskClient, method: str, path: str, why: str
    ) -> None:
        """403, because the credential is valid and the ROUTE is not allowed.

        A 401 here would send the user to re-enter a token that was fine.
        """
        r = _ui(client, method, path)
        assert r.status_code == 403, why
        assert r.get_json()["error"] == "token not permitted for this route"

    @pytest.mark.parametrize("query", ["", "?preview=1", "?preview=true", "?preview=0"])
    def test_rerun_is_denied_whatever_the_query_string(
        self, client: FlaskClient, query: str
    ) -> None:
        """No query string buys access to a bulk enqueue.

        An earlier revision allowed ?preview=1 on the strength of a preview
        mode the route did not have, so the UI token could enqueue thousands
        of rows at a chosen priority -- the exact action the allowlist comment
        said it withheld. The permission comes back with the preview
        implementation, not before.
        """
        assert _ui(client, "POST", f"/triage/rerun{query}").status_code == 403

    def test_the_full_token_still_reaches_everything(self, client: FlaskClient) -> None:
        """The scoped token must not have narrowed the real one."""
        r = client.put("/config", headers={"Authorization": f"Bearer {API_TOKEN}"})
        assert r.status_code == 200

    def test_a_wrong_token_is_401(self, client: FlaskClient) -> None:
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

    def test_several_origins_are_parsed_and_only_those_are_allowed(self) -> None:
        """Stripping, multiple origins, and nothing else getting in.

        The previous version of this asserted that empty chunks are "dropped",
        which no request can show: a dropped chunk and a chunk that was never
        there look the same from outside. What is checkable is that both real
        origins work and a third does not.
        """
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
        third = client.get(
            "/queue/dead",
            headers={
                "Authorization": f"Bearer {UI_TOKEN}",
                "Origin": "chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            },
        )
        assert "Access-Control-Allow-Origin" not in third.headers

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

    def test_a_403_carries_the_cors_header(self, client: FlaskClient) -> None:
        r = _ui(client, "PUT", "/config")
        assert r.status_code == 403
        assert r.headers["Access-Control-Allow-Origin"] == ORIGIN

    def test_a_401_carries_the_cors_header(self, client: FlaskClient) -> None:
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
