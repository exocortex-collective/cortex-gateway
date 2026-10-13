"""The trust boundary is "unrelayed request from a peer subnet", not "a private IP".

The gateway sits on three Docker networks. Traefik is one of them, so a request
relayed from the internet arrives with a private 172.24/16 source address that
looks exactly like a peer service. A naive "is it RFC1918?" check therefore
hands the whole API to anyone who can reach the Traefik route -- failing open
in precisely the case the token exists for.

These pin both halves of the rule: the subnet must be one where peers actually
live, AND no proxy header may be present.
"""

from __future__ import annotations

import ipaddress
from unittest.mock import patch

import pytest

from gateway.auth import _parse_subnets, _presented_token, is_internal

# A fixture topology, not this deployment's. The production values live in
# CORTEX_TRUSTED_SUBNETS, which is now required rather than defaulted.
_TRUSTED_FIXTURE = "172.26.0.0/16,172.29.0.0/16,127.0.0.1/32"


@pytest.fixture
def reloaded_gateway():
    """Reload gateway.config + gateway.app under the test's env, then undo it.

    Config's fields are dataclass CLASS-BODY defaults, read from os.environ at
    import time. So `importlib.reload(gateway.config)` with CORTEX_INTERNAL_PORT
    set bakes that value into the class for the REST OF THE SESSION -- monkeypatch
    restores the environment variable, but not the class attribute built from it.

    Round 5 proved the damage: the port test injected 8080, and
    test_config_internal_port_matches_a_port_the_dockerfile_binds, sixty lines
    later, then read the injected value instead of config.py's real default.
    Mutating that default to a port nothing binds -- exactly the drift the test
    exists to catch, which would 401 the entire pipeline -- left the whole suite
    GREEN. It failed only in isolation, and CI runs the whole suite.

    So reloading is a fixture with a teardown, never a bare call in a test.
    """
    import importlib
    import os
    import sys

    import gateway.app
    import gateway.config

    saved_env = dict(os.environ)

    # Every module that did `from gateway.config import config` holds its own
    # reference to the OLD instance, so reloading gateway.config alone leaves
    # them bound to a stale one. Latent today -- the stale instance holds
    # pristine values -- but it is the same vacuity class as the bug this
    # fixture exists to fix, one module over. Reload the bindings too.
    _config_importers = (
        "gateway.services.postgres",
        "gateway.services.duckdb",
        "gateway.blueprints.oauth",
        # ...and the package that re-exports the blueprint objects. Reloading
        # blueprints.oauth alone mints a NEW Blueprint while
        # gateway/blueprints/__init__.py keeps re-exporting the old one, so
        # create_app() registers the stale object and the reload of that entry
        # achieves nothing. Same shape as the bug this fixture exists to fix,
        # one level up the package.
        "gateway.blueprints",
    )

    def _reload():
        importlib.reload(gateway.config)
        for name in _config_importers:
            module = sys.modules.get(name)
            if module is not None:
                importlib.reload(module)
        importlib.reload(gateway.app)
        return gateway.app

    try:
        yield _reload
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        _reload()


TRUSTED = _parse_subnets(_TRUSTED_FIXTURE)


class Headers(dict):
    """Minimal stand-in for werkzeug's case-insensitive headers."""

    def get(self, key, default=None):  # type: ignore[override]
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def test_peer_service_is_internal() -> None:
    assert is_internal("172.26.0.13", Headers(), TRUSTED)


def test_metrics_network_is_internal() -> None:
    assert is_internal("172.29.0.11", Headers(), TRUSTED)


def test_lan_client_is_external() -> None:
    """A private address outside the peer subnets is still external.

    Verified on the deployment that a LAN request keeps its own source
    address rather than being masqueraded to the bridge gateway, so this is
    the shape real LAN traffic arrives in. The address here is a stand-in --
    the repo is public, so it must not name a real host.
    """
    assert not is_internal("10.0.0.99", Headers(), TRUSTED)


def test_traefik_subnet_is_not_trusted() -> None:
    """The whole point. 172.24/16 is a Docker subnet but it is the front door."""
    assert not is_internal("172.24.0.5", Headers(), TRUSTED)


