"""Main orchestrator loop -- creates services, runs health check, enters cycle.

Each cycle:
  1. Run ``balancer.update()`` for the token management probe.
  2. Read ``balancer.current_workers`` for concurrency budget.
  3. Run phases inside a TaskGroup (discover, contribute, iterate, etc.).
  4. Call ``fast_diagnostic`` on recent traces.
  5. Apply corrections from diagnostics.
  6. Sleep until next cycle.

Graceful shutdown on SIGTERM / SIGINT: sets a flag, finishes the current
cycle, cancels remaining tasks, flushes state.json, and closes memory.db.
"""

from __future__ import annotations

import asyncio
import json
import re
import signal
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime
from pathlib import Path

from osbot.config import settings
from osbot.discovery import discover
from osbot.gateway.github import GitHubCLI
from osbot.iteration import apply_patch, check_prs, read_feedback
from osbot.learning.diagnostics import deep_diagnostic, fast_diagnostic
from osbot.log import get_logger
from osbot.pipeline import run_pipeline
from osbot.state import BotState, MemoryDB, TraceWriter, write_heartbeat
from osbot.tokens import Balancer
from osbot.types import Correction, FeedbackType, OpenPR, Outcome, Trace

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Phase runners
# ---------------------------------------------------------------------------


async def _phase_discover(
    github: GitHubCLI,
    db: MemoryDB,
    state: BotState,
) -> None:
    """Run the full discovery pipeline and enqueue scored issues."""
    # Guard: skip discovery when the queue is already well-stocked
    queue_len = len(state.issue_queue)
    if queue_len >= 30:
        logger.info("phase_discover_skip", reason=f"queue has {queue_len} items")
        return

    logger.info("phase_discover_start")
    try:
        scored_issues = await discover(github, db)
        if scored_issues:
            await state.enqueue(scored_issues)
            logger.info(
                "phase_discover_done",
                issues_found=len(scored_issues),
                top_score=scored_issues[0].score if scored_issues else 0.0,
            )
        else:
            logger.info("phase_discover_done", issues_found=0)
    except Exception as exc:
        logger.error("phase_discover_error", error=str(exc), exc_info=True)


async def _phase_contribute(
    workers: int,
    state: BotState,
    gateway_protocol: object,
    github: GitHubCLI,
    db: MemoryDB,
    balancer: Balancer,
    trace_writer: TraceWriter,
    recent_traces: list[Trace],
) -> None:
    """Pop issues from the queue and run the pipeline for each, up to *workers* concurrently."""
    logger.info("phase_contribute_start", workers=workers)

    async def _run_one() -> None:
        issue = await state.pop_and_mark_active()
        if issue is None:
            return
        try:
            result = await run_pipeline(issue, gateway_protocol, github, db, balancer)  # type: ignore[arg-type]
            logger.info(
                "contribute_result",
                repo=result.repo,
                issue=result.issue_number,
                outcome=result.outcome.value,
            )

            # Write trace for this pipeline completion
            trace = Trace(
                ts=datetime.now(UTC).isoformat(),
                repo=result.repo,
                issue_number=result.issue_number,
                phase="contribute",
                outcome=result.outcome.value,
                failure_reason=result.failure_reason,
                tokens_used=result.tokens_used,
                claude_calls=result.claude_calls,
                duration_sec=result.duration_sec,
                pr_number=result.pr_number,
            )
            try:
                await trace_writer.write_trace(trace)
                recent_traces.append(trace)
                # Keep only the last 50 traces in memory
                if len(recent_traces) > 50:
                    del recent_traces[: len(recent_traces) - 50]
            except Exception as trace_exc:
                logger.debug("trace_write_error", error=str(trace_exc))

            # Track submitted PRs for the iteration phase to monitor
            if result.outcome == Outcome.SUBMITTED and result.pr_number:
                pr_url = result.pr_url or f"https://github.com/{issue.repo}/pull/{result.pr_number}"
                await state.add_open_pr(
                    OpenPR(
                        repo=issue.repo,
                        issue_number=issue.number,
                        pr_number=result.pr_number,
                        url=pr_url,
                        branch=f"fix/{issue.number}",
                        submitted_at=datetime.now(UTC).isoformat(),
                    )
                )
                logger.info(
                    "pr_tracked",
                    repo=issue.repo,
                    pr_number=result.pr_number,
                )

                # Clear other queued issues for the same repo to prevent
                # immediate re-attempts that trigger repeated cooldown bans.
                dequeued = await state.remove_queued_for_repo(issue.repo)
                if dequeued:
                    logger.info(
                        "queued_issues_cleared_after_pr",
                        repo=issue.repo,
                        removed=dequeued,
                    )

                # Celebrate! Send webhook alert for successful PR submission
                from osbot.comms.webhook import send_alert

                pr_url = result.pr_url or f"https://github.com/{issue.repo}/pull/{result.pr_number}"
                await send_alert(
                    f"PR submitted! {issue.repo}#{issue.number} -> PR #{result.pr_number}: {pr_url}",
                    severity="info",
                )

            # Event-triggered learning on rejection
            # Note: on_merge() is called in the iterate phase when the PR is
            # actually detected as merged — not here, since pipeline.run()
            # only returns SUBMITTED, never MERGED.
            if result.outcome == Outcome.REJECTED and result.failure_reason:
                from osbot.learning.lessons import on_rejection

                await on_rejection(issue.repo, issue.number, result.failure_reason, db)

        except Exception as exc:
            logger.error("contribute_worker_error", repo=issue.repo, issue=issue.number, error=str(exc))
        finally:
            await state.complete(issue, "done")

    async with asyncio.TaskGroup() as tg:
        for _ in range(workers):
            tg.create_task(_run_one())


