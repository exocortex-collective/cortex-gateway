# Cortex Gateway

Unified REST API gateway for Cortex email automation services.

## Quick Reference

- **Framework**: Flask with gunicorn
- **Endpoints**: /health, /emails/*, /queue/*, /backfill/*, /triage/*
- **Dependencies**: Postgres (queue, metadata), DuckDB API (email bodies)

## Project Structure

```
gateway/
├── src/gateway/
│   ├── app.py               # Flask app factory
│   ├── config.py            # Environment config
│   ├── blueprints/
│   │   ├── emails.py        # /emails/* routes
│   │   ├── queue.py         # /queue/* routes
│   │   ├── backfill.py      # /backfill/* routes
│   │   └── triage.py        # /triage/* routes
│   └── services/
│       ├── postgres.py      # DB queries
│       └── duckdb.py        # DuckDB API client
├── tests/
├── pyproject.toml
└── Dockerfile
```

## API Endpoints

### Health
- `GET /health` - Service health with dependency checks

### Emails
- `GET /emails` - List emails (paginated)
- `GET /emails/<gmail_id>` - Get email details
- `GET /emails/<gmail_id>/body` - Get raw body (via DuckDB)
- `GET /emails/<gmail_id>/text` - Get decoded plain text
- `GET /emails/stats` - Email statistics

### Queue
- `GET /queue/stats` - Queue depths by name/status
- `GET /queue/failed` - List dead letters
- `POST /queue/failed/<id>/retry` - Retry a failed job
- `DELETE /queue/failed/<id>` - Delete a failed job
- `POST /queue/failed/retry-all?queue=X` - Retry all failed for queue

### Backfill
- `POST /backfill` - Trigger backfill (queue, days, label, priority)
- `GET /backfill/status` - Check backfill progress
- `POST /backfill/cancel` - Cancel pending backfill jobs

### Triage
- `GET /triage/stats` - Classification statistics
- `POST /triage/rerun` - Re-run triage on emails
- `GET /triage/classifications` - List recent classifications

## Environment Variables

```bash
# Postgres
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=cortex
POSTGRES_USER=cortex
POSTGRES_PASSWORD=secret

# DuckDB API
DUCKDB_API_URL=http://localhost:8081

# Server
HOST=0.0.0.0
PORT=8080
METRICS_PORT=8001
```

## Development

```bash
uv sync --extra dev
uv run python -m gateway.app  # Development server

# Or with gunicorn
uv run gunicorn -w 4 -b 0.0.0.0:8080 'gateway.app:application'
```

## Testing

```bash
uv run pytest
uv run ruff check src/
uv run mypy src/
```

## Git Workflow

**NEVER push directly to main.** Always:

1. Create a feature branch
2. Make changes and commit
3. Push the branch and create a PR
4. Wait for CI; run pr-review-loop (Gemini is disabled in
   `.reviewers/config.json`)
5. Merge once the pr-review-loop converges (one clean round) or Devon
   says yes

## Deployment

- Docker image: `us-central1-docker.pkg.dev/cortex-gmail/cortex/gateway:latest`
- Stack: `cortex-gateway` (ID 38) on Hades via Portainer
- Traefik route: `cortex.hades.local` (internal port 81)
- Ports: 8097 (API), 8096 (metrics)

### Access

```bash
# Direct access
curl http://10.5.2.21:8097/health

# Via Traefik (internal network)
curl -H "Host: cortex.hades.local" http://10.5.2.21:81/health
```

### Redeploy

```bash
~/.claude/skills/portainer/scripts/redeploy-stack.sh 38 11 /path/to/gateway-stack.yml true
```