@pytest.mark.parametrize("header", ["X-Forwarded-For", "X-Real-IP", "Forwarded"])
def test_proxy_header_removes_trust_even_from_a_peer_subnet(header: str) -> None:
    """A relayed request is external no matter where it was relayed from.

    Spoofing this header can only ever LOSE access, never gain it, so an
    attacker has no incentive and a proxy has no way to be mistaken for a peer.
    """
    assert is_internal("172.26.0.13", Headers(), TRUSTED)
    assert not is_internal("172.26.0.13", Headers({header: "203.0.113.7"}), TRUSTED)


def test_unknown_or_missing_source_is_external() -> None:
    assert not is_internal(None, Headers(), TRUSTED)
    assert not is_internal("", Headers(), TRUSTED)
    assert not is_internal("not-an-ip", Headers(), TRUSTED)


def test_public_address_is_external() -> None:
    assert not is_internal("203.0.113.7", Headers(), TRUSTED)


def test_trusted_subnets_exclude_traefik() -> None:
    """Guard the constant itself: adding 172.24/16 here would fail open."""
    traefik = ipaddress.ip_address("172.24.0.5")
    assert not any(traefik in net for net in TRUSTED)


def test_bearer_and_direct_header_are_both_accepted() -> None:
    assert _presented_token(Headers({"Authorization": "Bearer abc123"})) == "abc123"
    assert _presented_token(Headers({"authorization": "bearer abc123"})) == "abc123"
    assert _presented_token(Headers({"X-Cortex-Token": "abc123"})) == "abc123"


def test_malformed_authorization_yields_no_token() -> None:
    assert _presented_token(Headers({"Authorization": "abc123"})) == ""
    assert _presented_token(Headers({"Authorization": "Basic abc123"})) == ""
    assert _presented_token(Headers({"Authorization": "Bearer "})) == ""
    assert _presented_token(Headers()) == ""


# --- end to end through a real Flask app -------------------------------------
#
# The predicate being right is not the same as the gate being installed. These
# drive a real app through init_auth and assert on status codes.


def _app(token: str):
    from flask import Flask, jsonify

    from gateway.auth import init_auth

    app = Flask(__name__)
    init_auth(app, token, _TRUSTED_FIXTURE)

    @app.route("/health")
    def health():
        return jsonify(ok=True)

    @app.route("/config", methods=["GET", "PUT"])
    def cfg():
        return jsonify(ok=True)

    return app


def _get(app, path="/config", addr="10.0.0.99", port=8080, **headers):
    """Werkzeug derives SERVER_PORT from the host, so set it via base_url.

    Passing SERVER_PORT in environ_base is silently overwritten -- which is how
    the first version of these tests ended up asserting against port 80.
    """
    return app.test_client().get(
        path,
        base_url=f"http://localhost:{port}",
        environ_base={"REMOTE_ADDR": addr},
        headers=headers,
    )


def test_external_request_without_token_is_rejected() -> None:
    r = _get(_app("s3cret"))
    assert r.status_code == 401
    assert r.get_json()["error"] == "authentication required"


def test_external_request_with_token_is_allowed() -> None:
    app = _app("s3cret")
    assert _get(app, Authorization="Bearer s3cret").status_code == 200
    assert _get(app, **{"X-Cortex-Token": "s3cret"}).status_code == 200


def test_external_request_with_wrong_token_is_rejected() -> None:
    assert _get(_app("s3cret"), Authorization="Bearer wrong").status_code == 401


def test_internal_request_needs_no_token() -> None:
    assert _get(_app("s3cret"), addr="172.26.0.13").status_code == 200


def test_traefik_relayed_request_needs_a_token() -> None:
    """The fail-open case: private source IP, but relayed from outside."""
    app = _app("s3cret")
    relayed = {"X-Forwarded-For": "203.0.113.7"}
    assert _get(app, addr="172.24.0.5", **relayed).status_code == 401
    assert _get(app, addr="172.26.0.13", **relayed).status_code == 401
    with_token = _get(app, addr="172.26.0.13", Authorization="Bearer s3cret", **relayed)
    assert with_token.status_code == 200