async def _cleanup_fork(repo: str, github: GitHubCLI) -> None:
    """Delete the bot's fork of *repo* after a PR is merged or closed.

    Prevents accumulation of orphaned forks (compliance / account hygiene).
    Failure is non-fatal -- logged and ignored.
    """
    if not settings.github_username:
        return
    repo_name = repo.split("/")[-1]
    fork_name = f"{settings.github_username}/{repo_name}"
    try:
        result = await github.run_gh(["repo", "delete", fork_name, "--yes"])
        if result.success:
            logger.info("fork_deleted", fork=fork_name, upstream=repo)
        else:
            # Fork may already be gone or name collision — not an error
            logger.debug("fork_delete_skipped", fork=fork_name, stderr=result.stderr[:100])
    except Exception as exc:
        logger.debug("fork_delete_error", fork=fork_name, error=str(exc))


async def _phase_iterate(
    state: BotState,
    github: GitHubCLI,
    db: MemoryDB,
    gateway_protocol: object,
) -> None:
    """Check open PRs for new feedback and apply patches if needed."""
    logger.info("phase_iterate_start")
    open_prs = await state.get_open_prs()
    if not open_prs:
        logger.debug("phase_iterate_skip", reason="no open PRs")
        return

    updates = await check_prs(open_prs, github, db)
    logger.info("phase_iterate_updates", count=len(updates))

    for update in updates:
        pr = update.pr

        # Handle terminal states
        if update.is_merged:
            await state.remove_pr(pr.pr_number)
            await db.record_outcome(
                repo=pr.repo,
                issue_number=pr.issue_number,
                pr_number=pr.pr_number,
                outcome="merged",
                failure_reason=None,
                tokens_used=0,
            )
            logger.info("pr_merged", repo=pr.repo, pr=pr.pr_number)
            # Positive learning: extract what worked for this repo
            try:
                from osbot.learning.lessons import on_merge

                await on_merge(pr.repo, pr.issue_number, db)
            except Exception as exc:
                logger.warning("on_merge_learning_failed", repo=pr.repo, error=str(exc))
            await _cleanup_fork(pr.repo, github)
            continue

        if update.is_closed:
            await state.remove_pr(pr.pr_number)
            await db.record_outcome(
                repo=pr.repo,
                issue_number=pr.issue_number,
                pr_number=pr.pr_number,
                outcome="rejected",
                failure_reason="closed by maintainer",
                tokens_used=0,
            )
            logger.info("pr_closed", repo=pr.repo, pr=pr.pr_number)
            await _cleanup_fork(pr.repo, github)
            continue

        # Process new feedback
        if update.has_new_feedback:
            all_comments = update.new_comments + [
                c for r in update.new_reviews for c in r.get("comments", {}).get("nodes", [])
            ]
            # Add review bodies as top-level comments too
            for review in update.new_reviews:
                if review.get("body", "").strip():
                    all_comments.append(review)

            # Defense-in-depth: exclude the bot's own comments from the
            # aggregated list to prevent self-referencing feedback loops.
            bot_login = settings.github_username.lower()
            if bot_login:
                all_comments = [
                    c for c in all_comments if (c.get("author") or {}).get("login", "").lower() != bot_login
                ]

            if all_comments:
                feedback = await read_feedback(pr, all_comments, gateway_protocol)  # type: ignore[arg-type]

                # Check for blockers that need human intervention
                from osbot.comms.blocker import notify_blocker

                comment_text = " ".join((c.get("body") or "").lower() for c in all_comments)
                pr_url = f"https://github.com/{pr.repo}/pull/{pr.pr_number}"

                if any(kw in comment_text for kw in ("screenshot", "visual", "screen shot", "screencast")):
                    await notify_blocker(
                        "screenshot_requested",
                        repo=pr.repo,
                        pr_number=str(pr.pr_number),
                        pr_url=pr_url,
                    )

                if feedback.feedback_type == FeedbackType.QUESTION and not feedback.should_patch:
                    # Question we may not be able to answer well — notify owner
                    question_preview = comment_text[:200]
                    await notify_blocker(
                        "maintainer_question",
                        repo=pr.repo,
                        pr_number=str(pr.pr_number),
                        pr_url=pr_url,
                        question=question_preview,
                    )

                if feedback.should_patch:
                    import tempfile
                    from pathlib import Path

                    workspace_dir = Path(settings.workspaces_dir)
                    workspace_dir.mkdir(parents=True, exist_ok=True)
                    workspace = tempfile.mkdtemp(
                        prefix=f"osbot-iterate-{pr.repo.replace('/', '-')}-{pr.pr_number}-",
                        dir=str(workspace_dir),
                    )

                    # Clone the bot's fork directly on the PR branch (no default-branch detour).
                    # --no-single-branch lets us fetch other branches after cloning.
                    fork_url = f"https://github.com/{settings.github_username}/{pr.repo.split('/')[-1]}.git"
                    clone_result = await github.run_git(
                        [
                            "clone",
                            "--depth=50",
                            "--no-single-branch",
                            "-b",
                            pr.branch,
                            fork_url,
                            workspace,
                        ]
                    )
                    if not clone_result.success:
                        # Fallback: clone upstream then fetch the branch from fork
                        clone_result = await github.run_git(
                            [
                                "clone",
                                "--depth=50",
                                "--no-single-branch",
                                f"https://github.com/{pr.repo}.git",
                                workspace,
                            ]
                        )
                        if clone_result.success:
                            await github.run_git(["remote", "add", "fork", fork_url], cwd=workspace)
                            await github.run_git(["fetch", "fork", pr.branch], cwd=workspace)
                    if clone_result.success:
                        await apply_patch(pr, feedback, workspace, gateway_protocol, github)  # type: ignore[arg-type]

                        # Record feedback for learning
                        from osbot.learning.lessons import on_feedback

                        maintainer = all_comments[0].get("author", {}).get("login", "") if all_comments else ""
                        await on_feedback(pr.repo, maintainer, feedback.feedback_type.value, "", db)
                    else:
                        logger.warning("iterate_clone_failed", repo=pr.repo, pr=pr.pr_number)

                    # Cleanup workspace
                    import shutil

                    shutil.rmtree(workspace, ignore_errors=True)

        # Always update last_checked_at so the monitor doesn't re-trigger on the
        # same comments next cycle.  Uses dataclass_replace since OpenPR is frozen.
        updated_pr = dataclass_replace(pr, last_checked_at=datetime.now(UTC).isoformat())
        await state.add_open_pr(updated_pr)


