"""Preflight validation -- all checks before any Claude call.

Order: repo not banned -> in domain -> not previously rejected ->
no duplicate PR -> issue still open -> CLA (flag, don't reject) ->
maintainer active -> assignment status.  Any failure short-circuits.
Zero Claude calls.

CLA-required repos are no longer rejected at preflight.  Instead, the
``needs_cla_signing`` flag is set on the returned ``PreflightMeta`` so
the pipeline can submit the PR and notify the owner to sign the CLA.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from osbot.config import settings
from osbot.intel.duplicates import check_claimed_in_comments
from osbot.log import get_logger
from osbot.safety.anti_spam import check_spam
from osbot.safety.circuit_breaker import can_attempt_repo
from osbot.types import GitHubCLIProtocol, MemoryDBProtocol, Outcome, ScoredIssue

logger = get_logger(__name__)

# After this many days, a REJECTED/STUCK outcome no longer blocks re-attempts.
# SUBMITTED/MERGED remain permanent (a duplicate PR or merged fix is final).
_OUTCOME_RETRY_DAYS = 7


@dataclass(frozen=True, slots=True)
class PreflightMeta:
    """Extra metadata from preflight checks, passed through the pipeline.

    Attributes:
        needs_cla_signing: True if the repo has a CLA requirement and
            the bot has not signed it.  The PR should still be submitted,
            and the owner should be notified to sign the CLA.
    """

    needs_cla_signing: bool = False


_QUESTION_TITLE_PREFIXES = (
    "how do ", "how to ", "how can ", "how should ",
    "why does ", "why is ", "why are ", "why can ",
    "what is ", "what are ", "what does ", "what should ",
    "is it possible", "is there a way", "is there any",
    "can you ", "can we ", "can someone ", "can anyone ",
    "could you ", "could we ", "could someone ",
    "should we ", "should i ", "would it be ",
    "looking for help", "need help with", "seeking help",
    "question:", "help:", "[question]", "[discussion]", "[rfc]",
)

_QUESTION_LABELS = frozenset({"question", "discussion", "invalid", "support", "wontfix", "won't fix"})

# Labels that explicitly signal the issue is blocked or won't be accepted.
# A PR fixing a "blocked" or "duplicate" issue will almost certainly be rejected.
_BLOCKED_LABELS = frozenset({
    "blocked", "on hold", "on-hold", "waiting", "waiting for feedback",
    "duplicate", "wontfix", "won't fix", "by design", "as designed",
    "intentional", "not a bug", "not-a-bug",
})


def _is_non_actionable(issue: ScoredIssue) -> bool:
    """Return True if the issue is a question, discussion, or support request.

    These issues do not have a concrete bug to fix — submitting a PR would
    be non-sensical and the critic would reject it, wasting 1-2 Claude calls.
    """
    title_lower = issue.title.lower().strip()
    if any(title_lower.startswith(p) for p in _QUESTION_TITLE_PREFIXES):
        return True
    # Check labels (lowercased)
    labels_lower = {lb.lower() for lb in (issue.labels or [])}
    return bool(labels_lower & _QUESTION_LABELS)


async def preflight(
    issue: ScoredIssue,
    db: MemoryDBProtocol,
    github: GitHubCLIProtocol,
) -> tuple[bool, str, PreflightMeta]:
    """Run all preflight gates.  Returns ``(passed, reason, meta)``.

    Every check is free (no Claude calls).  Failures short-circuit
    immediately with a descriptive reason string.

    The ``meta`` object carries flags for downstream stages (e.g.,
    ``needs_cla_signing`` so the pipeline can notify the owner after
    PR submission).
    """
    repo = issue.repo
    needs_cla = False

    # 0. Issue type filter — skip questions/discussions (saves 1-2 Claude calls each)
    if _is_non_actionable(issue):
        reason = "non-actionable issue (question, discussion, or support request)"
        logger.info("preflight_fail", repo=repo, gate="issue_type", reason=reason)
        return False, reason, PreflightMeta()

    # 0b. Blocked/won't-fix label filter — PRs on these issues are almost never merged.
    labels_lower = {lb.lower() for lb in (issue.labels or [])}
    blocked_hit = labels_lower & _BLOCKED_LABELS
    if blocked_hit:
        reason = f"issue labeled {sorted(blocked_hit)} — maintainer will not merge a fix"
        logger.info("preflight_fail", repo=repo, gate="blocked_label", reason=reason)
        return False, reason, PreflightMeta()

    # 1. Repo not banned (circuit breaker)
    ok, reason = await can_attempt_repo(repo, db)
    if not ok:
        logger.info("preflight_fail", repo=repo, gate="banned", reason=reason)
        return False, reason, PreflightMeta()

    # 2. Anti-spam (blacklist only — no artificial caps)
    ok, reason = await check_spam(repo, db)
    if not ok:
        logger.info("preflight_fail", repo=repo, gate="anti_spam", reason=reason)
        return False, reason, PreflightMeta()

    # 2b. Per-repo cooldown — don't submit multiple PRs to the same repo
    #     within 2 hours to avoid appearing as spam.
    cooldown_ok, cooldown_reason = await _check_repo_cooldown(repo, db)
    if not cooldown_ok:
        logger.info("preflight_fail", repo=repo, gate="repo_cooldown", reason=cooldown_reason)
        return False, cooldown_reason, PreflightMeta()

    # 3. Not previously rejected on this exact issue.
    # SUBMITTED/MERGED are permanent (duplicate-PR check catches open PRs separately).
    # REJECTED/STUCK use a time window: after _OUTCOME_RETRY_DAYS we allow another
    # attempt (timeouts, empty diffs, and CLI crashes from earlier bugs are retryable;
    # the issue-open and duplicate-PR checks below will still catch genuinely stale ones).
    existing = await db.get_outcome(repo, issue.number)
    if existing is not None:
        prev_outcome = existing.get("outcome", "")
        if prev_outcome in (Outcome.SUBMITTED.value, Outcome.MERGED.value, Outcome.ITERATED_MERGED.value):
            reason = f"already {prev_outcome} on #{issue.number}"
            logger.info("preflight_fail", repo=repo, gate="prior_outcome", reason=reason)
            return False, reason, PreflightMeta()
        if prev_outcome in (Outcome.REJECTED.value, Outcome.STUCK.value):
            created_str = existing.get("created_at", "") or ""
            try:
                created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                age_days = (datetime.now(UTC) - created_at).days
            except (ValueError, TypeError):
                age_days = 0  # Can't parse → assume recent, block it
            if age_days < _OUTCOME_RETRY_DAYS:
                reason = f"previously {prev_outcome} on #{issue.number} ({age_days}d ago, retry in {_OUTCOME_RETRY_DAYS - age_days}d)"
                logger.info("preflight_fail", repo=repo, gate="prior_outcome", reason=reason)
                return False, reason, PreflightMeta()
            # Outcome is old enough — allow retry (log so we can see it)
            logger.info(
                "prior_outcome_expired",
                repo=repo, issue=issue.number,
                prev_outcome=prev_outcome, age_days=age_days,
            )

    # 4. No duplicate PR (check if we already have an open PR for this issue)
    dup_ok, dup_reason = await _check_duplicate_pr(issue, github)
    if not dup_ok:
        logger.info("preflight_fail", repo=repo, gate="duplicate_pr", reason=dup_reason)
        return False, dup_reason, PreflightMeta()

    # 4b. Issue not claimed by another contributor in comments
    claim_ok, claim_reason = await _check_issue_claimed(issue, github)
    if not claim_ok:
        logger.info("preflight_fail", repo=repo, gate="issue_claimed", reason=claim_reason)
        return False, claim_reason, PreflightMeta()

    # 5. Issue still open
    open_ok, open_reason = await _check_issue_open(issue, github)
    if not open_ok:
        logger.info("preflight_fail", repo=repo, gate="issue_closed", reason=open_reason)
        return False, open_reason, PreflightMeta()

    # 6. CLA check -- flag instead of reject
    #    "cannot_sign" (corporate/physical) still rejects since we can never comply.
    #    "needs_signing" sets a flag so the owner gets notified after PR submission.
    cla_ok, cla_reason = await _check_cla(issue, db, github)
    if not cla_ok:
        if "cannot" in cla_reason.lower():
            # Corporate/physical CLA -- we genuinely cannot proceed
            logger.info("preflight_fail", repo=repo, gate="cla_cannot_sign", reason=cla_reason)
            return False, cla_reason, PreflightMeta()
        # Signable CLA -- proceed with PR, notify owner afterwards
        needs_cla = True
        logger.info(
            "preflight_cla_flagged",
            repo=repo,
            issue=issue.number,
            reason=cla_reason,
        )

    # 7. Maintainer active (last push within threshold)
    active_ok, active_reason = await _check_maintainer_active(issue, github)
    if not active_ok:
        logger.info("preflight_fail", repo=repo, gate="maintainer_inactive", reason=active_reason)
        return False, active_reason, PreflightMeta()

    # 8. Non-English issue detection — Claude cannot implement from non-English descriptions
    if _is_non_english(issue.title, issue.body):
        reason = "non-English issue title/body — implementation likely to produce empty diff"
        logger.info("preflight_fail", repo=repo, gate="non_english", reason=reason)
        return False, reason, PreflightMeta()

    # 9. Issue age check (soft — log only, no rejection).
    #    Issues opened <1h ago look suspicious when a PR arrives within the same hour.
    #    Typos/docs are exempt (humans can legitimately fix those in minutes).
    #    This does not reject but logs a bot-detection risk so we can monitor.
    if issue.created_at:
        try:
            created = datetime.fromisoformat(issue.created_at.replace("Z", "+00:00"))
            age_hours = (datetime.now(UTC) - created).total_seconds() / 3600
            labels_lower = {lb.lower() for lb in (issue.labels or [])}
            is_trivial = bool(labels_lower & {"typo", "documentation", "docs", "spelling",
                                              "cleanup", "chore"})
            if age_hours < 1.0 and not is_trivial:
                logger.warning(
                    "preflight_fresh_issue_warning",
                    repo=repo,
                    issue=issue.number,
                    age_hours=round(age_hours, 2),
                    note="PR on issue opened <1h ago may appear automated to maintainers",
                )
        except (ValueError, TypeError):
            pass  # Can't parse — skip silently

    logger.info("preflight_pass", repo=repo, issue=issue.number, needs_cla=needs_cla)
    return True, "", PreflightMeta(needs_cla_signing=needs_cla)


async def _check_duplicate_pr(
    issue: ScoredIssue,
    github: GitHubCLIProtocol,
) -> tuple[bool, str]:
    """Check if any PR (ours or others', open or merged) targets this issue.

    Catches three cases the old check missed:
    - Our own open PRs (original behavior)
    - Other contributors' open PRs for the same issue
    - Already-merged PRs that already fixed the issue
    """
    username = settings.github_username
    issue_ref = f"#{issue.number}"

    # Check ALL PRs (any author, any state) that reference this issue
    for state in ("open", "merged"):
        result = await github.run_gh([
            "pr", "list",
            "--repo", issue.repo,
            "--state", state,
            "--search", f"in:body {issue_ref}",
            "--json", "number,title,body,author",
            "--limit", "10",
        ])
        if not result.success:
            continue

        try:
            prs = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue

        for pr in prs:
            body = pr.get("body", "") or ""
            title = pr.get("title", "") or ""
            author = pr.get("author", {}).get("login", "")

            if issue_ref not in body and issue_ref not in title:
                continue

            if state == "merged":
                return False, f"already fixed: merged PR #{pr['number']} by {author} references {issue_ref}"

            if author.lower() == username.lower():
                return False, f"duplicate: our open PR #{pr['number']} already references {issue_ref}"
            else:
                return False, f"competing PR: open PR #{pr['number']} by {author} already targets {issue_ref}"

    return True, ""


async def _check_issue_claimed(
    issue: ScoredIssue,
    github: GitHubCLIProtocol,
) -> tuple[bool, str]:
    """Check if another contributor has claimed the issue in comments.

    Fetches issue comments via ``gh`` CLI and scans for claim language
    (e.g. "I'm working on this", "WIP", "claimed") from the last 7 days.
    This catches competing contributors that the timeline-based duplicate
    check would miss.
    """
    result = await github.run_gh([
        "issue", "view", str(issue.number),
        "--repo", issue.repo,
        "--json", "comments",
    ])
    if not result.success:
        # Can't check -- allow through (fail-open for non-critical gate)
        return True, ""

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return True, ""

    comments = data.get("comments", [])
    if not comments:
        return True, ""

    claimed, claimer = check_claimed_in_comments(comments)
    if claimed:
        return False, f"issue claimed by another contributor ({claimer})"

    return True, ""


async def _check_issue_open(
    issue: ScoredIssue,
    github: GitHubCLIProtocol,
) -> tuple[bool, str]:
    """Verify the issue is still open via gh CLI."""
    result = await github.run_gh([
        "issue", "view", str(issue.number),
        "--repo", issue.repo,
        "--json", "state",
    ])
    if not result.success:
        # Can't verify -- allow through
        return True, ""

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return True, ""

    state = data.get("state", "OPEN")
    if state != "OPEN":
        return False, f"issue #{issue.number} is {state}"

    return True, ""


async def _check_cla(
    issue: ScoredIssue,
    db: MemoryDBProtocol,
    github: GitHubCLIProtocol,
) -> tuple[bool, str]:
    """Check if the repo requires a CLA we haven't signed."""
    cla_required = await db.get_repo_fact(issue.repo, "cla_required")
    if cla_required and cla_required.lower() == "true":
        cla_signed = await db.get_repo_fact(issue.repo, "cla_signed")
        if not cla_signed or cla_signed.lower() != "true":
            return False, "CLA required but not signed"

    return True, ""


async def _check_maintainer_active(
    issue: ScoredIssue,
    github: GitHubCLIProtocol,
) -> tuple[bool, str]:
    """Check if the repo had a push within the configured threshold."""
    result = await github.run_gh([
        "api", f"repos/{issue.repo}",
        "--jq", ".pushed_at",
    ])
    if not result.success:
        return True, ""

    pushed_at = result.stdout.strip()
    if not pushed_at:
        return True, ""

    try:
        push_dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        age_days = (datetime.now(UTC) - push_dt).days
        if age_days > settings.repo_max_push_age_days:
            return False, f"last push {age_days}d ago (limit {settings.repo_max_push_age_days}d)"
    except (ValueError, TypeError):
        pass

    return True, ""


def _is_non_english(title: str, body: str) -> bool:
    """Return True if the issue text is primarily non-English (non-ASCII).

    Heuristic: if >40% of alphabetic characters in the title are non-ASCII,
    the issue is likely in a language Claude cannot implement from (Chinese,
    Japanese, Korean, Arabic, etc.). Body is checked as a secondary signal
    only when the title is ambiguous.

    This is intentionally conservative — mixed English/code content is fine.
    """
    def _non_ascii_ratio(text: str) -> float:
        alpha = [c for c in text if c.isalpha()]
        if not alpha:
            return 0.0
        non_ascii = sum(1 for c in alpha if ord(c) > 127)
        return non_ascii / len(alpha)

    # Title is the primary signal (short, descriptive)
    title_ratio = _non_ascii_ratio(title or "")
    if title_ratio > 0.40:
        return True

    # Body check: only if title had some non-ASCII (e.g. title is in English
    # but body is all in Chinese)
    if title_ratio > 0.10:
        body_ratio = _non_ascii_ratio((body or "")[:500])
        if body_ratio > 0.50:
            return True

    return False


_REPO_COOLDOWN_HOURS = 2


async def _check_repo_cooldown(
    repo: str,
    db: MemoryDBProtocol,
) -> tuple[bool, str]:
    """Block if we submitted a PR to this repo within the cooldown window.

    Prevents multi-PR spam that gets the account flagged.  Queries the
    outcomes table for the most recent submitted/merged entry for the repo.
    """
    row = await db.fetchone(
        """SELECT created_at FROM outcomes
           WHERE repo = ? AND outcome IN ('submitted', 'merged', 'iterated_merged')
           ORDER BY created_at DESC LIMIT 1""",
        (repo,),
    )
    if row is None:
        return True, ""

    last_ts = row[0] if isinstance(row, (tuple, list)) else row.get("created_at", "")
    if not last_ts:
        return True, ""

    try:
        last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        elapsed = datetime.now(UTC) - last_dt
        remaining_sec = (_REPO_COOLDOWN_HOURS * 3600) - elapsed.total_seconds()
        if remaining_sec > 0:
            remaining_min = int(remaining_sec / 60)
            return False, f"repo cooldown: last PR submitted {int(elapsed.total_seconds()/60)}m ago ({remaining_min}m remaining)"
    except (ValueError, TypeError):
        pass

    return True, ""
