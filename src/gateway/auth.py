"""Token auth for requests that arrive from outside the Docker network.

TRUST IS DECIDED BY THE PORT, THEN CONFIRMED BY THE SOURCE.

The gateway binds two ports. The internal one carries peer traffic and is
never published to the host; the external one is the only thing Docker
publishes, and always requires a token. Which socket a request arrived on is
therefore a structural fact enforced by port publishing, not an inference we
make from an address -- and it cannot drift the way a subnet list can when a
network is added.

The address check below is kept as a second lock, so that publishing the
internal port by mistake does not silently hand the API to the LAN.

Peers on the container network (triage-worker, gmail-sync, the ofelia jobs)
are trusted and send no credential. Anything else must present
``Authorization: Bearer <CORTEX_API_TOKEN>`` or ``X-Cortex-Token``.

WHY "INSIDE DOCKER" IS NOT JUST A SUBNET CHECK

The gateway is attached to three networks: cortex_default (172.26/16),
metrics (172.29/16) and traefik-public (172.24/16). A request proxied by
Traefik arrives with Traefik's *container* address as its source, which is a
private Docker address like any peer's. Trusting "any Docker subnet" would
therefore hand the whole API to anyone who can reach the Traefik route --
failing open in exactly the case the token exists for.

So two conditions must BOTH hold for a request to be treated as internal:

  1. The source address is in a subnet where peer services actually live
     (cortex_default, metrics). traefik-public is deliberately excluded.
  2. No proxy headers are present. Traefik sets X-Forwarded-For; its presence
     means the request was relayed on behalf of someone else, whoever that is.

Spoofing the header cannot grant access -- it only ever removes trust. And a
LAN client cannot spoof condition 1: verified on this deployment that a LAN
request to a published port arrives with the client's own address intact,
not masqueraded to the bridge gateway, so LAN traffic is externally
addressed and needs the token. (Had Docker rewritten it, address checks
would have been useless and the port lock would carry the whole boundary.)

Exempt paths are health and metrics only, so monitoring keeps working without
distributing a credential to it.
"""

from __future__ import annotations

import hmac
import ipaddress
import re
from typing import Any

from cortex_utils.logging import get_logger
from flask import Flask, Response, jsonify, request

logger = get_logger()

# Port carrying peer traffic. Never publish it to the host: everything below
# assumes reaching it already required being inside the container network.
DEFAULT_INTERNAL_PORT = 8080

# No default. Which subnets carry peer traffic is deployment topology, and
# this repo is public -- baking in one deployment's Docker CIDRs both leaks
# it and silently misconfigures every other deployment. Required whenever a
# token is set; see init_auth.
#
# Critically, this list must EXCLUDE whatever subnet the reverse proxy sits
# on. A request relayed from outside arrives with the proxy's container
# address, which is private and otherwise indistinguishable from a peer's.
TRUSTED_SUBNETS_ENV = "CORTEX_TRUSTED_SUBNETS"

# Headers that mean "this was relayed for someone else".
PROXY_HEADERS = ("X-Forwarded-For", "X-Real-IP", "Forwarded")

# Exact paths, or a path plus a "/" segment boundary. NEVER a bare prefix
# match: `startswith("/health")` also admits `/healthz-admin`, so adding any
# future route whose name merely begins with an exempt one would silently
# punch a hole in the gate.
#
# ONLY /oauth/callback is exempt, and only because Google redirects the
# USER'S BROWSER to it -- a redirect structurally cannot carry an
# Authorization header, so gating it breaks the grant outright.
#
# /oauth/start was exempt in an earlier revision of this file and that was a
# VULNERABILITY, not a convenience. /start is a plain GET with no
# caller-supplied credential, so with both legs open any host that could
# reach the external port could drive the whole flow itself: hit /start,
# consent as its OWN Google account, land on the exempt /callback, and have
# _save_token() overwrite the production token -- silently repointing
# gmail-sync at an attacker's mailbox. The state parameter does not help,
# because an attacker driving both legs controls both sides of it.
#
# With /start gated, /callback alone is safe: it requires a session state
# value that only /start sets, so a caller who cannot reach /start cannot
# produce a callback that validates.
#
# Operators are not locked out. _require_token() treats internal requests as
# trusted before it ever reaches the token check, so the flow can still be
# driven from inside the container network; from outside it needs the token.
EXEMPT_PATHS = ("/health", "/metrics", "/oauth/callback")