def test_health_is_exempt_so_monitoring_needs_no_credential() -> None:
    assert _get(_app("s3cret"), path="/health").status_code == 200


def test_writes_are_gated_too() -> None:
    app = _app("s3cret")
    unauth = app.test_client().put(
        "/config", base_url="http://localhost:8080", environ_base={"REMOTE_ADDR": "10.0.0.99"}
    )
    assert unauth.status_code == 401, "PUT /config from the LAN must require a token"


def test_no_token_configured_leaves_the_gateway_open() -> None:
    """Current production behaviour, preserved deliberately until the token is deployed."""
    assert _get(_app("")).status_code == 200


# --- port-based trust --------------------------------------------------------
#
# Which socket a request arrived on is enforced by Docker port publishing, so
# it cannot drift the way a subnet list can. The address check stays as a
# second lock for the case where the internal port gets published by mistake.


def test_peer_on_the_external_port_still_needs_a_token() -> None:
    """Arriving on the published port is external, whatever the source IP."""
    assert is_internal("172.26.0.13", Headers(), TRUSTED, 8080, 8080)
    assert not is_internal("172.26.0.13", Headers(), TRUSTED, 8098, 8080)


def test_lan_on_the_internal_port_still_needs_a_token() -> None:
    """Second lock: publishing the internal port by accident must not fail open."""
    assert not is_internal("10.0.0.99", Headers(), TRUSTED, 8080, 8080)


def test_unparseable_port_is_external() -> None:
    assert not is_internal("172.26.0.13", Headers(), TRUSTED, "not-a-port", 8080)


def test_port_check_is_skipped_when_not_configured() -> None:
    """Backward compatible with a single-port deployment."""
    assert is_internal("172.26.0.13", Headers(), TRUSTED, 9999, None)


def test_gate_end_to_end_rejects_peer_on_external_port() -> None:
    from flask import Flask, jsonify

    from gateway.auth import init_auth

    app = Flask(__name__)
    init_auth(app, "s3cret", _TRUSTED_FIXTURE, internal_port=8080)

    @app.route("/config")
    def cfg():
        return jsonify(ok=True)

    peer_internal = app.test_client().get(
        "/config", base_url="http://localhost:8080", environ_base={"REMOTE_ADDR": "172.26.0.13"}
    )
    peer_external = app.test_client().get(
        "/config", base_url="http://localhost:8098", environ_base={"REMOTE_ADDR": "172.26.0.13"}
    )
    assert peer_internal.status_code == 200
    assert peer_external.status_code == 401


# --- exempt-path boundary: the P1 two reviewers found independently ---------


def test_exempt_paths_do_not_match_by_bare_prefix() -> None:
    """`/healthz-admin` must NOT inherit `/health`'s exemption.

    A bare startswith() means any future route whose name merely begins with
    an exempt one is silently unauthenticated. That is how an auth gate
    develops a hole nobody edited.
    """
    from gateway.auth import _is_exempt

    assert _is_exempt("/health")
    assert _is_exempt("/health/")
    assert _is_exempt("/health/deep")
    assert not _is_exempt("/healthz-admin")
    assert not _is_exempt("/health-admin")
    assert not _is_exempt("/healthcheck")
    assert not _is_exempt("/metricsx")


def test_only_the_oauth_callback_is_exempt() -> None:
    """/oauth/start must NOT be exempt -- that was an account-takeover hole.

    With both legs open, any caller reaching the external port could drive
    the whole grant itself: /start, consent as its own Google account, land
    on /callback, and overwrite the production token -- silently repointing
    gmail-sync at an attacker's mailbox. The state parameter is no defence
    when the attacker drives both legs.
    """
    from gateway.auth import _is_exempt

    assert _is_exempt("/oauth/callback")
    assert not _is_exempt("/oauth/start"), "gating /start is what closes the takeover"
    assert not _is_exempt("/oauth/refresh")
    assert not _is_exempt("/oauth/status")
    assert not _is_exempt("/oauth")