_STALE_PR_DAYS = 14


async def _phase_monitor(
    state: BotState,
    github: GitHubCLI,
    db: MemoryDB,
) -> None:
    """Lightweight: check PR statuses, auto-close stale PRs, update state.

    No Claude calls.  A PR is considered stale when it has been open for
    ``_STALE_PR_DAYS`` days with no maintainer response (only bot comments).
    Stale PRs are closed with a polite comment to protect the bot's
    reputation.
    """
    logger.info("phase_monitor_start")
    open_prs = await state.get_open_prs()
    if not open_prs:
        return

    now = datetime.now(UTC)

    for pr in open_prs:
        try:
            result = await github.run_gh(
                [
                    "pr",
                    "view",
                    str(pr.pr_number),
                    "--repo",
                    pr.repo,
                    "--json",
                    "state,mergeable",
                ]
            )
            if result.success:
                data = json.loads(result.stdout)
                status = data.get("state", "").upper()
                if status == "MERGED":
                    await state.remove_pr(pr.pr_number)
                    logger.info("monitor_pr_merged", repo=pr.repo, pr=pr.pr_number)
                    continue
                elif status == "CLOSED":
                    await state.remove_pr(pr.pr_number)
                    logger.info("monitor_pr_closed", repo=pr.repo, pr=pr.pr_number)
                    continue

            # -- Stale PR auto-close --
            # If the PR has been open > _STALE_PR_DAYS with no maintainer
            # response, close it to prevent reputation damage.
            if pr.submitted_at:
                try:
                    submitted = datetime.fromisoformat(pr.submitted_at)
                    age_days = (now - submitted).total_seconds() / 86400
                except (ValueError, TypeError):
                    age_days = 0.0

                if age_days >= _STALE_PR_DAYS:
                    is_stale = await _is_pr_stale(pr, github)
                    if is_stale:
                        await _close_stale_pr(pr, state, github, db)
                        continue

            # -- CLA bot comment detection --
            # Scan comments for CLA bot activity and notify owner if found.
            try:
                await _check_cla_comments(pr, github, db)
            except Exception as cla_exc:
                logger.debug("cla_comment_check_error", repo=pr.repo, pr=pr.pr_number, error=str(cla_exc))

        except Exception as exc:
            logger.debug("monitor_pr_check_error", repo=pr.repo, pr=pr.pr_number, error=str(exc))

    logger.debug("phase_monitor_done", prs_checked=len(open_prs))