def _is_exempt(path: str) -> bool:
    """Exact match, or an exempt path followed by a `/` segment boundary."""
    return any(path == e or path.startswith(e + "/") for e in EXEMPT_PATHS)


# ROUTES THE SCOPED UI TOKEN MAY REACH. Everything absent from this list is
# 403 for that token, including every route added in future -- the list is an
# allowlist precisely so that a new endpoint is closed until someone decides
# otherwise. See Config.ui_token for why the UI does not simply use api_token.
#
# Six entries name routes this service does not serve yet, measured against
# create_app's url_map: GET /queue/dead, /queue/health, /queue/stuck,
# /triage/questions, and POST /queue/dead/resubmit, /queue/dead/dismiss.
# Listing one early is inert -- the UI token reaches routing and gets a 404 --
# whereas the reverse, a route landing without being listed, 403s the UI and
# reads as a bug.
#
# Deliberately NOT here, and each for a reason: PUT/POST /config and
# /config/rollback (one request rewrites every triage rule), PUT and DELETE
# /mappings (a precedent should be added from the UI, not silently removed),
# /sync/* and /backfill/* (they spend Gmail API quota), /oauth/* (credential
# management, and /oauth/status returns a server filesystem path),
# POST /queue/failed/retry-all (bulk, unscoped by id). POST /triage/rerun is
# conditional rather than withheld -- see _ui_route_allowed.
UI_ALLOWED_EXACT: frozenset[tuple[str, str]] = frozenset(
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
        # The question-answer path writes a mapping, so the UI needs POST.
        # PUT and DELETE stay out: answering a question adds a precedent.
        ("POST", "/mappings"),
    }
)

UI_ALLOWED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"^/queue/failed/\d+/retry$")),
    # Destructive, and allowed anyway: the UI needs delete as an action
    # (Devon, 2026-10-19). It names one id and delete_failed refuses a row
    # whose status is not 'failed', so the blast radius is one dead letter.
    # retry-all is the unscoped sibling and stays withheld.
    ("DELETE", re.compile(r"^/queue/failed/\d+$")),
    ("POST", re.compile(r"^/triage/questions/[A-Za-z0-9_-]+/answer$")),
    ("GET", re.compile(r"^/config/diff/\d+/\d+$")),
    ("GET", re.compile(r"^/mappings/history/.+$")),
    ("GET", re.compile(r"^/emails/by-label/.+$")),
    ("GET", re.compile(r"^/emails/[^/]+$")),
    ("GET", re.compile(r"^/emails/[^/]+/text$")),
)


def _token_matches(presented: str | None, expected: str) -> bool:
    """Constant-time compare of a presented credential against a configured one.

    Compare BYTES. hmac.compare_digest raises TypeError on a str with
    non-ASCII characters, so a header of `X-Cortex-Token: café` escaped the
    gate as an unhandled exception: Flask turned it into a 500, discarding
    both the intended 401 body and the api_auth_rejected audit line, and
    handing any external caller a one-request way to spray tracebacks into
    the logs.
    """
    if not presented or not expected:
        return False
    return hmac.compare_digest(
        presented.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    )


def _ui_route_allowed(method: str, path: str, args: Any) -> bool:
    """Whether the scoped UI token may call this method and path.

    POST /triage/rerun is the one conditional case. With ?preview=1 the route
    runs its INSERT and rolls back, reporting a count and writing nothing, so
    the UI may ask. Without preview the same path enqueues thousands of rows
    at a chosen priority, so it stays with api_token.

    This permission is only sound while blueprints/triage.py honours
    ?preview=; the gate and the route share is_preview so they cannot
    disagree about what one is.
    """
    if (method, path) in UI_ALLOWED_EXACT:
        return True
    if method == "POST" and path == "/triage/rerun":
        return is_preview(args)
    return any(m == method and pat.match(path) for m, pat in UI_ALLOWED_PATTERNS)


def is_preview(args: Any) -> bool:
    """A truthy ?preview= value. "0" and "false" do not count.

    Public because blueprints/triage.py imports it: the gate's notion of
    "this is a preview" and the route's must be the same function, or the
    allowlist permits one thing and the handler does another.
    """
    raw = (args.get("preview") or "").strip().lower()
    return raw in ("1", "true", "yes")


