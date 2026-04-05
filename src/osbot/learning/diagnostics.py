"""Diagnostics -- fast per-cycle checks and 12-hour deep analysis.

**fast_diagnostic** (every cycle, 0 Claude calls, < 1 second):
  Scans the last 20 traces for patterns that indicate stuck behavior.

**deep_diagnostic** (every 12 hours, 0 Claude calls):
  Computes waste ratio, per-repo failure rates, and bans underperformers.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from osbot.log import get_logger
from osbot.types import Correction, MemoryDBProtocol, Trace

logger = get_logger(__name__)

# How many recent traces to scan.
SCAN_WINDOW = 20

# Thresholds.
LOOP_THRESHOLD = 3  # same repo + same error
TIMEOUT_WARN = 2
TIMEOUT_BAN = 4
DEAD_CYCLE_THRESHOLD = 15

# Operational failure markers in trace detail/outcome.
_OPERATIONAL_MARKERS = (
    "tos",
    "terms of service",
    "accept terms",
    "auth error",
    "authentication",
    "not logged in",
    "oauth",
    "token expired",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def fast_diagnostic(
    traces: list[Trace],
    *,
    db: MemoryDBProtocol | None = None,
) -> list[Correction]:
    """Scan recent traces and return any corrections that should be applied.

    This function must be fast (< 1 second) and must NOT call Claude.
    The caller is responsible for persisting corrections and executing bans.

    Args:
        traces: The most recent traces (up to *SCAN_WINDOW*).
        db: Optional memory DB for writing bans.  If None, corrections are
            returned but bans are not executed (useful during startup before
            the DB is available).

    Returns:
        A list of ``Correction`` objects describing actions taken or recommended.
    """
    if not traces:
        return []

    window = traces[-SCAN_WINDOW:]
    corrections: list[Correction] = []

    # ------------------------------------------------------------------
    # 1. Operational failure detection
    # ------------------------------------------------------------------
    for trace in window:
        detail_lower = ((trace.failure_reason or "") + " " + trace.outcome).lower()
        for marker in _OPERATIONAL_MARKERS:
            if marker in detail_lower:
                correction = Correction(
                    ts=_now_iso(),
                    type="halt",
                    reason=f"operational failure detected: {marker}",
                    severity="critical",
                    message=f"Halt all work. Trace: repo={trace.repo} phase={trace.phase} detail={(trace.failure_reason or '')[:100]}",
                )
                corrections.append(correction)
                logger.error(
                    "diagnostic_halt",
                    reason=marker,
                    repo=trace.repo,
                    phase=trace.phase,
                )
                # Send webhook alert for halt corrections
                from osbot.comms.webhook import send_alert

                await send_alert(
                    f"HALT: {marker} in {trace.repo} ({trace.phase}). Detail: {(trace.failure_reason or '')[:100]}",
                    severity="critical",
                )
                # One halt is enough -- return immediately.
                return corrections

    # ------------------------------------------------------------------
    # 2. Loop detection: same repo + same error 3+ times
    # ------------------------------------------------------------------
    repo_error_counts: Counter[tuple[str, str]] = Counter()
    for trace in window:
        if trace.outcome in ("error", "rejected", "timeout", "self_review_rejected", "preflight_rejected"):
            key = (trace.repo, trace.failure_reason or trace.outcome)
            repo_error_counts[key] += 1

    for (repo, error), count in repo_error_counts.items():
        if count >= LOOP_THRESHOLD:
            correction = Correction(
                ts=_now_iso(),
                type="ban_repo",
                repo=repo,
                days=7,
                reason=f"loop detected: {error!r} x{count}",
                severity="warning",
                message=f"Same error repeated {count} times in scan window",
            )
            corrections.append(correction)
            logger.warning(
                "diagnostic_loop",
                repo=repo,
                error=error,
                count=count,
            )

            if db is not None:
                from osbot.safety.circuit_breaker import ban_repo

                await ban_repo(repo, 7, f"loop: {error} x{count}", db, "fast_diagnostic")

    # ------------------------------------------------------------------
    # 3. Timeout detection: same repo timed out 2+ / 4+ times
    # ------------------------------------------------------------------
    repo_timeouts: Counter[str] = Counter()
    for trace in window:
        if trace.outcome == "timeout":
            repo_timeouts[trace.repo] += 1

    for repo, count in repo_timeouts.items():
        if count >= TIMEOUT_BAN:
            correction = Correction(
                ts=_now_iso(),
                type="ban_repo",
                repo=repo,
                days=14,
                reason=f"timeout escalation: {count}x timeouts",
                severity="warning",
            )
            corrections.append(correction)
            logger.warning("diagnostic_timeout_ban", repo=repo, count=count)

            if db is not None:
                from osbot.safety.circuit_breaker import ban_repo

                await ban_repo(repo, 14, f"timeout x{count}", db, "fast_diagnostic")

        elif count >= TIMEOUT_WARN:
            correction = Correction(
                ts=_now_iso(),
                type="adjust_score",
                repo=repo,
                reason=f"timeout warning: {count}x timeouts, -2.0 score penalty",
                severity="low",
            )
            corrections.append(correction)
            logger.info("diagnostic_timeout_warn", repo=repo, count=count)

    # ------------------------------------------------------------------
    # 4. Dead cycle detection: 15+ consecutive cycles with 0 submissions
    # ------------------------------------------------------------------
    # A "submission" is any trace with outcome in {merged, rejected, ignored, stuck, iterated_merged}.
    submission_outcomes = {"merged", "rejected", "ignored", "stuck", "iterated_merged"}
    has_submission = any(t.outcome in submission_outcomes for t in window)

    if not has_submission and len(window) >= DEAD_CYCLE_THRESHOLD:
        correction = Correction(
            ts=_now_iso(),
            type="force_discovery",
            reason=f"dead cycles: {len(window)} traces with 0 submissions",
            severity="high",
            message="Forcing discovery phase to refresh issue queue",
        )
        corrections.append(correction)
        logger.warning("diagnostic_dead_cycles", trace_count=len(window))

        # Send webhook alert for high severity findings
        from osbot.comms.webhook import send_alert

        await send_alert(
            f"Dead cycles: {len(window)} traces with 0 submissions. Forcing discovery.",
            severity="high",
        )

    return corrections


# ---------------------------------------------------------------------------
# Deep diagnostic -- runs every 12 hours, 0 Claude calls
# ---------------------------------------------------------------------------

# Thresholds for deep analysis
_DEEP_ATTEMPT_THRESHOLD = 5  # repos with N+ attempts and 0 submissions -> ban
_DEEP_BAN_DAYS = 14
_WASTE_RATIO_ALERT = 0.30  # warn if > 30% of Claude calls didn't lead to submission


async def deep_diagnostic(
    db: MemoryDBProtocol,
) -> list[Correction]:
    """12-hour deep analysis.  Computes waste ratio, per-repo failure rates,
    and bans repos that consistently fail.

    Zero Claude calls -- pure arithmetic on the outcomes table.

    Args:
        db: Memory DB to read outcomes and write bans.

    Returns:
        A list of ``Correction`` objects describing actions taken.
    """
    corrections: list[Correction] = []

    # ------------------------------------------------------------------
    # 1. Per-repo submission rate: repos with 5+ attempts and 0 submissions
    # ------------------------------------------------------------------
    try:
        rows = await db.fetchall(
            """
            SELECT repo,
                   COUNT(*) as attempts,
                   SUM(CASE WHEN outcome = 'submitted' OR outcome = 'merged' THEN 1 ELSE 0 END) as submissions
            FROM outcomes
            GROUP BY repo
            HAVING attempts >= ?
            """,
            (_DEEP_ATTEMPT_THRESHOLD,),
        )

        for row in rows:
            repo = row.get("repo", "")
            attempts = row.get("attempts", 0)
            submissions = row.get("submissions", 0)

            if submissions == 0 and attempts >= _DEEP_ATTEMPT_THRESHOLD:
                # Check if already banned
                if await db.is_repo_banned(repo):
                    continue

                correction = Correction(
                    ts=_now_iso(),
                    type="ban_repo",
                    repo=repo,
                    days=_DEEP_BAN_DAYS,
                    reason=f"deep diag: {attempts} attempts, 0 submissions",
                    severity="warning",
                    message=f"Banning underperforming repo for {_DEEP_BAN_DAYS} days",
                )
                corrections.append(correction)
                logger.warning(
                    "deep_diag_ban",
                    repo=repo,
                    attempts=attempts,
                    submissions=submissions,
                )

                await db.ban_repo(repo, f"deep_diag: {attempts} attempts, 0 subs", _DEEP_BAN_DAYS, "deep_diagnostic")

    except Exception as exc:
        logger.error("deep_diag_repo_analysis_error", error=str(exc))

    # ------------------------------------------------------------------
    # 2. Waste ratio: Claude calls that didn't lead to submission
    # ------------------------------------------------------------------
    try:
        total_row = await db.fetchone(
            "SELECT COUNT(*) as total FROM outcomes",
            (),
        )
        submitted_row = await db.fetchone(
            "SELECT COUNT(*) as submitted FROM outcomes WHERE outcome IN ('submitted', 'merged')",
            (),
        )
        total = (total_row or {}).get("total", 0)
        submitted = (submitted_row or {}).get("submitted", 0)

        if total > 0:
            waste_ratio = 1.0 - (submitted / total)
            logger.info(
                "deep_diag_waste_ratio",
                total=total,
                submitted=submitted,
                waste_ratio=round(waste_ratio, 3),
            )

            if waste_ratio > _WASTE_RATIO_ALERT:
                correction = Correction(
                    ts=_now_iso(),
                    type="alert",
                    reason=f"waste ratio {waste_ratio:.1%} exceeds {_WASTE_RATIO_ALERT:.0%} threshold",
                    severity="high",
                    message=f"Total: {total}, submitted: {submitted}. Consider tightening issue selection.",
                )
                corrections.append(correction)

                from osbot.comms.webhook import send_alert

                await send_alert(
                    f"Waste ratio {waste_ratio:.1%}: {total} attempts, {submitted} submissions. "
                    f"Threshold: {_WASTE_RATIO_ALERT:.0%}.",
                    severity="high",
                )

    except Exception as exc:
        logger.error("deep_diag_waste_ratio_error", error=str(exc))

    # ------------------------------------------------------------------
    # 3. Per-phase failure rates (informational)
    # ------------------------------------------------------------------
    try:
        phase_rows = await db.fetchall(
            """
            SELECT failure_reason, COUNT(*) as cnt
            FROM outcomes
            WHERE outcome NOT IN ('submitted', 'merged')
              AND failure_reason IS NOT NULL
            GROUP BY failure_reason
            ORDER BY cnt DESC
            LIMIT 10
            """,
            (),
        )
        if phase_rows:
            top_failures = {row.get("failure_reason", ""): row.get("cnt", 0) for row in phase_rows}
            logger.info("deep_diag_failure_breakdown", top_failures=top_failures)

    except Exception as exc:
        logger.error("deep_diag_failure_breakdown_error", error=str(exc))

    # ------------------------------------------------------------------
    # 4. Step-level checkpoint analysis (PRM)
    # ------------------------------------------------------------------
    try:
        if hasattr(db, "get_phase_stats"):
            stats = await db.get_phase_stats()
            if stats:
                # Log per-phase pass rates
                phase_rates: dict[str, str] = {}
                bottleneck_phase = ""
                lowest_rate = 1.0

                for phase, counts in stats.items():
                    total = counts["total"]
                    passed = counts["passed"]
                    if total > 0:
                        rate = passed / total
                        phase_rates[phase] = f"{passed}/{total} ({rate:.0%})"
                        if rate < lowest_rate:
                            lowest_rate = rate
                            bottleneck_phase = phase
                    else:
                        phase_rates[phase] = "0/0"

                logger.info(
                    "deep_diag_phase_stats",
                    phase_pass_rates=phase_rates,
                    bottleneck=bottleneck_phase,
                    bottleneck_rate=round(lowest_rate, 3) if bottleneck_phase else None,
                )

                # Create a correction if a phase consistently fails (< 30% pass rate)
                if bottleneck_phase and lowest_rate < 0.30 and stats[bottleneck_phase]["total"] >= 5:
                    correction = Correction(
                        ts=_now_iso(),
                        type="alert",
                        reason=(
                            f"phase bottleneck: {bottleneck_phase} has "
                            f"{lowest_rate:.0%} pass rate "
                            f"({stats[bottleneck_phase]['passed']}/{stats[bottleneck_phase]['total']})"
                        ),
                        severity="high",
                        message=f"Consider addressing {bottleneck_phase} failures to improve pipeline throughput",
                    )
                    corrections.append(correction)
    except Exception as exc:
        logger.error("deep_diag_phase_stats_error", error=str(exc))

    if corrections:
        logger.info("deep_diag_complete", corrections=len(corrections))
    else:
        logger.info("deep_diag_complete", corrections=0, message="no issues found")

    return corrections
