#!/bin/bash
# Prepflow push helper.
# Pulls remote first, stages every local change, commits, pushes.
#
#   bash "/Users/tonydibbin/prep flow/site/push.sh"     # commit + push with default message
#   bash "/Users/tonydibbin/prep flow/site/push.sh" "your commit message"

set -e

cd "/Users/tonydibbin/prep flow/site"

echo "→ Cleaning stale .git lock files (if any)..."
find .git -name "*.lock" -delete 2>/dev/null || true

# Identity (sandbox-built commits sometimes lack this)
git config user.email "tony@tonydibbin.com" >/dev/null 2>&1
git config user.name "Tony Dibbin" >/dev/null 2>&1

# 1. Stash any uncommitted local changes so the pull-rebase doesn't choke on them.
echo "→ Stashing any uncommitted changes..."
git stash push -u -m "auto-stash before push.sh" >/dev/null 2>&1 || true

# 2. Pull remote with rebase to fast-forward over the bot's auto-build commits.
echo "→ Pulling remote (rebase)..."
git pull --rebase origin main || {
  echo "✗ Pull failed. Resolve conflicts manually, then re-run push.sh."
  exit 1
}

# 3. Restore the stashed changes (no-op if there was nothing to stash).
echo "→ Restoring stashed changes..."
git stash pop 2>/dev/null || true

# 4. Stage everything and commit if there's something to commit.
echo "→ Staging changes..."
git add -A

MSG="${1:-prepflow: local edits $(date +%Y-%m-%d_%H:%M)}"
if git diff --cached --quiet; then
  echo "  (nothing new to commit)"
else
  git commit -m "$MSG"
  echo "  committed: $MSG"
fi

# 5. Push.
echo "→ Pushing to GitHub..."
echo "   Username = tonydibbin"
echo "   Password = paste your token (Cmd+V); nothing will show on screen"
echo ""
git push -u origin main

echo ""
echo "✓ Done. https://github.com/tonydibbin/prepflow.theradiobusiness.co.uk"