# CLA bot usernames and keywords to detect CLA-related comments.
_CLA_BOT_USERNAMES: frozenset[str] = frozenset(
    {
        "claassistant",
        "cla-assistant",
        "googlebot",
        "google-cla",
        "mslobot",
        "microsoft-cla",
        "linux-foundation-easycla",
        "easycla",
        "cla-bot",
        "allcontributors",
        "cla-checker",
        "apache-cla",
        "salesforce-cla",
    }
)

_CLA_COMMENT_PATTERNS: list[str] = [
    "cla",
    "contributor license agreement",
    "cla-assistant",
    "salesforce-cla",
    "sign the cla",
    "sign our cla",
    "please sign",
    "cla signature",
]


async def _check_cla_comments(
    pr: OpenPR,
    github: GitHubCLI,
    db: MemoryDB,
) -> None:
    """Scan PR comments for CLA bot activity and notify the owner.

    Only notifies once per PR (tracked via repo_facts to avoid spam).
    """
    # Check if we already notified for this PR
    fact_key = f"cla_notified_pr_{pr.pr_number}"
    already_notified = await db.get_repo_fact(pr.repo, fact_key)
    if already_notified:
        return

    result = await github.run_gh(
        [
            "pr",
            "view",
            str(pr.pr_number),
            "--repo",
            pr.repo,
            "--json",
            "comments",
        ]
    )
    if not result.success:
        return

    data = json.loads(result.stdout)
    comments = data.get("comments", [])

    cla_detected = False
    cla_url = ""

    for comment in comments:
        author = (comment.get("author") or {}).get("login", "").lower()
        body = (comment.get("body") or "").lower()

        # Check if comment is from a known CLA bot
        if author in _CLA_BOT_USERNAMES:
            cla_detected = True
            # Try to extract CLA signing URL from the comment body
            url_match = re.search(r'https?://[^\s\)>"]+cla[^\s\)>"]*', body, re.IGNORECASE)
            if url_match:
                cla_url = url_match.group(0)
            break

        # Check comment body for CLA keywords
        for pattern in _CLA_COMMENT_PATTERNS:
            if pattern in body:
                cla_detected = True
                url_match = re.search(r'https?://[^\s\)>"]+', body)
                if url_match:
                    cla_url = url_match.group(0)
                break
        if cla_detected:
            break

    if not cla_detected:
        return

    # Mark as notified BEFORE sending to prevent duplicate sends on retry
    await db.set_repo_fact(pr.repo, fact_key, "true", source="cla_monitor")

    # Send email notification
    from osbot.comms.email import send_email

    pr_url = f"https://github.com/{pr.repo}/pull/{pr.pr_number}"
    cla_info = f"\nCLA signing URL: {cla_url}" if cla_url else ""

    await send_email(
        to=settings.alert_email,
        subject=f"[osbot] CLA signing required for {pr.repo}#{pr.pr_number}",
        body=(
            f"A CLA bot has commented on your PR requesting a signature.\n\n"
            f"Repository: {pr.repo}\n"
            f"PR: {pr_url}\n"
            f"Issue: #{pr.issue_number}\n"
            f"{cla_info}\n\n"
            f"Please sign the CLA so the PR can be reviewed and merged.\n"
            f"The PR has NOT been closed -- it is waiting for your signature."
        ),
        severity="warning",
    )
    logger.info(
        "cla_bot_notification_sent",
        repo=pr.repo,
        pr=pr.pr_number,
        cla_url=cla_url,
        email=settings.alert_email,
    )