def test_gate_allows_the_oauth_callback_without_a_token() -> None:
    from flask import Flask, jsonify

    from gateway.auth import init_auth

    app = Flask(__name__)
    init_auth(app, "s3cret", _TRUSTED_FIXTURE, internal_port=8080)

    @app.route("/oauth/callback")
    def cb():
        return jsonify(ok=True)

    @app.route("/oauth/refresh", methods=["POST"])
    def rf():
        return jsonify(ok=True)

    @app.route("/oauth/start")
    def st():
        return jsonify(ok=True)

    ext = {"REMOTE_ADDR": "10.0.0.99"}
    assert (
        app.test_client()
        .get("/oauth/callback", base_url="http://localhost:8098", environ_base=ext)
        .status_code
        == 200
    )
    assert (
        app.test_client()
        .post("/oauth/refresh", base_url="http://localhost:8098", environ_base=ext)
        .status_code
        == 401
    )
    # The takeover leg. /oauth/start must be refused unauthenticated from
    # outside: with it exempt, any caller reaching the external port could
    # drive the whole grant and overwrite the stored token.
    assert (
        app.test_client()
        .get("/oauth/start", base_url="http://localhost:8098", environ_base=ext)
        .status_code
        == 401
    )
    # ...while an operator inside the container network can still drive it,
    # via _require_token()'s internal bypass rather than an exemption.
    assert (
        app.test_client()
        .get(
            "/oauth/start",
            base_url="http://localhost:8080",
            environ_base={"REMOTE_ADDR": "172.26.0.13"},
        )
        .status_code
        == 200
    )


# --- port lock fails closed -------------------------------------------------


def test_missing_server_port_is_not_trusted() -> None:
    """An unknown port must not become an allowed one.

    Previously a missing SERVER_PORT skipped the port check and fell back to
    address-only trust, contradicting "both locks must hold" in the worst
    direction.
    """
    assert not is_internal("172.26.0.13", Headers(), TRUSTED, None, 8080)


# --- IPv6 -------------------------------------------------------------------


def test_ipv6_sources() -> None:
    trusted6 = _parse_subnets("fd00::/8,::1/128")
    assert is_internal("fd00::1", Headers(), trusted6)
    assert not is_internal("2001:db8::1", Headers(), trusted6)
    # An IPv6 source against an IPv4-only trust list is external, not a crash.
    assert not is_internal("fd00::1", Headers(), TRUSTED)
    # IPv4-mapped IPv6 must not sneak past an IPv4 trust list.
    assert not is_internal("::ffff:172.26.0.13", Headers(), TRUSTED)


# --- subnet parsing ---------------------------------------------------------


def test_parse_subnets_drops_malformed_entries_without_crashing() -> None:
    nets = _parse_subnets("172.26.0.0/16, not-a-subnet, ,999.0.0.1/8,127.0.0.1/32")
    assert len(nets) == 2, "valid entries survive, invalid are dropped"
    assert is_internal("172.26.0.5", Headers(), nets, 8080, 8080)


def test_empty_trust_list_trusts_nobody() -> None:
    """Fail closed: an empty list must not mean 'allow all'."""
    assert not is_internal("172.26.0.13", Headers(), [], 8080, 8080)


def test_init_auth_refuses_a_token_without_trusted_subnets() -> None:
    """Refusing to start beats every peer call failing at once."""
    import pytest
    from flask import Flask

    from gateway.auth import init_auth

    with pytest.raises(ValueError, match="CORTEX_TRUSTED_SUBNETS"):
        init_auth(Flask(__name__), "s3cret", "")


def test_no_token_still_starts_without_trusted_subnets() -> None:
    """The open-gateway path is unchanged, so this can deploy before the token."""
    from flask import Flask

    from gateway.auth import init_auth

    init_auth(Flask(__name__), "", "")  # must not raise


# --- round 2 findings --------------------------------------------------------


