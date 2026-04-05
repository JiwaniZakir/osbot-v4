"""GitHub + Git CLI wrappers -- async subprocess execution.

All ``gh`` and ``git`` calls go through these wrappers so we get:
- Async execution (no blocking the event loop)
- Timeout enforcement
- Rate-limit detection in stderr
- Structured logging of every call

The ``GitHubCLI`` class satisfies ``GitHubCLIProtocol`` from ``osbot.types``,
which combines ``run_gh``, ``run_git``, and ``graphql`` on a single object.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

from osbot.log import get_logger
from osbot.types import CLIResult

logger = get_logger(__name__)

# Rate-limit indicators in gh CLI stderr.
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "API rate limit exceeded",
    "secondary rate limit",
    "abuse detection",
    "403",
    "retry-after",
)


def _detect_rate_limit(stderr: str) -> bool:
    """Return True if stderr suggests a GitHub rate limit."""
    lower = stderr.lower()
    return any(marker.lower() in lower for marker in _RATE_LIMIT_MARKERS)


async def _run_cli(
    binary: str,
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: float = 30.0,
    label: str = "cli",
) -> CLIResult:
    """Execute ``<binary> <args>`` and return a ``CLIResult``.

    Never raises -- errors are captured in the result.
    """
    cmd = [binary, *args]
    logger.debug(f"{label}_run", cmd=cmd, cwd=cwd, timeout=timeout)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        returncode = proc.returncode or 0

        if label == "gh" and _detect_rate_limit(stderr):
            logger.warning("gh_rate_limit", cmd=cmd, stderr=stderr[:200])

        if returncode != 0:
            logger.debug(
                f"{label}_error",
                cmd=cmd,
                returncode=returncode,
                stderr=stderr[:500],
            )

        return CLIResult(returncode=returncode, stdout=stdout, stderr=stderr)

    except TimeoutError:
        logger.warning(f"{label}_timeout", cmd=cmd, timeout=timeout)
        try:
            proc.kill()  # type: ignore[possibly-undefined]
        except (ProcessLookupError, OSError):
            pass
        return CLIResult(returncode=-1, stdout="", stderr="timeout")

    except FileNotFoundError:
        logger.error(f"{label}_not_found", binary=binary)
        return CLIResult(returncode=-1, stdout="", stderr=f"binary not found: {binary}")

    except Exception as exc:
        logger.error(f"{label}_unexpected", cmd=cmd, error=str(exc))
        return CLIResult(returncode=-1, stdout="", stderr=str(exc))


class GitHubCLI:
    """Unified async wrapper around ``gh`` and ``git`` CLI binaries.

    Satisfies ``GitHubCLIProtocol`` which exposes ``run_gh``, ``run_git``,
    and ``graphql``.
    """

    def __init__(
        self,
        gh_binary: str | None = None,
        git_binary: str | None = None,
    ) -> None:
        self._gh = gh_binary or shutil.which("gh") or "gh"
        self._git = git_binary or shutil.which("git") or "git"

    # -- GitHubCLIProtocol methods --

    async def run_gh(
        self,
        args: list[str],
        cwd: str | None = None,
    ) -> CLIResult:
        """Execute ``gh <args>`` and return a ``CLIResult``."""
        return await _run_cli(self._gh, args, cwd=cwd, timeout=30.0, label="gh")

    async def run_git(
        self,
        args: list[str],
        cwd: str | None = None,
    ) -> CLIResult:
        """Execute ``git <args>`` and return a ``CLIResult``."""
        return await _run_cli(self._git, args, cwd=cwd, timeout=60.0, label="git")

    async def run_cmd(
        self,
        cmd: list[str],
        cwd: str | None = None,
        timeout: float = 60.0,
    ) -> CLIResult:
        """Execute an arbitrary command and return a ``CLIResult``.

        Used for running linters (ruff, flake8) and test runners (pytest)
        that are not git subcommands.  ``run_git(["ruff", ...])`` would
        incorrectly run ``git ruff ...`` instead of ``ruff ...``.
        """
        if not cmd:
            return CLIResult(returncode=-1, stdout="", stderr="empty command")
        binary = shutil.which(cmd[0]) or cmd[0]
        return await _run_cli(binary, cmd[1:], cwd=cwd, timeout=timeout, label="cmd")

    async def graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphQL query via ``gh api graphql``.

        Returns the parsed JSON response.  Raises ``RuntimeError`` on
        transport failures.
        """
        gh_args = ["api", "graphql", "-f", f"query={query}"]
        if variables:
            for key, value in variables.items():
                if isinstance(value, str):
                    # -f passes string values
                    gh_args.extend(["-f", f"{key}={value}"])
                else:
                    # -F passes raw JSON values (int, bool, null)
                    gh_args.extend(["-F", f"{key}={json.dumps(value)}"])

        result = await self.run_gh(gh_args)
        if not result.success:
            raise RuntimeError(f"GraphQL query failed: {result.stderr[:300]}")

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"GraphQL response not valid JSON: {exc}") from exc

    # -- Convenience methods --

    async def auth_status(self) -> CLIResult:
        """Check ``gh auth status``."""
        return await _run_cli(self._gh, ["auth", "status"], timeout=10.0, label="gh")