async def _is_pr_stale(
    pr: OpenPR,
    github: GitHubCLI,
) -> bool:
    """Return ``True`` if the PR has only bot comments (no maintainer response).

    Fetches PR comments and reviews and checks whether any non-bot author
    has commented.  If only the bot (or no one) has responded, the PR is
    considered stale.
    """
    bot_username = settings.github_username
    try:
        result = await github.run_gh(
            [
                "pr",
                "view",
                str(pr.pr_number),
                "--repo",
                pr.repo,
                "--json",
                "comments",
            ]
        )
        if not result.success:
            return False

        data = json.loads(result.stdout)
        comments = data.get("comments", [])

        for comment in comments:
            author = (comment.get("author") or {}).get("login", "")
            if author and author.lower() != bot_username.lower():
                # A non-bot user commented -- not stale
                return False

        # Also check reviews
        review_result = await github.run_gh(
            [
                "pr",
                "view",
                str(pr.pr_number),
                "--repo",
                pr.repo,
                "--json",
                "reviews",
            ]
        )
        if review_result.success:
            review_data = json.loads(review_result.stdout)
            reviews = review_data.get("reviews", [])
            for review in reviews:
                author = (review.get("author") or {}).get("login", "")
                if author and author.lower() != bot_username.lower():
                    return False

        # No non-bot comments or reviews found
        return True
    except Exception as exc:
        logger.debug("stale_check_error", repo=pr.repo, pr=pr.pr_number, error=str(exc))
        return False


async def _close_stale_pr(
    pr: OpenPR,
    state: BotState,
    github: GitHubCLI,
    db: MemoryDB,
) -> None:
    """Post a polite closing comment and close the stale PR.

    Records the outcome as ``Outcome.IGNORED`` and removes from tracked state.
    """
    close_comment = "Closing this PR as it hasn't received maintainer review. Happy to resubmit if there's interest."

    # Post closing comment
    try:
        await github.run_gh(
            [
                "pr",
                "comment",
                str(pr.pr_number),
                "--repo",
                pr.repo,
                "--body",
                close_comment,
            ]
        )
    except Exception as exc:
        logger.debug("stale_close_comment_error", repo=pr.repo, pr=pr.pr_number, error=str(exc))

    # Close the PR
    try:
        close_result = await github.run_gh(
            [
                "pr",
                "close",
                str(pr.pr_number),
                "--repo",
                pr.repo,
            ]
        )
        if close_result.success:
            logger.info("stale_pr_closed", repo=pr.repo, pr=pr.pr_number)
        else:
            logger.warning(
                "stale_pr_close_failed",
                repo=pr.repo,
                pr=pr.pr_number,
                error=getattr(close_result, "stderr", "")[:200],
            )
    except Exception as exc:
        logger.warning("stale_pr_close_error", repo=pr.repo, pr=pr.pr_number, error=str(exc))

    # Remove from state
    await state.remove_pr(pr.pr_number)

    # Record outcome as ignored
    try:
        await db.record_outcome(
            repo=pr.repo,
            issue_number=pr.issue_number,
            pr_number=pr.pr_number,
            outcome=Outcome.IGNORED,
            failure_reason="stale: no maintainer response after 14 days",
            tokens_used=0,
        )
    except Exception as exc:
        logger.debug("stale_outcome_record_error", repo=pr.repo, pr=pr.pr_number, error=str(exc))


