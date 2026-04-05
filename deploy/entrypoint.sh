#!/bin/bash
set -e

# Ensure Python can find our source
export PYTHONPATH="${PYTHONPATH:-/opt/osbot/src}"

git config --global user.name "Zakir Jiwani"
git config --global user.email "108548454+JiwaniZakir@users.noreply.github.com"

# Use GIT_ASKPASS for token auth (avoids writing token to disk)
if [ -n "$GH_TOKEN" ]; then
  export GIT_ASKPASS="$HOME/.git-askpass"
  cat > "$GIT_ASKPASS" <<'SCRIPT'
#!/bin/bash
echo "$GH_TOKEN"
SCRIPT
  chmod +x "$GIT_ASKPASS"

  # Configure gh CLI auth
  echo "$GH_TOKEN" | gh auth login --with-token 2>/dev/null || true
  gh auth setup-git 2>/dev/null || true
fi

# Ensure session-env exists (required for Claude CLI Bash tool)
mkdir -p "$HOME/.claude/session-env"

# Configure git to use gh CLI for authentication (enables git push to GitHub)
gh auth setup-git 2>/dev/null || true

# Ensure default Claude model setting
if [ ! -f "$HOME/.claude/settings.json" ]; then
  mkdir -p "$HOME/.claude"
  echo '{"model":"claude-sonnet-4-6"}' > "$HOME/.claude/settings.json"
fi

# Restore .claude.json from backup if missing (survives container rebuilds)
if [ ! -f "$HOME/.claude.json" ]; then
  BACKUP=$(ls -t "$HOME/.claude/backups/.claude.json.backup."* 2>/dev/null | head -1)
  if [ -n "$BACKUP" ]; then
    cp "$BACKUP" "$HOME/.claude.json"
  fi
fi

# Ensure state directory exists
mkdir -p "${OSBOT_STATE_DIR:-/opt/osbot/state}"
mkdir -p "${OSBOT_WORKSPACES_DIR:-/opt/osbot/workspaces}"

exec "$@"
