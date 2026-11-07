"""Flask blueprints for gateway routes."""

from gateway.blueprints.backfill import backfill_bp
from gateway.blueprints.config import config_bp
from gateway.blueprints.emails import emails_bp
from gateway.blueprints.labels import labels_bp
from gateway.blueprints.mappings import mappings_bp
from gateway.blueprints.oauth import oauth_bp
from gateway.blueprints.queue import queue_bp
from gateway.blueprints.sync import sync_bp
from gateway.blueprints.triage import triage_bp

__all__ = [
    "backfill_bp",
    "config_bp",
    "emails_bp",
    "labels_bp",
    "mappings_bp",
    "oauth_bp",
    "queue_bp",
    "sync_bp",
    "triage_bp",
]