async def _phase_review(
    state: BotState,
    github: GitHubCLI,
    db: MemoryDB,
    gateway: object,
) -> None:
    """Review others' open PRs to build reputation."""
    # Guard: only review when we have open PRs (reputation investment)
    open_prs = await state.get_open_prs()
    if not open_prs:
        logger.debug("phase_review_skip", reason="no open PRs")
        return

    from osbot.orchestrator.review import run_review_phase

    try:
        await run_review_phase(github, db, gateway)  # type: ignore[arg-type]
    except Exception as exc:
        logger.error("phase_review_error", error=str(exc), exc_info=True)


async def _phase_engage(
    state: BotState,
    github: GitHubCLI,
    db: MemoryDB,
    gateway: object,
) -> None:
    """Comment helpfully on issues before contributing."""
    # Guard: only engage when we have open PRs (reputation investment)
    open_prs = await state.get_open_prs()
    if not open_prs:
        logger.debug("phase_engage_skip", reason="no open PRs")
        return

    from osbot.orchestrator.engage import run_engage_phase

    try:
        await run_engage_phase(state, github, db, gateway)  # type: ignore[arg-type]
    except Exception as exc:
        logger.error("phase_engage_error", error=str(exc), exc_info=True)


async def _phase_notify(
    github: GitHubCLI,
    gateway: object,
) -> None:
    """Respond to @mentions in GitHub notifications."""
    from osbot.orchestrator.notify import run_notify_phase

    try:
        await run_notify_phase(github, gateway)  # type: ignore[arg-type]
    except Exception as exc:
        logger.error("phase_notify_error", error=str(exc), exc_info=True)


# ---------------------------------------------------------------------------
# Correction applicator
# ---------------------------------------------------------------------------


async def _apply_corrections(
    corrections: list[Correction],
    state: BotState,
    db: MemoryDB,
    trace_writer: TraceWriter,
) -> None:
    """Apply corrections from fast_diagnostic to the running system."""
    for c in corrections:
        try:
            await trace_writer.write_correction(c)
        except Exception:
            pass  # Best-effort persistence

        if c.type == "halt":
            logger.error("correction_halt", reason=c.reason, message=c.message)
            # A halt correction is critical -- the caller should check for this

        elif c.type == "force_discovery":
            logger.info("correction_force_discovery", reason=c.reason)
            # Discovery will be triggered next cycle by resetting the timer
            # (handled by the caller)


# ---------------------------------------------------------------------------
# Graceful shutdown helper
# ---------------------------------------------------------------------------


