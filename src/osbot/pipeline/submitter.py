"""Submitter -- fork, clone, push, create PR.

Handles fork-already-exists gracefully.  All git/gh calls via
``asyncio.create_subprocess_exec`` (through GitHubCLIProtocol).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from osbot.config import settings
from osbot.log import get_logger

if TYPE_CHECKING:
    from osbot.types import GitHubCLIProtocol, ScoredIssue

logger = get_logger(__name__)


_AI_PREFIXES = [
    "claude:",
    "gpt:",
    "chatgpt:",
    "openai:",
    "ai:",
    "copilot:",
    "gemini:",
    "bard:",
    "llm:",
    "bot:",
]


def _sanitize_title(title: str) -> str:
    """Strip AI-identifying prefixes from issue titles."""
    stripped = title.strip()
    for prefix in _AI_PREFIXES:
        if stripped.lower().startswith(prefix):
            stripped = stripped[len(prefix) :].strip()
            break
    return stripped


def _branch_name(issue: ScoredIssue) -> str:
    """Generate a deterministic branch name for the issue."""
    # Sanitize title for branch name
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", issue.title.lower())[:40].strip("-")
    return f"fix/{issue.number}-{slug}"


async def submit(
    issue: ScoredIssue,
    workspace: str,
    pr_body: str,
    github: GitHubCLIProtocol,
) -> tuple[str, int]:
    """Submit a PR for the implemented fix.

    Assumes the workspace already has committed changes on a feature branch
    (set up by the orchestrator before calling the pipeline).

    Args:
        issue: The issue being fixed.
        workspace: Path to the cloned repo with committed changes.
        pr_body: The full PR body (including ``Closes #N``).
        github: GitHub CLI wrapper.

    Returns:
        ``(pr_url, pr_number)`` on success.

    Raises:
        RuntimeError: If any critical step fails (fork, push, PR creation).
    """
    branch = _branch_name(issue)

    # 1. Fork the repo (idempotent -- gh fork handles already-forked)
    await _ensure_fork(issue.repo, github)

    # 2. Set up remote and push
    await _setup_remote_and_push(issue, workspace, branch, github)

    # 3. Humanizer delay before PR creation (anti-detection)
    from osbot.timing.humanizer import Humanizer

    humanizer = Humanizer()
    logger.info("submit_humanizer_delay_start", repo=issue.repo)
    await humanizer.delay_pr_creation()

    # 4. Create PR
    pr_url, pr_number = await _create_pr(issue, branch, pr_body, github)

    logger.info(
        "submit_done",
        repo=issue.repo,
        issue=issue.number,
        pr_url=pr_url,
        pr_number=pr_number,
        branch=branch,
    )

    return pr_url, pr_number


async def _ensure_fork(repo: str, github: GitHubCLIProtocol) -> None:
    """Fork the repo if not already forked.  Idempotent.

    Raises RuntimeError if the fork cannot be confirmed to exist.
    """
    result = await github.run_gh(
        [
            "repo",
            "fork",
            repo,
            "--clone=false",
        ]
    )

    if result.success:
        logger.debug("fork_created", repo=repo)
        return
    if "already exists" in result.stderr.lower():
        logger.debug("fork_exists", repo=repo)
        return

    # Fork command failed -- verify the fork exists anyway (race or prior fork).
    username = settings.github_username
    _, name = repo.split("/", 1)
    fork_repo = f"{username}/{name}"
    verify = await github.run_gh(["repo", "view", fork_repo])
    if verify.success:
        logger.debug("fork_verified_existing", repo=repo, fork=fork_repo)
        return

    raise RuntimeError(f"fork creation failed for {repo} and fork {fork_repo} does not exist: {result.stderr[:200]}")


async def _setup_remote_and_push(
    issue: ScoredIssue,
    workspace: str,
    branch: str,
    github: GitHubCLIProtocol,
) -> None:
    """Rename the current branch, add our fork as remote, and push."""
    username = settings.github_username
    owner, name = issue.repo.split("/", 1)
    fork_url = f"https://github.com/{username}/{name}.git"

    # Rename current branch to our feature branch name
    await github.run_git(["checkout", "-b", branch], cwd=workspace)

    # Add fork as remote (ignore error if already exists)
    add_result = await github.run_git(["remote", "add", "fork", fork_url], cwd=workspace)
    if not add_result.success and "already exists" not in add_result.stderr.lower():
        # Try setting the URL instead
        await github.run_git(["remote", "set-url", "fork", fork_url], cwd=workspace)

    # Push to fork
    push_result = await github.run_git(["push", "-u", "fork", branch, "--force"], cwd=workspace)
    if not push_result.success:
        raise RuntimeError(f"git push failed: {push_result.stderr[:300]}")

    logger.debug("push_done", repo=issue.repo, branch=branch)


async def _create_pr(
    issue: ScoredIssue,
    branch: str,
    pr_body: str,
    github: GitHubCLIProtocol,
) -> tuple[str, int]:
    """Create the PR via gh CLI.  Returns ``(url, number)``."""
    username = settings.github_username

    # Build PR title from issue title (sanitize AI-identifying prefixes)
    clean_title = _sanitize_title(issue.title)
    pr_title = f"Fix #{issue.number}: {clean_title}"
    if len(pr_title) > 72:
        pr_title = pr_title[:69] + "..."

    head = f"{username}:{branch}"

    result = await github.run_gh(
        [
            "pr",
            "create",
            "--repo",
            issue.repo,
            "--head",
            head,
            "--title",
            pr_title,
            "--body",
            pr_body,
        ]
    )

    if not result.success:
        raise RuntimeError(f"gh pr create failed: {result.stderr[:300]}")

    # Parse URL and number from output
    pr_url = result.stdout.strip()
    pr_number = _extract_pr_number(pr_url)

    return pr_url, pr_number


def _extract_pr_number(url: str) -> int:
    """Extract PR number from a GitHub PR URL."""
    # URL format: https://github.com/owner/repo/pull/123
    match = re.search(r"/pull/(\d+)", url)
    if match:
        return int(match.group(1))
    # Fallback: try to find any trailing number
    match = re.search(r"(\d+)\s*$", url)
    if match:
        return int(match.group(1))
    return 0
