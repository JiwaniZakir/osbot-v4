# Trigger Branch Strategy

## Problem

When morning, evening, and Friday triggers all push directly to `dev`, they
create merge conflicts with each other. A trigger that starts while another is
mid-flight will see uncommitted remote changes and fail or produce a dirty
merge.

## Current Flow

```
trigger → push to dev → PR to main
```

All triggers race on the same `dev` branch. If two triggers overlap, the
second push fails or silently overwrites the first.

## Recommended Flow

```
trigger → push to cron/<name>-MMDD → PR to dev (auto-merge) → dev accumulates → PR to main
```

Each trigger gets its own short-lived branch named by trigger type and date:

- `cron/morning-0405`
- `cron/evening-0405`
- `cron/friday-0404`

### Steps

1. **Trigger runs.** Claude Code makes changes on a fresh
   `cron/<name>-MMDD` branch created from the latest `dev`.
2. **PR to dev.** The trigger opens a PR from `cron/<name>-MMDD` into `dev`
   with auto-merge enabled. Because each branch is independent, there are no
   conflicts between concurrent triggers.
3. **Dev accumulates.** Throughout the week, `dev` collects all merged trigger
   PRs. This is the integration branch.
4. **PR to main.** Periodically (or on Friday), a PR from `dev` into `main`
   is opened for final review.

### Benefits

- **No push conflicts.** Each trigger writes to its own branch.
- **Full audit trail.** Every trigger produces a separate PR with its own diff
  and CI status.
- **Safe rollback.** A bad trigger can be reverted by closing its PR without
  affecting other triggers.
- **Parallel execution.** Morning and evening triggers can run simultaneously
  without coordination.

### Branch Cleanup

Branches matching `cron/*` should be deleted after merge. GitHub's "delete
branch on merge" setting handles this automatically. For manual cleanup:

```bash
git branch -r --merged dev | grep 'origin/cron/' | sed 's|origin/||' | xargs -I{} git push origin --delete {}
```
