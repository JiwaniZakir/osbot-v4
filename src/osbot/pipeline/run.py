"""Pipeline orchestrator -- ties all pipeline stages together.

``run_pipeline`` is the single entry point: issue in, PipelineResult out.
Flow: preflight -> [assignment] -> workspace setup -> implement ->
quality gates -> critic -> PR write -> submit.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import time
from pathlib import Path

from osbot.config import settings
from osbot.learning.lessons import generate_reflection
from osbot.log import get_logger
from osbot.pipeline.assignment import (
    AWAITING,
    REJECTED,
    check_assignment,
    request_assignment,
)
from osbot.pipeline.critic import review
from osbot.pipeline.implementer import implement
from osbot.pipeline.pr_writer import write_pr
from osbot.pipeline.preflight import preflight
from osbot.pipeline.quality import run_gates
from osbot.pipeline.submitter import submit
from osbot.safety.circuit_breaker import record_failure, record_timeout
from osbot.types import (
    BalancerProtocol,
    ClaudeGatewayProtocol,
    CriticVerdict,
    GitHubCLIProtocol,
    MemoryDBProtocol,
    Outcome,
    PipelineResult,
    ScoredIssue,
)

logger = get_logger(__name__)


async def run_pipeline(
    issue: ScoredIssue,
    gateway: ClaudeGatewayProtocol,
    github: GitHubCLIProtocol,
    db: MemoryDBProtocol,
    balancer: BalancerProtocol,
) -> PipelineResult:
    """Execute the full contribution pipeline for a single issue.

    Flow:
        1. Preflight (free) -- repo bans, domain, duplicates, issue state
        2. Assignment (free) -- claim comment if needed, poll status
        3. Workspace setup (free) -- fork, clone, branch
        4. Implement (Claude call #1) -- minimal fix
        5. Quality gates (free) -- diff size, lint, tests
        6. Critic (Claude call #2) -- MAR-style review, HARD GATE
        7. PR write (Claude call #3) -- description generation
        8. Submit (free) -- push and ``gh pr create``

    Records outcome to ``db`` on completion.  Cleans up workspace.

    Args:
        issue: The scored issue to fix.
        gateway: Claude gateway for Agent SDK calls.
        github: GitHub CLI wrapper.
        db: Memory DB for state and outcome recording.
        balancer: Token balancer (controls model selection).

    Returns:
        PipelineResult with outcome, PR info, and metrics.
    """
    start = time.monotonic()
    tokens_total = 0
    claude_calls = 0
    workspace: str | None = None
    # Step-level checkpoints (PRM): accumulate pass/fail per phase
    checkpoints: dict[str, bool] = {
        "preflight_passed": False,
        "implementation_completed": False,
        "tests_pass": False,
        "style_matches": False,
        "diff_size_ok": False,
        "scope_correct": False,
        "critic_approves": False,
        "pr_submitted": False,
    }
    # Session 3: track which prompt variants were used for outcome feedback
    variant_info: dict[str, str] = {}
    # Session 4: track whether we used downscope mode (ECHO)
    used_downscope = False

    try:
        # ---------------------------------------------------------------
        # 1. Preflight
        # ---------------------------------------------------------------
        ok, reason, preflight_meta = await preflight(issue, db, github)
        if not ok:
            return await _finalize(
                issue,
                db,
                Outcome.REJECTED,
                start,
                tokens_total,
                claude_calls,
                failure_reason=reason,
                failure_phase="preflight",
                checkpoints=checkpoints,
                variant_info=variant_info,
            )

        checkpoints["preflight_passed"] = True

        # ---------------------------------------------------------------
        # 2. Assignment flow (if required)
        # ---------------------------------------------------------------
        if issue.requires_assignment:
            status = await check_assignment(issue, db, github)
            if status == AWAITING:
                # Post claim comment and return -- we'll be polled later
                claimed = await request_assignment(issue, github, db)
                if not claimed:
                    return await _finalize(
                        issue,
                        db,
                        Outcome.REJECTED,
                        start,
                        tokens_total,
                        claude_calls,
                        failure_reason="failed to post claim comment",
                        failure_phase="assignment",
                        checkpoints=checkpoints,
                        variant_info=variant_info,
                    )
                # Return a special "not done yet" result
                return await _finalize(
                    issue,
                    db,
                    Outcome.STUCK,
                    start,
                    tokens_total,
                    claude_calls,
                    failure_reason="awaiting_assignment",
                    failure_phase="assignment",
                    checkpoints=checkpoints,
                    variant_info=variant_info,
                )
            if status == REJECTED:
                return await _finalize(
                    issue,
                    db,
                    Outcome.REJECTED,
                    start,
                    tokens_total,
                    claude_calls,
                    failure_reason="assignment rejected or timed out",
                    failure_phase="assignment",
                    checkpoints=checkpoints,
                    variant_info=variant_info,
                )

        # ---------------------------------------------------------------
        # 3. Workspace setup: clone repo to temp directory
        # ---------------------------------------------------------------
        workspace = await _setup_workspace(issue, github)
        if workspace is None:
            return await _finalize(
                issue,
                db,
                Outcome.REJECTED,
                start,
                tokens_total,
                claude_calls,
                failure_reason="workspace setup failed (clone error)",
                failure_phase="workspace",
                checkpoints=checkpoints,
                variant_info=variant_info,
            )

        # ---------------------------------------------------------------
        # 3b. Two-pass scope analysis (cheap Claude call, ~30s)
        # Asks Claude to identify the target file/function before full implementation.
        # The scope hint is injected as a constraint to reduce scope creep.
        # ---------------------------------------------------------------
        scope_hint = ""
        try:
            from osbot.pipeline.scoper import get_scope_hint

            scope_hint = await get_scope_hint(issue, gateway)
            claude_calls += 1
        except Exception:
            pass  # Scoper is non-critical -- proceed without hint

        # ---------------------------------------------------------------
        # 4. Implement (Claude call #1)
        # ---------------------------------------------------------------
        impl_result, variant_info = await implement(issue, workspace, gateway, db, scope_hint=scope_hint)
        tokens_total += impl_result.tokens_used
        claude_calls += 1

        if not impl_result.success:
            # Timeout = hard reject (no commit possible).
            # Other errors (e.g., test suite failure AFTER a valid commit) may
            # still have produced a diff -- check before giving up.
            if impl_result.error == "timeout":
                return await _finalize(
                    issue,
                    db,
                    Outcome.REJECTED,
                    start,
                    tokens_total,
                    claude_calls,
                    failure_reason="timeout",
                    failure_phase="implement",
                    checkpoints=checkpoints,
                    variant_info=variant_info,
                )
            # Non-timeout failure: attempt to salvage by checking the diff.
            # If there's no diff either, _finalize below will catch it.
            logger.info(
                "implementer_non_timeout_failure",
                repo=issue.repo,
                issue=issue.number,
                error=impl_result.error,
                msg="checking diff before giving up",
            )

        checkpoints["implementation_completed"] = True

        # ---------------------------------------------------------------
        # 5. Quality gates (free)
        # ---------------------------------------------------------------
        gate_result = await run_gates(workspace, github)

        # Record individual gate checkpoints regardless of overall pass/fail
        checkpoints["tests_pass"] = gate_result.tests_passed
        checkpoints["style_matches"] = gate_result.lint_passed
        checkpoints["diff_size_ok"] = gate_result.diff_lines <= settings.max_diff_lines

        if not gate_result.passed:
            reason = "; ".join(gate_result.failures)
            return await _finalize(
                issue,
                db,
                Outcome.REJECTED,
                start,
                tokens_total,
                claude_calls,
                failure_reason=f"quality gates: {reason}",
                failure_phase="quality_gates",
                checkpoints=checkpoints,
                variant_info=variant_info,
            )

        # Get the diff for critic and PR writer
        diff_result = await github.run_git(["diff", "HEAD~1"], cwd=workspace)
        diff = diff_result.stdout if diff_result.success else ""

        if not diff.strip():
            # Diagnose WHY the diff is empty based on implementation behavior.
            tool_call_count = len(impl_result.tool_trace)
            if tool_call_count > 50:
                empty_reason = (
                    f"empty diff: implementation made {tool_call_count} tool calls "
                    f"but never committed. Issue may be too investigative or complex "
                    f"for a minimal fix."
                )
            elif tool_call_count < 10:
                empty_reason = (
                    f"empty diff: implementation made only {tool_call_count} tool calls "
                    f"and gave up quickly. Issue may be unclear or lack actionable detail."
                )
            else:
                empty_reason = (
                    f"empty diff: implementation made {tool_call_count} tool calls "
                    f"but did not produce a commit. Issue may require deeper investigation "
                    f"than a minimal fix allows."
                )
            return await _finalize(
                issue,
                db,
                Outcome.REJECTED,
                start,
                tokens_total,
                claude_calls,
                failure_reason=empty_reason,
                failure_phase="quality_gates",
                checkpoints=checkpoints,
                variant_info=variant_info,
            )

        # ---------------------------------------------------------------
        # 5b. Pre-critic scope check (free, no Claude call)
        # Count files in diff. If > max_files_changed, pre-reject before
        # spending critic tokens — the critic would reject anyway.
        # ---------------------------------------------------------------
        diff_files = {
            line[4:].split("\t")[0] for line in diff.splitlines() if line.startswith("--- ") or line.startswith("+++ ")
        }
        # Remove /dev/null (new/deleted files show up as this in unified diffs)
        diff_files.discard("/dev/null")
        # Strip the a/ and b/ prefixes git adds
        actual_files = {f.lstrip("ab/").split(" ")[0] for f in diff_files if f not in ("/dev/null",)}
        changed_file_count = len(actual_files) // 2 if actual_files else 0  # --- and +++ per file

        if changed_file_count > settings.max_files_changed:
            scope_reason = (
                f"pre-critic scope rejection: diff touches {changed_file_count} files "
                f"(limit {settings.max_files_changed}). Too broad for a minimal fix."
            )
            logger.info(
                "pre_critic_scope_reject",
                repo=issue.repo,
                issue=issue.number,
                files=changed_file_count,
                limit=settings.max_files_changed,
            )
            checkpoints["scope_correct"] = False
            # Real Reflexion: Claude diagnoses its own scope error
            _real_reflexion_done = False
            try:
                from osbot.learning.lessons import generate_real_reflection

                await generate_real_reflection(
                    repo=issue.repo,
                    issue_number=issue.number,
                    title=issue.title,
                    labels=list(issue.labels),
                    failure_phase="quality_gates",
                    failure_reason=scope_reason,
                    diff=diff,
                    gateway=gateway,
                    db=db,
                )
                _real_reflexion_done = True
            except Exception:
                pass  # Reflexion is non-critical
            return await _finalize(
                issue,
                db,
                Outcome.REJECTED,
                start,
                tokens_total,
                claude_calls,
                failure_reason=scope_reason,
                failure_phase="quality_gates",
                checkpoints=checkpoints,
                variant_info=variant_info,
                reflection_done=_real_reflexion_done,
            )
        checkpoints["scope_correct"] = True

        # ---------------------------------------------------------------
        # 6. Critic (Claude call #2) -- HARD GATE with soft retry + ECHO
        # ---------------------------------------------------------------
        # v3 had an iteration loop that could accept low-quality fixes.
        # v4 uses a hard gate BUT allows ONE retry if the critic has low
        # confidence and the issues are minor (scope/style, not correctness).
        # This is a compromise: we don't submit bad work, but we don't
        # permanently abandon fixable implementations either.
        #
        # Session 4 (ECHO): If the rejection mentions scope/complexity
        # AND we haven't already downscoped, retry with downscope_mode=True.
        # Max 1 downscope retry. Do NOT downscope on correctness rejections.
        critic_result = await review(
            issue,
            diff,
            impl_result.tool_trace,
            gateway,
            prefer_sonnet=balancer.should_prefer_sonnet,
        )
        claude_calls += 1

        if critic_result.verdict == CriticVerdict.REJECT:
            # Classify the rejection
            has_correctness_issue = any(
                "correct" in i.lower() or "bug" in i.lower() or "wrong" in i.lower() for i in critic_result.issues
            )
            has_scope_issue = any(
                "scope" in i.lower() or "complex" in i.lower() or "too many" in i.lower() or "too large" in i.lower()
                for i in critic_result.issues
            )

            # Standard soft retry conditions
            can_retry = (
                not has_correctness_issue
                and len(critic_result.issues) <= 2  # few issues = likely fixable
                and claude_calls < 3
            )

            if can_retry:
                # Session 4 (ECHO): If scope/complexity rejection AND not already
                # downscoped, use downscope_mode for the retry.
                should_downscope = has_scope_issue and not used_downscope

                if should_downscope:
                    logger.info(
                        "echo_downscope_retry",
                        repo=issue.repo,
                        issue=issue.number,
                        issues=critic_result.issues,
                        reasoning=critic_result.reasoning,
                    )
                    used_downscope = True
                    variant_info["downscoped"] = "True"
                    retry_result, _ = await implement(
                        issue,
                        workspace,
                        gateway,
                        db,
                        extra_context=(
                            f"Previous attempt was rejected for scope/complexity: "
                            f"{critic_result.reasoning}. "
                            f"Issues: {'; '.join(critic_result.issues)}."
                        ),
                        downscope_mode=True,
                    )
                else:
                    logger.info(
                        "critic_soft_retry",
                        repo=issue.repo,
                        issue=issue.number,
                        issues=critic_result.issues,
                        reasoning=critic_result.reasoning,
                    )
                    # Standard soft retry with critic feedback
                    retry_result, _ = await implement(
                        issue,
                        workspace,
                        gateway,
                        db,
                        extra_context=(
                            f"Previous attempt was rejected: {critic_result.reasoning}. "
                            f"Issues: {'; '.join(critic_result.issues)}. "
                            f"Fix ONLY these specific issues."
                        ),
                    )

                tokens_total += retry_result.tokens_used
                claude_calls += 1

                if retry_result.success:
                    # Re-run quality gates on the retry
                    retry_gates = await run_gates(workspace, github)
                    if retry_gates.passed:
                        # Get new diff and re-review
                        diff_result = await github.run_git(["diff", "HEAD~1"], cwd=workspace)
                        diff = diff_result.stdout if diff_result.success else ""
                        critic_result = await review(
                            issue,
                            diff,
                            retry_result.tool_trace,
                            gateway,
                            prefer_sonnet=balancer.should_prefer_sonnet,
                        )
                        claude_calls += 1

            # After retry (or no retry), hard gate applies
            if critic_result.verdict == CriticVerdict.REJECT:
                # Scope check: if critic issues mention "scope", mark scope_correct=False
                scope_issues = any("scope" in i.lower() for i in critic_result.issues)
                checkpoints["scope_correct"] = not scope_issues
                # Real Reflexion: Claude diagnoses its own critic rejection
                _real_reflexion_done = False
                try:
                    from osbot.learning.lessons import generate_real_reflection

                    await generate_real_reflection(
                        repo=issue.repo,
                        issue_number=issue.number,
                        title=issue.title,
                        labels=list(issue.labels),
                        failure_phase="critic",
                        failure_reason=critic_result.reasoning,
                        diff=diff,
                        gateway=gateway,
                        db=db,
                    )
                    _real_reflexion_done = True
                except Exception:
                    pass  # Reflexion is non-critical
                return await _finalize(
                    issue,
                    db,
                    Outcome.REJECTED,
                    start,
                    tokens_total,
                    claude_calls,
                    failure_reason=f"critic rejected: {critic_result.reasoning}",
                    failure_phase="critic",
                    checkpoints=checkpoints,
                    variant_info=variant_info,
                    reflection_done=_real_reflexion_done,
                )

        checkpoints["scope_correct"] = True
        checkpoints["critic_approves"] = True

        # ---------------------------------------------------------------
        # 7. PR description (Claude call #3)
        # ---------------------------------------------------------------
        pr_body = await write_pr(
            issue,
            diff,
            gateway,
            github=github,
            db=db,
            downscoped=used_downscope,
        )
        claude_calls += 1

        # ---------------------------------------------------------------
        # 8. Submit (fork, push, gh pr create)
        # ---------------------------------------------------------------
        try:
            pr_url, pr_number = await submit(issue, workspace, pr_body, github)
        except RuntimeError as exc:
            return await _finalize(
                issue,
                db,
                Outcome.REJECTED,
                start,
                tokens_total,
                claude_calls,
                failure_reason=f"submit failed: {exc}",
                failure_phase="submit",
                checkpoints=checkpoints,
                variant_info=variant_info,
            )

        checkpoints["pr_submitted"] = True

        # Skill library: record this successful diff for future few-shot injection
        try:
            from osbot.learning.skill_library import record_skill

            language = getattr(issue, "language", "") or ""
            await record_skill(issue.repo, issue.number, issue.title, list(issue.labels), language, diff, db)
        except Exception:
            pass  # Skill recording is non-critical

        # ---------------------------------------------------------------
        # 8b. CLA notification (if flagged during preflight)
        # ---------------------------------------------------------------
        if preflight_meta.needs_cla_signing and pr_number:
            try:
                from osbot.comms.email import send_email

                effective_pr_url = pr_url or f"https://github.com/{issue.repo}/pull/{pr_number}"
                await send_email(
                    to=settings.alert_email,
                    subject=f"[osbot] CLA signing required for {issue.repo}#{pr_number}",
                    body=(
                        f"A PR was submitted to a repo that requires CLA signing.\n\n"
                        f"Repository: {issue.repo}\n"
                        f"Issue: #{issue.number} - {issue.title}\n"
                        f"PR: {effective_pr_url}\n\n"
                        f"A CLA bot will likely comment on the PR requesting a signature.\n"
                        f"Please sign the CLA to allow the PR to be reviewed.\n\n"
                        f"The PR has NOT been closed -- it is waiting for you to sign."
                    ),
                    severity="warning",
                )
                logger.info(
                    "cla_notification_sent",
                    repo=issue.repo,
                    pr_number=pr_number,
                    email=settings.alert_email,
                )
            except Exception as exc:
                logger.warning("cla_notification_failed", error=str(exc))

        return await _finalize(
            issue,
            db,
            Outcome.SUBMITTED,
            start,
            tokens_total,
            claude_calls,
            pr_url=pr_url,
            pr_number=pr_number,
            checkpoints=checkpoints,
            variant_info=variant_info,
        )

    except Exception as exc:
        logger.error(
            "pipeline_exception",
            repo=issue.repo,
            issue=issue.number,
            error=str(exc),
            exc_info=True,
        )
        return await _finalize(
            issue,
            db,
            Outcome.REJECTED,
            start,
            tokens_total,
            claude_calls,
            failure_reason=f"unexpected error: {exc}",
            failure_phase="pipeline",
            checkpoints=checkpoints,
            variant_info=variant_info,
        )

    finally:
        # Clean up workspace
        if workspace is not None:
            with contextlib.suppress(Exception):
                shutil.rmtree(workspace, ignore_errors=True)


async def _setup_workspace(
    issue: ScoredIssue,
    github: GitHubCLIProtocol,
) -> str | None:
    """Clone the repo into a temporary workspace directory.

    Returns the workspace path, or None on failure.
    """
    workspace_dir = Path(settings.workspaces_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    workspace = tempfile.mkdtemp(
        prefix=f"osbot-{issue.repo.replace('/', '-')}-{issue.number}-",
        dir=str(workspace_dir),
    )

    # Clone the repo
    clone_result = await github.run_git(
        [
            "clone",
            "--depth=1",
            f"https://github.com/{issue.repo}.git",
            workspace,
        ]
    )

    if not clone_result.success:
        logger.warning(
            "clone_failed",
            repo=issue.repo,
            stderr=clone_result.stderr[:300],
        )
        shutil.rmtree(workspace, ignore_errors=True)
        return None

    # Create feature branch
    branch_name = f"fix/{issue.number}"
    branch_result = await github.run_git(["checkout", "-b", branch_name], cwd=workspace)

    if not branch_result.success:
        logger.warning(
            "branch_failed",
            repo=issue.repo,
            stderr=branch_result.stderr[:200],
        )
        shutil.rmtree(workspace, ignore_errors=True)
        return None

    logger.debug("workspace_ready", repo=issue.repo, workspace=workspace)
    return workspace


async def _finalize(
    issue: ScoredIssue,
    db: MemoryDBProtocol,
    outcome: Outcome,
    start: float,
    tokens_used: int,
    claude_calls: int,
    *,
    failure_reason: str | None = None,
    failure_phase: str | None = None,
    pr_url: str | None = None,
    pr_number: int | None = None,
    checkpoints: dict[str, bool] | None = None,
    variant_info: dict[str, str] | None = None,
    reflection_done: bool = False,
) -> PipelineResult:
    """Record outcome and build the final PipelineResult.

    Generates a compressed narrative summary (~200 tokens) of the attempt
    alongside the structured outcome data. This gives the learning engine
    richer context for lesson synthesis.

    Also records step-level checkpoints, generates a Reflexion on rejection,
    and updates prompt variant statistics (Session 3).

    Args:
        reflection_done: If True, skip the template generate_reflection() call because
            generate_real_reflection() was already called for this rejection (avoids
            double-writing reflection records for scope/critic rejections).
    """
    duration = time.monotonic() - start

    # Build compressed narrative summary (Pattern 2 from claude-mem)
    # This is assembled from structured data, NOT a Claude call -- zero cost.
    summary_parts: list[str] = []
    summary_parts.append(f"{outcome.value} on {issue.repo}#{issue.number}")
    summary_parts.append(f"'{issue.title[:80]}'")
    if failure_phase:
        summary_parts.append(f"failed at {failure_phase}")
    if failure_reason:
        summary_parts.append(f"reason: {failure_reason[:120]}")
    if pr_url:
        summary_parts.append(f"PR: {pr_url}")
    summary_parts.append(f"{claude_calls} calls, {tokens_used} tokens, {round(duration, 1)}s")
    summary = ". ".join(summary_parts)

    # Record outcome with summary to memory DB
    try:
        # Use the enhanced method if available (migration 2 adds summary column)
        if hasattr(db, "record_outcome_with_summary"):
            await db.record_outcome_with_summary(
                repo=issue.repo,
                issue_number=issue.number,
                pr_number=pr_number,
                outcome=outcome,
                failure_reason=failure_reason,
                tokens_used=tokens_used,
                summary=summary,
            )
        else:
            await db.record_outcome(
                repo=issue.repo,
                issue_number=issue.number,
                pr_number=pr_number,
                outcome=outcome,
                failure_reason=failure_reason,
                tokens_used=tokens_used,
            )
    except Exception as exc:
        logger.error("outcome_record_failed", error=str(exc))

    # Record step-level checkpoints (PRM)
    if checkpoints is not None:
        try:
            if hasattr(db, "record_checkpoints"):
                await db.record_checkpoints(
                    repo=issue.repo,
                    issue_number=issue.number,
                    checkpoints=checkpoints,
                )
                logger.debug(
                    "checkpoint_recorded",
                    repo=issue.repo,
                    issue=issue.number,
                    checkpoints=checkpoints,
                )
        except Exception as exc:
            logger.warning("checkpoint_record_failed", error=str(exc))

    # Generate Reflexion on rejection (zero Claude calls)
    # Skip if generate_real_reflection() was already called for this rejection --
    # that function records its own reflection (or falls back to this template),
    # so calling it again here would double-write for scope/critic rejections.
    if outcome == Outcome.REJECTED and failure_phase and failure_reason and not reflection_done:
        try:
            await generate_reflection(
                repo=issue.repo,
                issue_number=issue.number,
                title=issue.title,
                labels=list(issue.labels),
                failure_phase=failure_phase,
                failure_reason=failure_reason,
                db=db,
            )
        except Exception as exc:
            logger.warning("reflection_failed", error=str(exc))

    # Circuit breaker: auto-ban repos with repeated failures
    if outcome == Outcome.REJECTED and failure_reason:
        try:
            if failure_reason == "timeout":
                await record_timeout(issue.repo, db)
            else:
                await record_failure(issue.repo, failure_reason, db)
        except Exception as exc:
            logger.warning("circuit_breaker_record_failed", error=str(exc))

    # Session 3: Update prompt variant statistics based on outcome.
    # A "success" for variant tracking is reaching PR submission --
    # the variant contributed to getting past all gates.
    if variant_info and hasattr(db, "update_variant_stats"):
        success = outcome == Outcome.SUBMITTED
        try:
            task_variant = variant_info.get("task_variant", "")
            forbidden_variant = variant_info.get("forbidden_variant", "")
            if task_variant and task_variant != "default":
                await db.update_variant_stats("task", task_variant, success)
            if forbidden_variant and forbidden_variant != "default":
                await db.update_variant_stats("forbidden", forbidden_variant, success)
            logger.debug(
                "variant_stats_updated",
                repo=issue.repo,
                issue=issue.number,
                task_variant=task_variant,
                forbidden_variant=forbidden_variant,
                success=success,
            )
        except Exception as exc:
            logger.warning("variant_stats_update_failed", error=str(exc))

    result = PipelineResult(
        repo=issue.repo,
        issue_number=issue.number,
        outcome=outcome,
        pr_number=pr_number,
        pr_url=pr_url,
        failure_reason=failure_reason,
        failure_phase=failure_phase,
        tokens_used=tokens_used,
        claude_calls=claude_calls,
        duration_sec=round(duration, 2),
    )

    log_fn = logger.info if outcome == Outcome.SUBMITTED else logger.warning
    log_fn(
        "pipeline_complete",
        repo=issue.repo,
        issue=issue.number,
        outcome=outcome.value,
        pr_number=pr_number,
        failure_reason=failure_reason,
        failure_phase=failure_phase,
        duration_sec=result.duration_sec,
        claude_calls=claude_calls,
    )

    return result
