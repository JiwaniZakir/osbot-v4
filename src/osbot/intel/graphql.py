"""GraphQL client -- shared GitHub API v4 access via ``gh api graphql``.

All queries go through ``GitHubCLIProtocol.graphql()``.  This module
provides typed, higher-level methods on top of raw GraphQL so consumers
never write query strings directly.

Zero Claude calls.  Layer 2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from osbot.log import get_logger

if TYPE_CHECKING:
    from osbot.types import GitHubCLIProtocol

logger = get_logger(__name__)


class GraphQLClient:
    """Shared GraphQL client wrapping ``gh api graphql``.

    All methods are async and return parsed dicts / lists.
    Raises ``RuntimeError`` on transport failures (via the underlying
    ``GitHubCLIProtocol.graphql``).
    """

    def __init__(self, github: GitHubCLIProtocol) -> None:
        self._gh = github

    # ------------------------------------------------------------------
    # Issue detail (comments + timeline)
    # ------------------------------------------------------------------

    async def issue_detail(
        self, owner: str, repo: str, number: int
    ) -> dict[str, Any]:
        """Fetch an issue with comments, timeline events, reactions, and author associations.

        Returns the ``repository.issue`` node from the GraphQL response.
        """
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) {
            issue(number: $number) {
              number
              title
              body
              state
              createdAt
              updatedAt
              author { login }
              labels(first: 20) { nodes { name } }
              reactions { totalCount }
              comments(first: 50) {
                nodes {
                  author { login }
                  authorAssociation
                  body
                  createdAt
                }
              }
              timelineItems(first: 100, itemTypes: [
                CROSS_REFERENCED_EVENT,
                ASSIGNED_EVENT,
                LABELED_EVENT,
                CLOSED_EVENT
              ]) {
                nodes {
                  __typename
                  ... on CrossReferencedEvent {
                    source {
                      ... on PullRequest {
                        number
                        state
                        author { login }
                        url
                      }
                    }
                  }
                  ... on AssignedEvent {
                    assignee { ... on User { login } }
                  }
                  ... on LabeledEvent {
                    label { name }
                    createdAt
                  }
                  ... on ClosedEvent {
                    createdAt
                  }
                }
              }
            }
          }
        }
        """
        data = await self._gh.graphql(
            query,
            variables={"owner": owner, "repo": repo, "number": number},
        )
        issue = data.get("data", {}).get("repository", {}).get("issue", {})
        if not issue:
            logger.warning("graphql_issue_not_found", owner=owner, repo=repo, number=number)
            return {}
        return issue  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Repo health (recent PRs + issues + activity)
    # ------------------------------------------------------------------

    async def repo_health(self, owner: str, repo: str) -> dict[str, Any]:
        """Fetch recent PRs, issue counts, and contributor activity.

        Returns a dict with keys ``recent_prs``, ``open_issues_count``,
        ``has_ci``, ``default_branch``.
        """
        query = """
        query($owner: String!, $repo: String!) {
          repository(owner: $owner, name: $repo) {
            defaultBranchRef { name }
            hasIssuesEnabled
            issues(states: OPEN) { totalCount }
            pullRequests(last: 50, states: [MERGED, CLOSED]) {
              nodes {
                number
                state
                author { login }
                authorAssociation
                mergedAt
                closedAt
                createdAt
                commits(first: 1) {
                  nodes {
                    commit {
                      statusCheckRollup { state }
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = await self._gh.graphql(
            query, variables={"owner": owner, "repo": repo}
        )
        repo_data = data.get("data", {}).get("repository", {})
        if not repo_data:
            logger.warning("graphql_repo_not_found", owner=owner, repo=repo)
            return {}

        prs = repo_data.get("pullRequests", {}).get("nodes", [])

        # Detect CI from status checks on recent PRs
        has_ci = False
        for pr in prs:
            commits = pr.get("commits", {}).get("nodes", [])
            if commits:
                rollup = commits[0].get("commit", {}).get("statusCheckRollup")
                if rollup is not None:
                    has_ci = True
                    break

        return {
            "recent_prs": prs,
            "open_issues_count": repo_data.get("issues", {}).get("totalCount", 0),
            "has_ci": has_ci,
            "default_branch": (
                repo_data.get("defaultBranchRef", {}) or {}
            ).get("name", "main"),
        }

    # ------------------------------------------------------------------
    # PR comments (with optional ``since`` filter)
    # ------------------------------------------------------------------

    async def pr_comments(
        self, owner: str, repo: str, number: int, since: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch PR review comments and issue-style comments.

        Args:
            since: ISO 8601 timestamp.  If provided, only comments created
                   after this timestamp are returned.

        Returns a list of comment dicts with ``author``, ``body``,
        ``createdAt``, ``authorAssociation``.
        """
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $number) {
              comments(first: 100) {
                nodes {
                  author { login }
                  authorAssociation
                  body
                  createdAt
                }
              }
              reviews(first: 50) {
                nodes {
                  author { login }
                  authorAssociation
                  body
                  state
                  createdAt
                  comments(first: 50) {
                    nodes {
                      author { login }
                      authorAssociation
                      body
                      path
                      line
                      createdAt
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = await self._gh.graphql(
            query,
            variables={"owner": owner, "repo": repo, "number": number},
        )
        pr = data.get("data", {}).get("repository", {}).get("pullRequest", {})
        if not pr:
            return []

        comments: list[dict[str, Any]] = []

        # Issue-style comments
        for c in pr.get("comments", {}).get("nodes", []):
            comments.append(c)

        # Review-level comments (body of the review itself)
        for rev in pr.get("reviews", {}).get("nodes", []):
            if rev.get("body"):
                comments.append(rev)
            for rc in rev.get("comments", {}).get("nodes", []):
                comments.append(rc)

        # Filter by ``since`` if provided
        if since:
            comments = [c for c in comments if c.get("createdAt", "") > since]

        return comments