def test_non_ascii_token_is_rejected_not_a_500() -> None:
    """hmac.compare_digest raises TypeError on non-ASCII str.

    Before this was fixed, `X-Cortex-Token: café` escaped _require_token as an
    unhandled exception. Flask turned it into a 500, which still denied the
    view but discarded the 401 body and the api_auth_rejected audit line, and
    handed any external caller a one-request way to spray tracebacks.
    """
    app = _app("s3cret")
    for bad in ("café", "tökén", "日本語", "\udcff"):
        r = _get(app, **{"X-Cortex-Token": bad})
        assert r.status_code == 401, f"{bad!r} must be a clean 401, not a 500"
        assert r.get_json()["error"] == "authentication required"


def test_a_non_ascii_token_can_still_authenticate() -> None:
    """Encoding both sides must not break a legitimately non-ASCII token."""
    app = _app("pàsswörd")
    assert _get(app, **{"X-Cortex-Token": "pàsswörd"}).status_code == 200
    assert _get(app, **{"X-Cortex-Token": "pàssword"}).status_code == 401


def test_unknown_paths_are_denied_by_default_end_to_end() -> None:
    """The deny-by-default property, through a real app rather than _is_exempt.

    A refactor that stopped calling _is_exempt() from _require_token would
    pass the unit tests while reopening the prefix hole. This drives the gate.
    """
    from flask import Flask, jsonify

    from gateway.auth import init_auth

    app = Flask(__name__)
    init_auth(app, "s3cret", _TRUSTED_FIXTURE, internal_port=8080)

    @app.route("/healthz-admin")
    def healthz_admin():
        return jsonify(ok=True)

    @app.route("/anything")
    def anything():
        return jsonify(ok=True)

    ext = {"REMOTE_ADDR": "10.0.0.99"}
    for path in ("/healthz-admin", "/anything"):
        assert (
            app.test_client()
            .get(path, base_url="http://localhost:8098", environ_base=ext)
            .status_code
            == 401
        ), f"{path} must be gated"
        assert (
            app.test_client()
            .get(
                path,
                base_url="http://localhost:8098",
                environ_base=ext,
                headers={"Authorization": "Bearer s3cret"},
            )
            .status_code
            == 200
        )


def test_create_app_refuses_to_boot_without_trusted_subnets(monkeypatch, reloaded_gateway) -> None:
    """The real startup chain: env var -> config -> create_app -> raise.

    This PR changed config.py's CORTEX_TRUSTED_SUBNETS default to "", which is
    exactly the input that trips the raise, so the chain is worth exercising
    rather than only the bare-Flask path.
    """
    monkeypatch.setenv("CORTEX_API_TOKEN", "s3cret")
    monkeypatch.setenv("CORTEX_TRUSTED_SUBNETS", "")
    monkeypatch.setenv("POSTGRES_PASSWORD", "x")
    monkeypatch.setenv("OAUTH_SECRET_KEY", "x")
    monkeypatch.setenv("OAUTH_TOKEN_PATH", "/tmp/t.json")

    gateway_app = reloaded_gateway()
    with pytest.raises(ValueError, match="CORTEX_TRUSTED_SUBNETS"):
        gateway_app.create_app()


def test_create_app_binds_the_gate_to_the_internal_port_not_the_external_one(
    monkeypatch, reloaded_gateway
) -> None:
    """The real wiring, which every other port test bypasses.

    Round 4 review: swapping `config.internal_port` for `config.external_port`
    at the single `init_auth()` call site passes the whole suite while
    INVERTING the trust boundary -- the gate would trust the published port
    and 401 every peer. All the other port tests pass `internal_port=8080` as
    a literal to a bare Flask app, so none of them ever sees that call.

    The gate runs in before_request, ahead of routing, so an unknown path is
    enough to read it: 401 means the gate challenged, 404 means it let the
    request through to routing. No database, no real route.
    """
    monkeypatch.setenv("CORTEX_API_TOKEN", "s3cret")
    monkeypatch.setenv("CORTEX_TRUSTED_SUBNETS", "172.26.0.0/16")
    monkeypatch.setenv("CORTEX_INTERNAL_PORT", "8080")
    monkeypatch.setenv("POSTGRES_PASSWORD", "x")
    monkeypatch.setenv("OAUTH_SECRET_KEY", "x")
    monkeypatch.setenv("OAUTH_TOKEN_PATH", "/tmp/t.json")

    gateway_app = reloaded_gateway()

    # create_app() also opens the Postgres pool and binds the metrics port.
    # Neither is what this test is about, and both need infrastructure.
    with (
        patch("gateway.app.init_pool"),
        patch("gateway.app.start_metrics_server"),
    ):
        app = gateway_app.create_app()

    peer = {"REMOTE_ADDR": "172.26.0.13"}
    client = app.test_client()

    assert (
        client.get(
            "/_no_such_path", base_url="http://localhost:8080", environ_base=peer
        ).status_code
        == 404
    ), "a peer on the INTERNAL port must reach routing without a token"
    assert (
        client.get(
            "/_no_such_path", base_url="http://localhost:8098", environ_base=peer
        ).status_code
        == 401
    ), "the same peer on the EXTERNAL port must be challenged"


