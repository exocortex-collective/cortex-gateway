"""Configuration for Cortex Gateway."""

import os
from dataclasses import dataclass


def _get_int_env(key: str, default: str) -> int:
    """Get integer from environment variable with error handling."""
    try:
        return int(os.environ.get(key, default))
    except ValueError as e:
        raise ValueError(f"Invalid integer value for {key}: {os.environ.get(key)}") from e


@dataclass
class Config:
    """Gateway configuration from environment variables."""

    # Postgres
    postgres_host: str = os.environ.get("POSTGRES_HOST", "localhost")
    postgres_port: int = _get_int_env("POSTGRES_PORT", "5432")
    postgres_db: str = os.environ.get("POSTGRES_DB", "cortex")
    postgres_user: str = os.environ.get("POSTGRES_USER", "cortex")
    postgres_password: str = os.environ["POSTGRES_PASSWORD"]

    # DuckDB API
    duckdb_api_url: str = os.environ.get("DUCKDB_API_URL", "http://localhost:8081")

    # Server
    host: str = os.environ.get("HOST", "0.0.0.0")
    port: int = _get_int_env("PORT", "8080")
    metrics_port: int = _get_int_env("METRICS_PORT", "8001")

    # OAuth
    oauth_token_path: str = os.environ.get("OAUTH_TOKEN_PATH", "")
    oauth_secret_key: str = os.environ.get("OAUTH_SECRET_KEY", "")

    # API auth. Requests from inside the container network are trusted and
    # send no credential; anything else must present this token. Unset means
    # the gateway stays open (and logs a warning at startup).
    api_token: str = os.environ.get("CORTEX_API_TOKEN", "")
    # No default: deployment topology, and this repo is public. Required
    # whenever api_token is set -- init_auth refuses to start without it.
    trusted_subnets: str = os.environ.get("CORTEX_TRUSTED_SUBNETS", "")
    # Port carrying peer traffic. Publish ONLY the external port to the host;
    # requests arriving on any other port must present the token.
    #
    # The BINDS are Dockerfile literals -- gunicorn's -b flags -- so this is
    # the gate's view of which of them is internal, not a way to move them.
    # Change it only alongside the Dockerfile; test_config_internal_port_
    # matches_a_port_the_dockerfile_binds fails if the two drift.
    #
    # There is deliberately no `external_port`: nothing read it, so
    # CORTEX_EXTERNAL_PORT was a knob that silently did nothing.
    internal_port: int = _get_int_env("CORTEX_INTERNAL_PORT", "8080")

    # A SECOND, SCOPED token for the browser extension, because api_token is
    # omnipotent and this one lives in chrome.storage. With api_token a caller
    # can PUT /config -- rewriting every triage rule in one request -- delete
    # mappings, refresh the Gmail OAuth credential, and read a server
    # filesystem path out of /oauth/status. Extension storage is readable by
    # anything with the browser profile and by any extension holding
    # `management` or `debugger` permission, so the UI gets a credential that
    # is accepted on UI_ALLOWED routes only; everything else is 403.
    #
    # Unset means no scoped access, which is the safe default: the UI simply
    # cannot authenticate.
    ui_token: str = os.environ.get("CORTEX_UI_TOKEN", "")
    # Exact browser origins allowed to make cross-origin calls, comma
    # separated, e.g. chrome-extension://<id>. No default and never "*": a
    # wildcard here would let any page in any browser read the whole API with
    # a token the user has already stored.
    ui_origins: str = os.environ.get("CORTEX_UI_ORIGINS", "")

    # Logging
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")

    @property
    def postgres_dsn(self) -> str:
        """Build Postgres connection string."""
        return (
            f"host={self.postgres_host} "
            f"port={self.postgres_port} "
            f"dbname={self.postgres_db} "
            f"user={self.postgres_user} "
            f"password={self.postgres_password}"
        )


config = Config()