async def _shutdown(
    gateway: object,
    state: BotState,
    db: MemoryDB,
) -> None:
    """Flush state, close connections, stop the gateway consumer."""
    logger.info("shutdown_start")

    # Stop the gateway's background consumer
    from osbot.gateway.claude import ClaudeGateway

    if isinstance(gateway, ClaudeGateway):
        try:
            await gateway.shutdown()
            logger.info("shutdown_gateway_stopped")
        except Exception as exc:
            logger.warning("shutdown_gateway_error", error=str(exc))

    # Flush state.json to disk
    try:
        async with state._lock:
            await state._flush()
        logger.info("shutdown_state_flushed")
    except Exception as exc:
        logger.warning("shutdown_state_flush_error", error=str(exc))

    # Close the SQLite connection
    try:
        await db.close()
        logger.info("shutdown_db_closed")
    except Exception as exc:
        logger.warning("shutdown_db_close_error", error=str(exc))

    logger.info("shutdown_complete")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def run() -> None:
    """Entry point for the orchestrator.

    Creates services, runs the startup health check, then enters the
    main cycle loop.  On SIGTERM or SIGINT the loop exits cleanly:
    state is flushed, memory.db is closed, and the gateway consumer is
    stopped.

    Designed to be called from ``__main__.py``.
    """
    from osbot.orchestrator.health import startup_check

    # -- Create services --
    github = GitHubCLI()
    db = MemoryDB()
    await db.connect(settings.db_path)
    state = BotState()
    await state.load()
    trace_writer = TraceWriter(settings.traces_path, settings.corrections_path)
    balancer = Balancer(db)

    # -- Create Claude gateway with token tracking callback --
    from osbot.gateway.claude import ClaudeGateway

    gateway = ClaudeGateway(
        on_call_complete=lambda tokens, model: balancer._decay.record(tokens, model),
    )
    logger.info("gateway_created", gateway_type=type(gateway).__name__)

    # -- Register signal handlers for graceful shutdown --
    shutdown_requested = asyncio.Event()

    def _handle_signal(sig: signal.Signals) -> None:
        logger.info("shutdown_signal_received", signal=sig.name)
        shutdown_requested.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: _handle_signal(s))

    # -- Startup health check --
    logger.info("orchestrator_starting")
    healthy = await startup_check(github, db=db)
    if not healthy:
        logger.error("orchestrator_halted", reason="health_check_failed")
        # Notify owner instead of just dying
        from osbot.comms.blocker import notify_blocker

        await notify_blocker("health_failed", failed_checks="see logs for details")
        # Don't return — retry in a loop. Health issues may be transient.
        logger.info("health_check_retry_loop", msg="Will retry every 60s")
        while not shutdown_requested.is_set():
            try:
                await asyncio.wait_for(shutdown_requested.wait(), timeout=60)
                break  # Shutdown requested during wait
            except TimeoutError:
                pass
            if shutdown_requested.is_set():
                break
            healthy = await startup_check(github, db=db)
            if healthy:
                logger.info("health_check_recovered")
                break
        if not healthy:
            await _shutdown(gateway, state, db)
            return

    # Clear zombie active work from previous run
    await state.clear_active()

    # Session 3: Seed default prompt variants if table is empty
    try:
        from osbot.learning.prompt_variants import seed_variants

        await seed_variants(db)
    except Exception as exc:
        logger.warning("seed_variants_failed", error=str(exc))

    logger.info("orchestrator_ready", cycle_sec=settings.cycle_interval_sec)

    # -- Timing trackers for phase cadence --
    last_discover = datetime.min.replace(tzinfo=UTC)
    last_review = datetime.min.replace(tzinfo=UTC)
    last_engage = datetime.min.replace(tzinfo=UTC)
    last_learn = datetime.min.replace(tzinfo=UTC)

    # -- Trace buffer for fast_diagnostic --
    recent_traces: list[Trace] = []

    cycle_count = 0
    force_discover = False

    # Emit an initial heartbeat so the Docker liveness probe doesn't fail
    # during the first cycle (which can take minutes).
    write_heartbeat(Path(settings.state_dir), cycle_count)

    # -- Main loop (exits on shutdown signal or halt correction) --
    while not shutdown_requested.is_set():
        cycle_count += 1
        now = datetime.now(UTC)

        # -- Token management probe --
        try:
            await balancer.update()
        except Exception as exc:
            logger.warning("balancer_update_error", cycle=cycle_count, error=str(exc))

        # -- OAuth token expiry check (every cycle, before contribute) --
        try:
            from osbot.tokens.probe import check_token_expiry

            await check_token_expiry()
        except Exception as exc:
            logger.debug("token_expiry_check_error", cycle=cycle_count, error=str(exc))

        workers = balancer.current_workers

        logger.info(
            "cycle_start",
            cycle=cycle_count,
            workers=workers,
            prefer_sonnet=balancer.should_prefer_sonnet,
        )

        try:
            async with asyncio.TaskGroup() as tg:
                # Monitor runs every cycle (no Claude)
                tg.create_task(_phase_monitor(state, github, db))

                # Notify runs every cycle (inline, fast)
                tg.create_task(_phase_notify(github, gateway))

                # Contribute runs every cycle
                tg.create_task(
                    _phase_contribute(
                        workers,
                        state,
                        gateway,
                        github,
                        db,
                        balancer,
                        trace_writer,
                        recent_traces,
                    )
                )

                # Iterate runs every cycle
                tg.create_task(_phase_iterate(state, github, db, gateway))

                # Discovery on its own timer
                discover_elapsed = (now - last_discover).total_seconds()
                if discover_elapsed >= settings.discover_interval_sec or force_discover:
                    tg.create_task(_phase_discover(github, db, state))
                    last_discover = now
                    force_discover = False

                # Review on its own timer
                review_elapsed = (now - last_review).total_seconds()
                if review_elapsed >= settings.review_interval_sec:
                    tg.create_task(_phase_review(state, github, db, gateway))
                    last_review = now

                # Engage on its own timer
                engage_elapsed = (now - last_engage).total_seconds()
                if engage_elapsed >= settings.engage_interval_sec:
                    tg.create_task(_phase_engage(state, github, db, gateway))
                    last_engage = now

        except* Exception as eg:  # noqa: F841
            # TaskGroup wraps exceptions in ExceptionGroup.
            for exc in eg.exceptions:
                logger.error("cycle_phase_error", cycle=cycle_count, error=str(exc))

        # -- Fast diagnostic (inline, < 1 second) --
        try:
            corrections = await fast_diagnostic(recent_traces, db=db)
            if corrections:
                logger.info(
                    "diagnostics_corrections",
                    cycle=cycle_count,
                    count=len(corrections),
                    types=[c.type for c in corrections],
                )
                await _apply_corrections(corrections, state, db, trace_writer)

                # Check for halt
                if any(c.type == "halt" for c in corrections):
                    logger.error("orchestrator_halted_by_diagnostic", cycle=cycle_count)
                    break

                # Check for forced discovery
                if any(c.type == "force_discovery" for c in corrections):
                    force_discover = True

        except Exception as exc:
            logger.error("diagnostics_error", cycle=cycle_count, error=str(exc))

        # -- Learn phase: deep diagnostic every 12 hours --
        learn_elapsed = (now - last_learn).total_seconds()
        if learn_elapsed >= settings.learn_interval_sec:
            try:
                logger.info("phase_learn_start")
                learn_corrections = await deep_diagnostic(db)
                if learn_corrections:
                    logger.info(
                        "learn_corrections",
                        cycle=cycle_count,
                        count=len(learn_corrections),
                        types=[c.type for c in learn_corrections],
                    )
                    await _apply_corrections(learn_corrections, state, db, trace_writer)
                else:
                    logger.info("phase_learn_done", corrections=0)

                # Trending repo discovery: find high-velocity repos and
                # seed them into the signal pipeline for the next discovery
                # cycle to pick up.
                try:
                    from osbot.discovery.trending import find_trending_repos

                    trending = await find_trending_repos(github, db)
                    if trending:
                        logger.info("trending_repos_seeded", count=len(trending))
                except Exception as trend_exc:
                    logger.warning("trending_discovery_error", error=str(trend_exc))

                # A-Mem consolidation: synthesize cross-repo meta-lessons
                # from outcome patterns. Zero Claude calls.
                try:
                    from osbot.learning.lessons import consolidate_meta_lessons

                    meta_count = await consolidate_meta_lessons(db)
                    if meta_count:
                        logger.info("meta_lessons_consolidated", count=meta_count)
                except Exception as meta_exc:
                    logger.warning("meta_lessons_consolidation_error", error=str(meta_exc))

                last_learn = now
            except Exception as exc:
                logger.error("phase_learn_error", cycle=cycle_count, error=str(exc))

        logger.info("cycle_end", cycle=cycle_count)

        # Liveness heartbeat — healthcheck reads this to detect crash-loops.
        write_heartbeat(Path(settings.state_dir), cycle_count)

        # -- Sleep until next cycle, but wake early on shutdown signal --
        try:
            await asyncio.wait_for(
                shutdown_requested.wait(),
                timeout=settings.cycle_interval_sec,
            )
            # If we get here, shutdown was requested during the sleep
        except TimeoutError:
            # Normal case: the sleep period elapsed without a shutdown signal
            pass

    # -- Graceful shutdown --
    await _shutdown(gateway, state, db)
