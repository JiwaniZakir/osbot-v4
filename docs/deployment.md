# Deployment

osbot-v4 runs in Docker on a Hetzner CX22 VPS.

## Quick Deploy

```bash
# Push code changes then rebuild on VPS
ssh aegis-ext 'cd /opt/osbot-v4/deploy && git pull && docker compose build --no-cache && docker compose up -d'
```

## Full Deploy

```bash
ssh aegis-ext
cd /opt/osbot-v4/deploy
./deploy.sh
```

## Docker Compose

The service runs with:
- **CPU**: 2.0 limit, 0.5 reserved
- **Memory**: 4GB limit, 512MB reserved
- **Restart**: unless-stopped
- **Logging**: JSON file driver, 50MB max, 5 files retained

## Health Check

The container runs a health check every 60 seconds via `deploy/health_check.py`.

## Automated Audits

Two CCR (Claude Code Remote) agents run daily:
- **Morning (6:30 AM ET)**: Analyzes PR outcomes, plans improvements, implements and deploys changes
- **Evening (9:00 PM ET)**: Verifies morning changes worked, tracks new errors, makes small refinements