def _parse_subnets(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            nets.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            logger.error("trusted_subnet_invalid", subnet=chunk)
    return nets


def is_internal(
    remote_addr: str | None,
    headers: Any,
    trusted: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
    server_port: int | str | None = None,
    internal_port: int | None = None,
) -> bool:
    """True only for an unrelayed peer-subnet request on the internal port.

    Both locks must hold. The port says "this socket is not published
    externally"; the address says "and you really are a peer, not the LAN
    reaching a port that got published by accident".
    """
    if internal_port is not None:
        # Fail closed. Previously a missing SERVER_PORT skipped this check
        # entirely and fell back to address-only trust, quietly contradicting
        # the "both locks must hold" design in the worst direction: an
        # unknown port became an allowed one.
        if server_port is None:
            return False
        try:
            if int(server_port) != internal_port:
                return False
        except (TypeError, ValueError):
            return False
    if not remote_addr:
        return False
    if any(headers.get(h) for h in PROXY_HEADERS):
        return False
    try:
        ip = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    return any(ip in net for net in trusted)


def init_auth(
    app: Flask,
    token: str,
    trusted_subnets: str = "",
    internal_port: int | None = DEFAULT_INTERNAL_PORT,
    *,
    ui_token: str = "",
    ui_origins: str = "",
) -> None:
    """Install the before_request gate.

    With no token configured the gateway stays open, but says so loudly at
    startup -- silently unauthenticated is how it got to production the first
    time.

    With a token configured, trusted_subnets is REQUIRED: an empty list would
    mean no request is ever internal, so every peer call would start failing
    at once. Refusing to start is a better failure than that.
    """
    # Strip the CONFIGURED token too, not just the presented one. The
    # extractor strips what the client sends, so a token set from a file or a
    # heredoc -- `CORTEX_API_TOKEN=$(cat secret)` with a trailing newline, the
    # usual way -- would never match a correctly-sent credential. The symptom
    # is a total lockout in which the operator's token is demonstrably right
    # and every request still 401s, with nothing in the logs to explain it.
    raw_token, token = token, token.strip()
    if raw_token and not token:
        # Stripping must never turn "configured" into "unconfigured". An empty
        # or unpopulated secret file gives CORTEX_API_TOKEN="\n", which strips
        # to "" -- falsy, so the guard below would not fire and _require_token
        # would return None on every request. The gate would be GONE, and the
        # only signal would be a startup line reading "CORTEX_API_TOKEN is
        # unset", which is both false and points at the wrong problem.
        #
        # Same argument as trusted_subnets below: refusing to start is a far
        # better failure than silently serving an open API.
        raise ValueError(
            "CORTEX_API_TOKEN is set but contains only whitespace. This is "
            "usually an empty or unpopulated secret file. Unset it deliberately "
            "to run without auth, or give it a real value."
        )

    trusted = _parse_subnets(trusted_subnets)
    # Same two arguments as the api token above, including the second one: a
    # whitespace-only value must not strip down to "unconfigured".
    raw_ui_token, ui_token = ui_token, ui_token.strip()
    if raw_ui_token and not ui_token:
        raise ValueError(
            "CORTEX_UI_TOKEN is set but contains only whitespace. This is "
            "usually an empty or unpopulated secret file. Unset it to run "
            "without scoped UI access, or give it a real value."
        )
    origins = frozenset(o.strip() for o in ui_origins.split(",") if o.strip())

    if "*" in origins:
        raise ValueError(
            "CORTEX_UI_ORIGINS must list exact origins, never '*'. A wildcard "
            "would let any page in the browser read the API with a token the "
            "user has already stored for the extension."
        )
    if ui_token and not origins:
        logger.warning(
            "ui_token_without_origins",
            message=(
                "CORTEX_UI_TOKEN is set but CORTEX_UI_ORIGINS is empty, so no "
                "browser origin can use it. Set the extension origin."
            ),
        )

    if token and not trusted:
        raise ValueError(
            f"{TRUSTED_SUBNETS_ENV} must be set when CORTEX_API_TOKEN is set. "
            "List the subnets peer services call from, e.g. the compose "
            "network's CIDR plus 127.0.0.1/32. Exclude the reverse-proxy "
            "network: a relayed request arrives with the proxy's own private "
            "address and would otherwise be trusted as a peer."
        )

    if not token:
        logger.warning(
            "api_token_not_set",
            message=(
                "CORTEX_API_TOKEN is unset: every caller is accepted, including "
                "from outside the Docker network. Set it to require a token."
            ),
        )
    else:
        logger.info(
            "api_token_enabled",
            trusted_subnets=[str(n) for n in trusted],
            internal_port=internal_port,
            exempt=list(EXEMPT_PATHS),
            ui_token_set=bool(ui_token),
            ui_origins=sorted(origins),
        )

    def _allowed_origin() -> str | None:
        """The request's Origin, if we allow it. None otherwise."""
        origin = request.headers.get("Origin")
        return origin if origin and origin in origins else None

    @app.after_request
    def _cors(response: Response) -> Response:
        """Attach CORS headers for an allowlisted origin.

        `Vary: Origin` goes on EVERY response, allowlisted or not. Without it
        a cache that saw an allowlisted request could hand the same response,
        carrying Allow-Origin for that extension, to a different origin.

        Access-Control-Allow-Credentials is deliberately never sent. The UI
        authenticates with a bearer token, and /oauth/start puts a Flask
        SESSION COOKIE on this origin -- allowing credentials would make that
        cookie rideable from a page.
        """
        response.headers.add("Vary", "Origin")
        allowed = _allowed_origin()
        if allowed:
            response.headers["Access-Control-Allow-Origin"] = allowed
        return response

    @app.before_request
    def _require_token() -> Response | tuple[Response, int] | None:
        # PREFLIGHT FIRST, BEFORE ANY CREDENTIAL CHECK. A CORS preflight is
        # sent by the browser with no Authorization header -- it cannot carry
        # one -- and _is_exempt matches on PATH only, so without this branch
        # every preflight 401s and the extension cannot make a single call.
        #
        # It answers 204 with headers and no body, and answers identically
        # for a path that does not exist, so it discloses nothing about the
        # route table. Only an allowlisted origin gets this far; anything
        # else falls through to the normal gate.
        if request.method == "OPTIONS" and _allowed_origin():
            preflight = Response(status=204)
            # DELETE and X-Updated-By are advertised although no UI_ALLOWED
            # route accepts them. A preflight is not a security control -- the
            # gate decides, and a withheld route is 403 either way -- but a
            # method omitted here is blocked by the BROWSER, so the extension
            # sees an opaque failed fetch instead of the deliberate 403.
            preflight.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            preflight.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, X-Created-By, X-Updated-By"
            )
            preflight.headers["Access-Control-Max-Age"] = "600"
            return preflight

        if not token:
            return None
        path = request.path or "/"
        if _is_exempt(path):
            return None
        if is_internal(
            request.remote_addr,
            request.headers,
            trusted,
            request.environ.get("SERVER_PORT"),
            internal_port,
        ):
            return None

        presented = _presented_token(request.headers)
        if _token_matches(presented, token):
            return None

        # The scoped UI token: authenticated, but only for UI_ALLOWED routes.
        # 403 rather than 401 so the extension can tell "this token is not
        # permitted here" from "this token is wrong" -- the first is a bug in
        # the UI, the second is a misconfigured credential, and a single 401
        # for both sends the user to re-enter a token that was fine.
        if _token_matches(presented, ui_token):
            if _ui_route_allowed(request.method, path, request.args):
                return None
            logger.warning(
                "ui_token_route_denied",
                remote_addr=request.remote_addr,
                path=path,
                method=request.method,
            )
            return (
                jsonify(
                    {
                        "error": "token not permitted for this route",
                        "path": path,
                    }
                ),
                403,
            )

        # Log the attempt, never the token. remote_addr is attacker-influenced,
        # so keep it a structured field rather than interpolating it.
        logger.warning(
            "api_auth_rejected",
            remote_addr=request.remote_addr,
            path=path,
            method=request.method,
            server_port=request.environ.get("SERVER_PORT"),
            had_credential=bool(presented),
        )
        return (
            jsonify(
                {
                    "error": "authentication required",
                    "detail": (
                        "requests from outside the Docker network must present "
                        "Authorization: Bearer <token> or X-Cortex-Token"
                    ),
                }
            ),
            401,
        )


def _presented_token(headers: Any) -> str:
    """Pull a bearer token out of the request, or return ''."""
    auth: str = headers.get("Authorization", "") or ""
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    direct: str = headers.get("X-Cortex-Token", "") or ""
    return direct.strip()


__all__ = ["init_auth", "is_internal", "is_preview"]
