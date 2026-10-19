"""Flask application factory for Cortex Gateway."""

import httpx
from cortex_utils.api import MetricsMiddleware, health_bp, register_health_check
from cortex_utils.logging import configure_logging, get_logger
from cortex_utils.metrics import start_metrics_server
from flask import Flask

from gateway.auth import init_auth
from gateway.blueprints import (
    backfill_bp,
    config_bp,
    emails_bp,
    mappings_bp,
    oauth_bp,
    queue_bp,
    sync_bp,
    triage_bp,
)
from gateway.config import config
from gateway.services.postgres import ConnectionContext, init_pool

logger = get_logger()


def create_app() -> Flask:
    """Create and configure the Flask application."""
    # Configure logging
    log_level = config.log_level
    configure_logging(service_name="cortex-gateway", level=log_level)

    app = Flask("cortex-gateway")

    # Configure secret key for session management (OAuth CSRF protection)
    app.secret_key = config.oauth_secret_key
    if not app.secret_key:
        logger.critical(
            "oauth_secret_key_not_set",
            message="OAUTH_SECRET_KEY must be set for production deployments.",
        )
        raise ValueError("OAUTH_SECRET_KEY must be set for production deployments.")
    if not config.oauth_token_path:
        logger.critical(
            "oauth_token_path_not_set",
            message="OAUTH_TOKEN_PATH must be set for production deployments.",
        )
        raise ValueError("OAUTH_TOKEN_PATH must be set for production deployments.")

    # Require a token for anything arriving from outside the container network
    # Raises if a token is set without trusted subnets: refusing to start is a
    # better failure than every peer call beginning to 401 at once.
    init_auth(
        app,
        config.api_token,
        config.trusted_subnets,
        config.internal_port,
        ui_token=config.ui_token,
        ui_origins=config.ui_origins,
    )

    # Apply metrics middleware
    app.wsgi_app = MetricsMiddleware(app.wsgi_app, "cortex-gateway")  # type: ignore[method-assign]

    # Register blueprints
    app.register_blueprint(health_bp)
    app.register_blueprint(emails_bp, url_prefix="/emails")
    app.register_blueprint(queue_bp, url_prefix="/queue")
    app.register_blueprint(backfill_bp, url_prefix="/backfill")
    app.register_blueprint(triage_bp, url_prefix="/triage")
    app.register_blueprint(sync_bp, url_prefix="/sync")
    app.register_blueprint(config_bp, url_prefix="/config")
    app.register_blueprint(mappings_bp, url_prefix="/mappings")
    app.register_blueprint(oauth_bp)

    # Register health checks
    register_health_check(app, check_postgres)
    register_health_check(app, check_duckdb)

    # Initialize on first request
    with app.app_context():
        init_pool()

    # Start metrics server
    start_metrics_server(port=config.metrics_port)

    logger.info(
        "gateway_started",
        host=config.host,
        port=config.port,
        metrics_port=config.metrics_port,
    )

    return app


def check_postgres() -> tuple[str, bool]:
    """Health check for Postgres connection."""
    try:
        with ConnectionContext() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return ("postgres", True)
    except Exception as e:
        logger.warning("postgres_health_check_failed", error_type=type(e).__name__)
        return ("postgres", False)


def check_duckdb() -> tuple[str, bool]:
    """Health check for DuckDB API."""
    try:
        resp = httpx.get(f"{config.duckdb_api_url}/health", timeout=5.0)
        return ("duckdb", resp.status_code == 200)
    except httpx.RequestError as e:
        logger.warning("duckdb_health_check_failed", error_type=type(e).__name__)
        return ("duckdb", False)


if __name__ == "__main__":
    # For local development only
    application = create_app()
    application.run(host=config.host, port=config.port, debug=True)