def test_create_app_wires_the_ui_token_and_origins_through(monkeypatch, reloaded_gateway) -> None:
    """The real chain: env -> Config -> create_app -> init_auth.

    Every other scoped-token test calls init_auth directly with literals, so
    none of them sees this call site. Dropping both arguments here passed the
    whole suite while leaving the feature dead in production: the extension
    would 401 on every request and the startup log would say ui_token_set=False.
    """
    origin = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setenv("CORTEX_API_TOKEN", "s3cret")
    monkeypatch.setenv("CORTEX_TRUSTED_SUBNETS", "172.26.0.0/16")
    monkeypatch.setenv("CORTEX_UI_TOKEN", "ui-s3cret")
    monkeypatch.setenv("CORTEX_UI_ORIGINS", origin)
    monkeypatch.setenv("POSTGRES_PASSWORD", "x")
    monkeypatch.setenv("OAUTH_SECRET_KEY", "x")
    monkeypatch.setenv("OAUTH_TOKEN_PATH", "/tmp/t.json")

    gateway_app = reloaded_gateway()
    with (
        patch("gateway.app.init_pool"),
        patch("gateway.app.start_metrics_server"),
    ):
        app = gateway_app.create_app()

    client = app.test_client()
    external = "http://localhost:8098"
    ui = {"Authorization": "Bearer ui-s3cret", "Origin": origin}

    # A route off the allowlist: 403, not 401, is what proves the UI token was
    # wired in -- an unconfigured one makes this request 401. Deliberately not
    # asserting on an allowlisted route here: every one this app serves calls
    # Postgres, so the request opened a real socket and the only honest
    # assertion left was "not 401", which a 500 also satisfies.
    assert client.put("/config", base_url=external, headers=ui).status_code == 403
    # And the origin reached the CORS layer.
    assert (
        client.get("/health", base_url=external, headers=ui).headers["Access-Control-Allow-Origin"]
        == origin
    )


def test_config_internal_port_matches_a_port_the_dockerfile_binds(
    monkeypatch, reloaded_gateway
) -> None:
    """The gate's port and gunicorn's binds must not drift apart.

    `internal_port` decides which socket is trusted; the Dockerfile's `-b`
    flags decide which sockets exist. They are configured in different files
    with no link between them, so setting CORTEX_INTERNAL_PORT to a port
    nothing binds silently makes every request external -- a gate that 401s
    the whole pipeline, with no error at startup to say why.

    Round 4 review flagged this as unguarded after CORTEX_EXTERNAL_PORT was
    found to be dead config.
    """
    import re
    from pathlib import Path

    # Read the default with the env var ABSENT, under the fixture's managed
    # reload. Without this the test reads whatever a previous test's reload
    # baked into the class -- which is how it went vacuous.
    monkeypatch.delenv("CORTEX_INTERNAL_PORT", raising=False)
    reloaded_gateway()

    import gateway.config as _config_module

    # src/gateway/config.py -> parents[2] IS the repo root. (An extra .parent
    # here walked out into the multi-repo tree, which is how the utils suite
    # broke in CI one round ago -- same mistake, different direction.)
    dockerfile = Path(_config_module.__file__).resolve().parents[2] / "Dockerfile"
    assert dockerfile.is_file(), f"expected a Dockerfile at {dockerfile}"

    # Accept the forms gunicorn actually takes, not just the one in the file
    # today: -b / --bind, = or space or quote separated, and any host part
    # (0.0.0.0, [::], a hostname, or none at all). The narrow version failed
    # CLOSED on a reformatted CMD -- safe, but it would have cried drift at
    # someone who changed nothing that matters.
    bound = {
        int(p)
        for p in re.findall(
            r"(?:-b|--bind)[=\"',\s]+(?:[\w.]+|\[[0-9a-fA-F:]+\])?:(\d+)",
            dockerfile.read_text(),
        )
    }
    assert bound, "no gunicorn -b binds found in the Dockerfile"

    default_internal = _config_module.Config().internal_port
    assert default_internal in bound, (
        f"internal_port default {default_internal} is not bound by the Dockerfile "
        f"(binds: {sorted(bound)}). The gate would treat every request as external."
    )
    assert len(bound) >= 2, (
        "the two-port split needs both an internal and an external bind; "
        f"Dockerfile binds only {sorted(bound)}"
    )


