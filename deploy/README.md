# Deploy

## Auto-deploy trigger

`.github/workflows/deploy-vps.yml` fires on:

| Event | Example | Purpose |
|---|---|---|
| `workflow_dispatch` from `pr-quality-gate.yml` after auto-merge | Claude Quality Gate APPROVE → auto-merge → explicit dispatch | **Primary path for bot-authored merges.** GitHub's anti-loop rule suppresses both `push` and `pull_request: closed` events when a commit is made by `GITHUB_TOKEN`, so the Quality Gate dispatches the deploy itself once it confirms the merge landed. |
| `pull_request: closed + merged` → `main` | Human merge via GitHub UI | Fires for human-initiated merges (human tokens aren't anti-looped). |
| `push` → `main` | Direct human push | Safety net for bypass-branch-protection pushes. |
| `workflow_dispatch` | `gh workflow run deploy-vps.yml --ref main` | Manual rollback / replay. |

Concurrency key is `deploy-vps` with `cancel-in-progress: false` — two deploys cannot run in parallel; the second waits for the first to finish.

## State preservation across rebuilds

Three named volumes live outside the image and survive every rebuild. The container is **stateless**; all persistence is volume-backed.

| Volume | Mount | Contents |
|---|---|---|
| `deploy_osbot-v4-state` (external) | `/opt/osbot/state` | `state.json` (issue queue, active work, open PRs), `memory.db` (SQLite: repo_facts, outcomes, profiles, signals, bans, usage), `traces.jsonl`, `corrections.jsonl`, `blocker_dedup.json` |
| `deploy_claude-creds` (external) | `/home/botuser/.claude` | OAuth credentials, session cache |
| `deploy_gh-config` (external) | `/home/botuser/.config/gh` | `gh` CLI auth |

All three are `external: true` — `docker compose down -v` is physically incapable of destroying them. To destroy state intentionally: `docker volume rm deploy_osbot-v4-state`.

### Graceful shutdown

`stop_grace_period: 60s` in `docker-compose.yml` gives `src/osbot/orchestrator/loop.py` time to:

1. Receive SIGTERM (handler registered at `loop.py:848-857`)
2. Set the shutdown flag
3. Let the current cycle finish its in-flight pipeline attempts
4. Flush `state.json` via atomic tempfile rename
5. Close the SQLite WAL

After 60s, Docker sends SIGKILL. The worst case — SIGKILL mid-write — is safe because:
- `state.json` writes go to a temp file then `os.rename()` — atomic
- SQLite uses WAL mode; unflushed writes replay from the WAL on next startup
- `traces.jsonl` / `corrections.jsonl` are append-only with per-line flushes

### In-flight work on restart

Anything in `/opt/osbot/workspaces` (temp clone dirs for active implementations) is **not** volume-mounted and is lost on restart. The issue queue in `state.json` preserves what the bot was working on, and the orchestrator re-picks from the queue next cycle.

## Bootstrap on a fresh host

```bash
# 1. External volumes (state + auth)
docker volume create deploy_osbot-v4-state
docker volume create deploy_claude-creds
docker volume create deploy_gh-config

# 2. Seed OAuth + gh config into volumes (one-time)
#    See ../docs/operations.md for the token rotation flow
docker run --rm -v deploy_claude-creds:/c alpine sh -c 'cat > /c/.credentials.json' < claude-creds.json
docker run --rm -v deploy_gh-config:/g alpine sh -c 'cat > /g/hosts.yml'            < gh-hosts.yml

# 3. Build + launch
cd /opt/osbot-v4/deploy
docker compose build
docker compose up -d --wait
```

## Rollback

```bash
# Manual dispatch of the deploy workflow against a specific SHA
gh workflow run deploy-vps.yml --repo JiwaniZakir/osbot-v4 --ref <sha>
```

For a failed deploy that left an unhealthy container:

```bash
ssh aegis-ext
cd /opt/osbot-v4
git reset --hard <last-good-sha>
cd deploy
docker compose build
docker compose up -d --wait
```

Volumes preserve state — the rollback restores code and restarts, memory is untouched.
