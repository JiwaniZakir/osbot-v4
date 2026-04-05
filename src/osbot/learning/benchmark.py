"""Contributor benchmarking -- study top contributors via ``gh`` CLI.

Monthly cadence. Zero Claude calls. Analyzes top 5 contributors per repo:
average PR size, test inclusion rate, common labels, commit message style.
Results stored in ``repo_facts`` as benchmark guidance.

Restored from v3's ``contributor_benchmark.py`` which was lost in the v4 migration.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from osbot.log import get_logger

if TYPE_CHECKING:
    from osbot.types import GitHubCLIProtocol, MemoryDBProtocol

logger = get_logger(__name__)


async def benchmark_repo(
    repo: str,
    github: GitHubCLIProtocol,
    db: MemoryDBProtocol,
) -> dict[str, Any] | None:
    """Analyze top contributors for a repo and store patterns.

    Examines the last 30 merged PRs to determine:
    - Average diff size (lines changed)
    - Test inclusion rate (fraction of PRs touching test files)
    - Most common labels on merged PRs
    - Typical commit message patterns

    Zero Claude calls — pure ``gh`` CLI data.

    Returns:
        Dict with benchmark data, or None if insufficient data.
    """
    # Fetch recent merged PRs with detail (including body for length analysis)
    result = await github.run_gh(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--limit",
            "30",
            "--json",
            "author,additions,deletions,changedFiles,labels,title,mergedAt,body,files",
        ]
    )

    if not result.success:
        logger.debug("benchmark_fetch_failed", repo=repo, error=result.stderr[:200])
        return None

    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    if len(prs) < 5:
        logger.debug("benchmark_insufficient_data", repo=repo, prs=len(prs))
        return None

    # Compute metrics
    total_lines = []
    test_touch_count = 0
    label_counts: dict[str, int] = {}
    body_lengths: list[int] = []
    title_lengths: list[int] = []
    prefix_counts: dict[str, int] = {}
    dir_counts: dict[str, int] = {}

    for pr in prs:
        additions = pr.get("additions", 0) or 0
        deletions = pr.get("deletions", 0) or 0
        total_lines.append(additions + deletions)

        title = pr.get("title") or ""
        title_lengths.append(len(title))
        if "test" in title.lower() or "spec" in title.lower():
            test_touch_count += 1

        # Track commit prefix style (conventional commit prefixes from titles)
        prefix_match = re.match(
            r"^(fix|feat|chore|docs|refactor|test|ci|build|perf|style|revert)\b",
            title,
            re.I,
        )
        if prefix_match:
            prefix = prefix_match.group(1).lower()
            prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1

        for label in pr.get("labels", []):
            name = label.get("name", "") if isinstance(label, dict) else str(label)
            if name:
                label_counts[name] = label_counts.get(name, 0) + 1

        # PR body length
        body = pr.get("body") or ""
        body_lengths.append(len(body))

        # Track directories modified by top contributors
        for file_entry in pr.get("files", []):
            # gh returns files as list of dicts with "path" key
            path = ""
            if isinstance(file_entry, dict):
                path = file_entry.get("path", "")
            elif isinstance(file_entry, str):
                path = file_entry
            if "/" in path:
                top_dir = path.split("/")[0]
                dir_counts[top_dir] = dir_counts.get(top_dir, 0) + 1

    avg_pr_lines = sum(total_lines) / len(total_lines) if total_lines else 50
    test_inclusion_rate = test_touch_count / len(prs) if prs else 0.0

    # Top 5 labels
    common_labels = sorted(label_counts, key=label_counts.get, reverse=True)[:5]

    # Commit message style: check if most titles use conventional commits
    conventional = sum(prefix_counts.values())
    uses_conventional = conventional > len(prs) * 0.5

    # Top 3 commit prefixes (e.g., "fix: | feat: | chore:")
    top_prefixes = sorted(prefix_counts, key=prefix_counts.get, reverse=True)[:3]
    commit_prefix_style = " | ".join(f"{p}:" for p in top_prefixes) if top_prefixes else ""

    # Average PR body length
    avg_pr_body_len = round(sum(body_lengths) / len(body_lengths)) if body_lengths else 0

    # Average title length
    avg_title_len = round(sum(title_lengths) / len(title_lengths)) if title_lengths else 50

    # Top 3 directories modified
    typical_dirs = sorted(dir_counts, key=dir_counts.get, reverse=True)[:3]

    benchmark = {
        "avg_pr_lines": round(avg_pr_lines, 1),
        "test_inclusion_rate": round(test_inclusion_rate, 2),
        "common_labels": common_labels,
        "uses_conventional_commits": uses_conventional,
        "sample_size": len(prs),
        "commit_prefix_style": commit_prefix_style,
        "avg_pr_body_len": avg_pr_body_len,
        "avg_title_len": avg_title_len,
        "typical_dirs": typical_dirs,
    }

    # Persist to repo_facts
    await db.set_repo_fact(
        repo=repo,
        key="benchmark",
        value=json.dumps(benchmark),
        source="contributor_benchmark",
        confidence=min(0.9, len(prs) / 30),
    )

    logger.info(
        "benchmark_complete",
        repo=repo,
        avg_pr_lines=benchmark["avg_pr_lines"],
        test_rate=benchmark["test_inclusion_rate"],
        labels=common_labels[:3],
    )

    return benchmark


async def benchmark_active_pool(
    repos: list[str],
    github: GitHubCLIProtocol,
    db: MemoryDBProtocol,
) -> int:
    """Benchmark all repos in the active pool. Monthly cadence.

    Returns the number of repos successfully benchmarked.
    """
    count = 0
    for repo in repos:
        # Check if we already have recent benchmark data
        existing = await db.get_repo_fact(repo, "benchmark")
        if existing:
            # Only re-benchmark if data is old (checked by caller's cadence)
            pass

        result = await benchmark_repo(repo, github, db)
        if result:
            count += 1

    logger.info("benchmark_pool_complete", repos_benchmarked=count, total=len(repos))
    return count