def test_a_configured_token_with_surrounding_whitespace_still_authenticates() -> None:
    """Both sides must be stripped, or neither.

    The extractor strips what the client presents. A configured token carrying
    a trailing newline -- CORTEX_API_TOKEN=$(cat secret), the ordinary way to
    load one -- could therefore never match a correctly-sent credential. The
    operator sees every request 401 with a token they can see is right, and
    nothing in the logs says why.
    """
    from flask import Flask, jsonify

    from gateway.auth import init_auth

    app = Flask(__name__)
    init_auth(app, "s3cret\n", _TRUSTED_FIXTURE, internal_port=8080)

    @app.route("/thing")
    def thing():
        return jsonify(ok=True)

    ext = {"REMOTE_ADDR": "10.0.0.99"}
    client = app.test_client()
    assert (
        client.get(
            "/thing",
            base_url="http://localhost:8098",
            environ_base=ext,
            headers={"Authorization": "Bearer s3cret"},
        ).status_code
        == 200
    ), "a trailing newline in CORTEX_API_TOKEN must not lock everyone out"
    assert (
        client.get("/thing", base_url="http://localhost:8098", environ_base=ext).status_code == 401
    ), "stripping must not weaken the gate"


def test_a_whitespace_only_token_refuses_to_boot_rather_than_disabling_auth() -> None:
    """Stripping must not turn "configured" into "unconfigured".

    CORTEX_API_TOKEN=$(cat secret) against an empty secret file yields "\\n".
    Stripped, that is "" -- falsy -- so the trusted-subnets guard would not
    fire and _require_token would return None for every request: the gate
    silently gone, announced by a startup log line saying the token is unset,
    which is false and points at the wrong problem.

    Introduced by the round 5 strip fix; found by round 6 review. Fail closed.
    """
    from flask import Flask

    from gateway.auth import init_auth

    for blank in ("\n", "   ", "\t\n "):
        app = Flask(__name__)
        with pytest.raises(ValueError, match="only whitespace"):
            init_auth(app, blank, _TRUSTED_FIXTURE, internal_port=8080)


def test_a_whitespace_only_token_does_not_slip_past_the_trusted_subnets_guard() -> None:
    """The startup guard must still fire, not be bypassed by the blank token.

    `init_auth(token="\\n", trusted_subnets="")` previously booted, where
    `init_auth(token="s3cret", trusted_subnets="")` correctly raises.
    """
    from flask import Flask

    from gateway.auth import init_auth

    with pytest.raises(ValueError):
        init_auth(Flask(__name__), "\n", "", internal_port=8080)


def test_an_unset_token_still_means_open_access() -> None:
    """The deliberate no-auth case must keep working: "" is not an error."""
    from flask import Flask, jsonify

    from gateway.auth import init_auth

    app = Flask(__name__)
    init_auth(app, "", "", internal_port=8080)

    @app.route("/thing")
    def thing():
        return jsonify(ok=True)

    assert (
        app.test_client()
        .get("/thing", base_url="http://localhost:8098", environ_base={"REMOTE_ADDR": "10.0.0.99"})
        .status_code
        == 200
    )
