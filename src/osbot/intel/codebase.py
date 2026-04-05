"""Codebase analyzer -- detect test framework, lint tools, style conventions.

Scans a cloned workspace using ``gh`` / ``git`` CLI.  Returns a compact
dict of style notes (max 200 tokens) that the implementation prompt
injects so Claude follows repo conventions.

Zero Claude calls.  Layer 2.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from osbot.log import get_logger

if TYPE_CHECKING:
    from osbot.types import GitHubCLIProtocol

logger = get_logger(__name__)

# File-path patterns used to detect test frameworks and lint tools.
_TEST_INDICATORS: dict[str, list[str]] = {
    "pytest": ["pytest.ini", "pyproject.toml", "conftest.py", "setup.cfg"],
    "unittest": [],  # detected via import scanning
    "jest": ["jest.config.js", "jest.config.ts", "jest.config.mjs"],
    "vitest": ["vitest.config.ts", "vitest.config.js"],
    "mocha": [".mocharc.yml", ".mocharc.json"],
}

_LINT_INDICATORS: dict[str, list[str]] = {
    "ruff": ["ruff.toml", "pyproject.toml"],
    "flake8": [".flake8", "setup.cfg", "tox.ini"],
    "pylint": [".pylintrc", "pyproject.toml"],
    "eslint": [".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml"],
    "biome": ["biome.json"],
}


async def analyze_codebase(
    repo: str,
    github: GitHubCLIProtocol,
) -> dict[str, Any]:
    """Detect test framework, lint tool, primary language, and style cues.

    Args:
        repo: ``"owner/name"`` identifier.
        github: CLI protocol for running ``gh`` commands.

    Returns:
        Dict with keys: ``test_framework``, ``lint_tool``, ``language``,
        ``has_contributing``, ``has_ci``, ``style_notes`` (str, max ~200 tokens).
    """
    result: dict[str, Any] = {
        "test_framework": None,
        "lint_tool": None,
        "language": None,
        "has_contributing": False,
        "has_ci": False,
        "style_notes": "",
    }

    # Fetch repo metadata for primary language
    meta = await github.run_gh(["api", f"repos/{repo}", "--jq", ".language"])
    if meta.success and meta.stdout.strip():
        result["language"] = meta.stdout.strip()

    # Fetch top-level directory listing via the API
    tree_result = await github.run_gh([
        "api", f"repos/{repo}/git/trees/HEAD",
        "--jq", '.tree[] | .path',
    ])
    top_files: set[str] = set()
    if tree_result.success:
        top_files = {line.strip() for line in tree_result.stdout.splitlines() if line.strip()}

    # CI detection
    if ".github" in top_files:
        workflows = await github.run_gh([
            "api", f"repos/{repo}/contents/.github/workflows",
            "--jq", '.[].name',
        ])
        if workflows.success and workflows.stdout.strip():
            result["has_ci"] = True

    # CONTRIBUTING.md detection
    for candidate in ("CONTRIBUTING.md", "CONTRIBUTING.rst", ".github/CONTRIBUTING.md"):
        if candidate in top_files or candidate.split("/")[0] in top_files:
            check = await github.run_gh([
                "api", f"repos/{repo}/contents/{candidate}",
                "--jq", ".name",
            ])
            if check.success and check.stdout.strip():
                result["has_contributing"] = True
                break

    # Detect test framework via pyproject.toml / package.json content
    test_framework = await _detect_test_framework(repo, top_files, github)
    result["test_framework"] = test_framework

    # Detect lint tool
    lint_tool = await _detect_lint_tool(repo, top_files, github)
    result["lint_tool"] = lint_tool

    # Build style notes string (compact, max ~200 tokens)
    notes_parts: list[str] = []
    if result["language"]:
        notes_parts.append(f"Language: {result['language']}")
    if test_framework:
        notes_parts.append(f"Test framework: {test_framework}")
    if lint_tool:
        notes_parts.append(f"Lint tool: {lint_tool}")
    if result["has_ci"]:
        notes_parts.append("CI: GitHub Actions")
    if result["has_contributing"]:
        notes_parts.append("Has CONTRIBUTING.md")

    result["style_notes"] = ". ".join(notes_parts)

    logger.info("codebase_analyzed", repo=repo, **{k: v for k, v in result.items() if k != "style_notes"})
    return result


async def _detect_test_framework(
    repo: str, top_files: set[str], github: GitHubCLIProtocol
) -> str | None:
    """Detect the test framework from config files and content."""
    # Check pyproject.toml for pytest config
    if "pyproject.toml" in top_files:
        content = await github.run_gh([
            "api", f"repos/{repo}/contents/pyproject.toml",
            "--jq", ".content",
        ])
        if content.success:
            text = _decode_base64(content.stdout.strip())
            if text:
                if re.search(r"\[tool\.pytest", text) or re.search(r"pytest", text):
                    return "pytest"
                if re.search(r"\[tool\.unittest", text):
                    return "unittest"

    # Check package.json for JS test frameworks
    if "package.json" in top_files:
        content = await github.run_gh([
            "api", f"repos/{repo}/contents/package.json",
            "--jq", ".content",
        ])
        if content.success:
            text = _decode_base64(content.stdout.strip())
            if text:
                try:
                    pkg = json.loads(text)
                    all_deps = {
                        **pkg.get("devDependencies", {}),
                        **pkg.get("dependencies", {}),
                    }
                    for fw in ("vitest", "jest", "mocha"):
                        if fw in all_deps:
                            return fw
                except (json.JSONDecodeError, TypeError):
                    pass

    # Check for standalone config files
    for framework, markers in _TEST_INDICATORS.items():
        for marker in markers:
            if marker in top_files and framework not in ("pytest", "unittest"):
                return framework

    # Check for tests/ directory as a fallback signal for pytest
    if "tests" in top_files or "test" in top_files:
        return "pytest"  # most common default for Python

    return None


async def _detect_lint_tool(
    repo: str, top_files: set[str], github: GitHubCLIProtocol
) -> str | None:
    """Detect the lint tool from config files."""
    # Standalone config files first (highest confidence)
    if "ruff.toml" in top_files:
        return "ruff"
    if ".flake8" in top_files:
        return "flake8"
    if ".pylintrc" in top_files:
        return "pylint"
    if "biome.json" in top_files:
        return "biome"

    for eslint_file in (".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml"):
        if eslint_file in top_files:
            return "eslint"

    # Check pyproject.toml for ruff/flake8/pylint sections
    if "pyproject.toml" in top_files:
        content = await github.run_gh([
            "api", f"repos/{repo}/contents/pyproject.toml",
            "--jq", ".content",
        ])
        if content.success:
            text = _decode_base64(content.stdout.strip())
            if text:
                if re.search(r"\[tool\.ruff", text):
                    return "ruff"
                if re.search(r"\[tool\.flake8", text):
                    return "flake8"
                if re.search(r"\[tool\.pylint", text):
                    return "pylint"

    return None


def _decode_base64(encoded: str) -> str | None:
    """Decode base64 content from GitHub API, returning None on failure."""
    import base64

    try:
        # GitHub API base64 content may have newlines
        cleaned = encoded.replace("\n", "").replace("\\n", "")
        return base64.b64decode(cleaned).decode("utf-8", errors="replace")
    except Exception:
        return None
