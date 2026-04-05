#!/usr/bin/env python3
"""
osbot-v4 twice-daily performance audit.

Invoked by the host wrapper via:
    docker exec osbot-v4 python3 /opt/osbot/deploy/audit.py

Sections (no LLM calls, <5 seconds total):
  1. Phase funnel       — last 24h checkpoint pass rates
  2. Token headroom     — latest snapshot vs configured ceilings
  3. Outcome summary    — 24h / 7d totals and submission rate
  4. Wasted calls       — timeout + empty_diff by repo (7d)
  5. Fix: auto-ban      — repos with ≥3 timeouts in 7d (if not already banned)
  6. Fix: expire bans   — prune stale bans
  7. Learning health    — reflections, meta-lessons, skills, prompt variants
  8. Scope analysis     — pass rate per repo (identifies chronic scoping failures)
  9. Report             — appended to state/audit_report.txt (auto-trimmed at 5 MB)

Exit codes: 0 = healthy, 1 = DB unavailable, 2 = token utilization critical
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Config (mirrors config.py defaults; override via container env vars)
# ---------------------------------------------------------------------------

_STATE_DIR = os.environ.get("OSBOT_STATE_DIR", "/opt/osbot/state")
DB_PATH = f"{_STATE_DIR}/memory.db"
REPORT_PATH = f"{_STATE_DIR}/audit_report.txt"

FIVE_HOUR_CEILING: float = float(os.environ.get("OSBOT_FIVE_HOUR_CEILING", "0.60"))
SEVEN_DAY_CEILING: float = float(os.environ.get("OSBOT_SEVEN_DAY_CEILING", "0.50"))
OPUS_CEILING: float = float(os.environ.get("OSBOT_OPUS_CEILING", "0.40"))

TIMEOUT_BAN_THRESHOLD: int = 3     # ban repo after this many timeouts in 7d
TIMEOUT_BAN_DAYS: int = 7          # ban duration in days
TOKEN_HIGH_WARN: float = 0.85      # fraction of ceiling that triggers WARN-HIGH
TOKEN_CRITICAL: float = 0.95       # fraction of ceiling that triggers CRITICAL (exit 2)
TOKEN_LOW_WARN: float = 0.05       # absolute utilisation below which we suspect a stall

# ---------------------------------------------------------------------------
# Shared state for this run
# ---------------------------------------------------------------------------

_now_utc = datetime.now(UTC)
_now_str = _now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
_24h_ago = (_now_utc - timedelta(hours=24)).isoformat()
_7d_ago = (_now_utc - timedelta(days=7)).isoformat()

# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


def _pct(n: int | float, d: int | float) -> str:
    if not d:
        return "n/a"
    return f"{n / d:.0%}"


def _bar(pct: float, width: int = 18) -> str:
    filled = round(max(0.0, min(1.0, pct)) * width)
    return "[" + "█" * filled + "░" * (width - filled) + f"] {pct:.1%}"


def _ago(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        secs = (_now_utc - dt).total_seconds()
        if secs < 3600:
            return f"{int(secs / 60)}m ago"
        if secs < 86400:
            return f"{int(secs / 3600)}h ago"
        return f"{int(secs / 86400)}d ago"
    except Exception:
        return "?"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


# ---------------------------------------------------------------------------
# Section 1: Phase funnel
# ---------------------------------------------------------------------------

_PHASES = [
    ("preflight_passed",         "Preflight    "),
    ("implementation_completed", "Implement    "),
    ("tests_pass",               "Tests        "),
    ("diff_size_ok",             "Diff size    "),
    ("scope_correct",            "Scope        "),
    ("critic_approves",          "Critic       "),
    ("pr_submitted",             "Submitted    "),
]


def _phase_funnel(conn: sqlite3.Connection) -> list[str]:
    out = ["─── Phase Funnel (last 24h) ───"]

    if not _table_exists(conn, "phase_checkpoints"):
        out.append("  (phase_checkpoints not found — migration may not have run yet)")
        return out

    total: int = conn.execute(
        "SELECT COUNT(*) FROM phase_checkpoints WHERE created_at > ?", (_24h_ago,)
    ).fetchone()[0]

    if total == 0:
        out.append("  (no checkpoint rows in last 24h)")
        return out

    out.append(f"  Pipeline runs: {total}")
    for col, label in _PHASES:
        try:
            passed = int(
                conn.execute(
                    f"SELECT COALESCE(SUM({col}), 0) FROM phase_checkpoints WHERE created_at > ?",
                    (_24h_ago,),
                ).fetchone()[0]
            )
        except sqlite3.OperationalError:
            passed = 0
        pct = passed / total
        # Flag low-pass phases that are expected to have meaningful throughput
        flag = " ⚠" if pct < 0.10 and col in ("scope_correct", "critic_approves", "pr_submitted") else ""
        out.append(f"  {label}  {passed:4d}/{total:<4d}  {_bar(pct)}{flag}")

    return out


# ---------------------------------------------------------------------------
# Section 2: Token headroom
# ---------------------------------------------------------------------------


def _token_headroom(conn: sqlite3.Connection) -> tuple[list[str], int]:
    """Returns (lines, exit_code_contribution)."""
    out = ["─── Token Headroom ───"]
    exit_code = 0

    if not _table_exists(conn, "usage_snapshots"):
        out.append("  (usage_snapshots not found)")
        return out, exit_code

    row = conn.execute(
        "SELECT ts, five_hour, seven_day, opus_weekly, sonnet_weekly "
        "FROM usage_snapshots ORDER BY ts DESC LIMIT 1"
    ).fetchone()

    if not row:
        out.append("  (no snapshots recorded yet — token probe may not have run)")
        return out, exit_code

    ts, five_h, seven_d, opus_w, sonnet_w = (
        row[0], float(row[1]), float(row[2]), float(row[3]), float(row[4])
    )

    def _status(val: float, ceiling: float) -> str:
        if not ceiling:
            return "?"
        ratio = val / ceiling
        if ratio >= TOKEN_CRITICAL:
            return "CRITICAL"
        if ratio >= TOKEN_HIGH_WARN:
            return "WARN-HIGH"
        if val < TOKEN_LOW_WARN:
            return "WARN-LOW "
        return "OK       "

    fh_s = _status(five_h, FIVE_HOUR_CEILING)
    sd_s = _status(seven_d, SEVEN_DAY_CEILING)
    op_s = _status(opus_w, OPUS_CEILING)

    out.append(f"  Snapshot age : {_ago(ts)}")
    out.append(f"  5h  utiliz.  : {five_h:.4f} / {FIVE_HOUR_CEILING:.2f} ceiling  [{fh_s}]")
    out.append(f"  7d  utiliz.  : {seven_d:.4f} / {SEVEN_DAY_CEILING:.2f} ceiling  [{sd_s}]")
    out.append(f"  Opus 7d      : {opus_w:.4f} / {OPUS_CEILING:.2f} ceiling  [{op_s}]")
    out.append(f"  Sonnet 7d    : {sonnet_w:.4f}")

    statuses = {fh_s.strip(), sd_s.strip(), op_s.strip()}
    if "CRITICAL" in statuses:
        out.append("  !! CRITICAL: utilization near hard ceiling — bot will pause soon !!")
        exit_code = 2
    elif "WARN-HIGH" in statuses:
        out.append("  !! WARNING: approaching ceiling — monitor closely")
    elif "WARN-LOW" in statuses:
        out.append("  !! WARNING: utilization suspiciously low — bot may be stalled or idle")

    return out, exit_code


# ---------------------------------------------------------------------------
# Section 3: Outcome summary
# ---------------------------------------------------------------------------


def _outcome_summary(conn: sqlite3.Connection) -> list[str]:
    out = ["─── Outcome Summary ───"]

    if not _table_exists(conn, "outcomes"):
        out.append("  (outcomes table not found)")
        return out

    def _counts(since: str) -> dict[str, int]:
        rows = conn.execute(
            "SELECT LOWER(outcome), COUNT(*) FROM outcomes WHERE created_at > ? GROUP BY LOWER(outcome)",
            (since,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    d24 = _counts(_24h_ago)
    d7 = _counts(_7d_ago)

    total_24 = sum(d24.values())
    total_7 = sum(d7.values())
    sub_24 = d24.get("submitted", 0)
    sub_7 = d7.get("submitted", 0)
    merged_7 = d7.get("merged", 0) + d7.get("iterated_merged", 0)
    rejected_24 = d24.get("rejected", 0)

    out.append(
        f"  24h:  {total_24:4d} attempts  →  {sub_24} submitted  "
        f"({_pct(sub_24, total_24)} submit rate)  {rejected_24} rejected"
    )
    out.append(
        f"  7d :  {total_7:4d} attempts  →  {sub_7} submitted  "
        f"→  {merged_7} merged  ({_pct(merged_7, total_7)} merge rate)"
    )

    # Top failure reasons (24h)
    top = conn.execute(
        """
        SELECT failure_reason, COUNT(*) as n FROM outcomes
        WHERE created_at > ?
          AND LOWER(outcome) = 'rejected'
          AND failure_reason IS NOT NULL AND failure_reason != ''
        GROUP BY failure_reason ORDER BY n DESC LIMIT 5
        """,
        (_24h_ago,),
    ).fetchall()
    if top:
        out.append("  Top failure reasons (24h):")
        for reason, count in top:
            out.append(f"    {count:3d}×  {reason}")

    # Lifetime
    lt = conn.execute("SELECT COUNT(*), COALESCE(SUM(tokens_used),0) FROM outcomes").fetchone()
    if lt:
        out.append(f"  Lifetime: {lt[0]:,} total attempts  {int(lt[1]):,} tokens used")

    return out


# ---------------------------------------------------------------------------
# Section 4: Wasted call analysis
# ---------------------------------------------------------------------------


def _wasted_calls(conn: sqlite3.Connection) -> list[str]:
    out = ["─── Wasted Calls (7d) ───"]

    if not _table_exists(conn, "outcomes"):
        out.append("  (outcomes table not found)")
        return out

    # All recognised waste patterns
    waste = conn.execute(
        """
        SELECT failure_reason, COUNT(*) as n, COALESCE(SUM(tokens_used), 0) as tok
        FROM outcomes
        WHERE created_at > ?
          AND (
            failure_reason LIKE '%timeout%'
            OR failure_reason IN ('empty_diff', 'empty diff', 'no_diff', 'no diff')
          )
        GROUP BY failure_reason ORDER BY n DESC
        """,
        (_7d_ago,),
    ).fetchall()

    if not waste:
        out.append("  No timeouts or empty-diff failures in 7d — clean!")
        return out

    tw_n = sum(r[1] for r in waste)
    tw_tok = sum(int(r[2]) for r in waste)
    out.append(f"  Total wasted runs: {tw_n}  (~{tw_tok:,} tokens)")
    for reason, n, tok in waste:
        out.append(f"    {n:3d}×  {reason:<35}  ~{int(tok):,} tokens")

    # Per-repo timeout leaders
    by_repo = conn.execute(
        """
        SELECT repo, COUNT(*) as n FROM outcomes
        WHERE created_at > ? AND failure_reason LIKE '%timeout%'
        GROUP BY repo ORDER BY n DESC LIMIT 8
        """,
        (_7d_ago,),
    ).fetchall()
    if by_repo:
        out.append("  Timeout leaders (7d):")
        for repo, n in by_repo:
            already_banned = bool(
                conn.execute(
                    "SELECT 1 FROM repo_bans WHERE repo=? AND expires_at > ?",
                    (repo, _now_str),
                ).fetchone()
            )
            action = "[already banned]" if already_banned else (
                "→ will auto-ban" if n >= TIMEOUT_BAN_THRESHOLD else ""
            )
            out.append(f"    {n:3d}×  {repo:<40}  {action}")

    return out


# ---------------------------------------------------------------------------
# Section 5: Auto-fix — ban timeout offenders + prune expired bans
# ---------------------------------------------------------------------------


def _auto_ban(conn: sqlite3.Connection) -> list[str]:
    out = ["─── Auto-Fix: Repo Bans ───"]

    if not _table_exists(conn, "repo_bans"):
        out.append("  (repo_bans table not found)")
        return out

    # Prune expired bans first
    deleted = conn.execute(
        "DELETE FROM repo_bans WHERE expires_at < ?", (_now_str,)
    ).rowcount
    conn.commit()
    if deleted:
        out.append(f"  Pruned {deleted} expired ban(s).")

    # Auto-ban timeout offenders
    offenders = conn.execute(
        """
        SELECT repo, COUNT(*) as n FROM outcomes
        WHERE created_at > ? AND failure_reason LIKE '%timeout%'
        GROUP BY repo HAVING n >= ?
        """,
        (_7d_ago, TIMEOUT_BAN_THRESHOLD),
    ).fetchall()

    new_bans = 0
    already = 0
    for repo, n in offenders:
        still_banned = bool(
            conn.execute(
                "SELECT 1 FROM repo_bans WHERE repo=? AND expires_at > ?",
                (repo, _now_str),
            ).fetchone()
        )
        if still_banned:
            already += 1
            continue
        expires = (_now_utc + timedelta(days=TIMEOUT_BAN_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO repo_bans (repo, reason, banned_at, expires_at, created_by) "
            "VALUES (?, ?, ?, ?, 'audit_cron')",
            (repo, f"auto-ban: {n} timeouts in 7d", _now_str, expires),
        )
        out.append(f"  BANNED {repo}  ({n} timeouts → {TIMEOUT_BAN_DAYS}d ban)")
        new_bans += 1
    conn.commit()

    if not offenders:
        out.append("  No repos eligible for timeout-ban.")
    elif new_bans == 0 and already > 0:
        out.append(f"  {already} timeout offender(s) already banned — nothing new.")
    elif new_bans > 0:
        out.append(f"  Applied {new_bans} new ban(s).")

    # Show active bans
    active = conn.execute(
        "SELECT repo, reason, expires_at FROM repo_bans WHERE expires_at > ? ORDER BY expires_at",
        (_now_str,),
    ).fetchall()
    if active:
        out.append(f"  Active bans ({len(active)}):")
        for repo, reason, expires in active:
            out.append(f"    {repo:<38}  until {expires[:10]}  [{reason[:48]}]")
    else:
        out.append("  No active bans.")

    return out


# ---------------------------------------------------------------------------
# Section 6: Learning system health
# ---------------------------------------------------------------------------


def _learning_health(conn: sqlite3.Connection) -> list[str]:
    out = ["─── Learning System Health ───"]

    # Reflections
    if _table_exists(conn, "reflections"):
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(used_count),0), COALESCE(SUM(led_to_success),0) "
            "FROM reflections"
        ).fetchone()
        total_r, total_used, led_ok = int(row[0]), int(row[1]), int(row[2])
        new_7d = conn.execute(
            "SELECT COUNT(*) FROM reflections WHERE created_at > ?", (_7d_ago,)
        ).fetchone()[0]
        out.append(
            f"  Reflections : {total_r} total  +{new_7d} (7d)  "
            f"{total_used}× retrieved  {led_ok} led to success"
        )
        phases = conn.execute(
            "SELECT failure_phase, COUNT(*) FROM reflections "
            "GROUP BY failure_phase ORDER BY 2 DESC"
        ).fetchall()
        if phases:
            out.append("    by phase: " + "  ".join(f"{p}:{n}" for p, n in phases))
    else:
        out.append("  Reflections : table not found")

    # Meta-lessons
    if _table_exists(conn, "meta_lessons"):
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(confidence), 0) FROM meta_lessons"
        ).fetchone()
        out.append(f"  Meta-lessons: {row[0]} total  max confidence {float(row[1]):.2f}")
    else:
        out.append("  Meta-lessons: table not found")

    # Skill library
    if _table_exists(conn, "skills"):
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(led_to_success),0), COALESCE(SUM(used_count),0) "
            "FROM skills"
        ).fetchone()
        total_sk, sk_ok, sk_used = int(row[0]), int(row[1]), int(row[2])
        new_7d_sk = conn.execute(
            "SELECT COUNT(*) FROM skills WHERE created_at > ?", (_7d_ago,)
        ).fetchone()[0]
        out.append(
            f"  Skills      : {total_sk} total  +{new_7d_sk} (7d)  "
            f"{sk_ok} led to success  {sk_used}× retrieved"
        )
    else:
        out.append("  Skills      : table not found")

    # Prompt variants
    if _table_exists(conn, "prompt_variants"):
        variants = conn.execute(
            "SELECT prompt_section, variant_name, success_rate, times_used "
            "FROM prompt_variants WHERE active=1 "
            "ORDER BY prompt_section, success_rate DESC"
        ).fetchall()
        if variants:
            out.append(f"  Prompt variants ({len(variants)}):")
            for sec, name, rate, used in variants:
                out.append(f"    {sec}/{name:<20}  {float(rate):.0%} success  {used}× used")
    else:
        out.append("  Prompt variants: table not found")

    return out


# ---------------------------------------------------------------------------
# Section 7: Scope pass rate per repo
# ---------------------------------------------------------------------------


def _scope_analysis(conn: sqlite3.Connection) -> list[str]:
    out = ["─── Scope Pass Rate by Repo (7d, ≥5 runs) ───"]

    if not _table_exists(conn, "phase_checkpoints"):
        out.append("  (phase_checkpoints not found)")
        return out

    rows = conn.execute(
        """
        SELECT repo,
               COUNT(*)              AS total,
               SUM(scope_correct)    AS scope_ok,
               SUM(pr_submitted)     AS submitted
        FROM phase_checkpoints
        WHERE created_at > ?
        GROUP BY repo HAVING total >= 5
        ORDER BY CAST(scope_ok AS REAL) / total ASC
        LIMIT 12
        """,
        (_7d_ago,),
    ).fetchall()

    if not rows:
        out.append("  (fewer than 5 runs per repo in last 7d — not enough data)")
        return out

    for repo, total, scope_ok, submitted in rows:
        scope_ok = int(scope_ok or 0)
        submitted = int(submitted or 0)
        banned = bool(
            conn.execute(
                "SELECT 1 FROM repo_bans WHERE repo=? AND expires_at > ?",
                (repo, _now_str),
            ).fetchone()
        )
        flag = "  [banned]" if banned else ""
        out.append(
            f"  {repo:<38}  scope {_pct(scope_ok, total):<5}  "
            f"submit {_pct(submitted, total):<5}  ({total} runs){flag}"
        )

    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ts_start = datetime.now(UTC)
    separator = "=" * 65

    header_lines = [
        "",
        separator,
        "  OSBOT-V4 PERFORMANCE AUDIT",
        f"  {ts_start.strftime('%Y-%m-%d %H:%M UTC')}",
        separator,
    ]
    for line in header_lines:
        print(line, flush=True)

    conn = None  # opened below

    # Open DB
    db_path = Path(DB_PATH)
    if not db_path.exists():
        print(f"[AUDIT] ERROR: DB not found at {DB_PATH}", flush=True)
        sys.exit(1)
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error as exc:
        print(f"[AUDIT] ERROR: cannot open DB: {exc}", flush=True)
        sys.exit(1)

    all_output: list[str] = list(header_lines)
    exit_code = 0

    try:
        token_lines, token_exit = _token_headroom(conn)
        exit_code = max(exit_code, token_exit)

        all_sections: list[list[str]] = [
            _phase_funnel(conn),
            token_lines,
            _outcome_summary(conn),
            _wasted_calls(conn),
            _auto_ban(conn),
            _learning_health(conn),
            _scope_analysis(conn),
        ]

        for section in all_sections:
            print("", flush=True)
            all_output.append("")
            for line in section:
                print(line, flush=True)
                all_output.append(line)

    finally:
        conn.close()

    elapsed = (datetime.now(UTC) - ts_start).total_seconds()
    footer_lines = [
        "",
        separator,
        f"  Audit complete in {elapsed:.1f}s  exit={exit_code}",
        separator,
        "",
    ]
    for line in footer_lines:
        print(line, flush=True)
    all_output.extend(footer_lines)

    # Persist report (append, auto-trim at 5 MB)
    try:
        report_path = Path(REPORT_PATH)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("a") as f:
            f.write("\n".join(all_output) + "\n")
        if report_path.stat().st_size > 5 * 1024 * 1024:
            content = report_path.read_text()
            report_path.write_text(content[-(4 * 1024 * 1024):])
    except OSError as exc:
        print(f"[AUDIT] WARNING: could not write report file: {exc}", flush=True)

    sys.exit(exit_code)
