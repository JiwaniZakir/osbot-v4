# osbot.discovery

## Purpose

Find repos and issues worth contributing to, using zero Claude calls. This is the entry point for the contribution pipeline: it builds a dynamic pool of repos from GitHub search, enriches them with external signals, scores issues, and pushes ranked issues into the state queue. Everything here is arithmetic and GitHub API calls -- no LLM involvement.

## Key Interfaces

```python
class RepoFinder:
    """GitHub search -> candidate repos."""

    async def search(self) -> list[RepoCandidate]:
        """Search GitHub for repos matching language, topic, stars, and recency filters."""


class RepoScorer:
    """Signal-based repo scoring. No Claude calls."""

    async def score(self, repo: RepoCandidate, signals: RepoSignals) -> float:
        """Score 0-10 based on external merge rate, response time, CI, policy, etc."""


class RepoSignalCollector:
    """Compute external signals for a repo."""

    async def collect(self, repo: str) -> RepoSignals:
        """Fetch merge rate, response time, close-completion rate, CI presence, policies.
        Cached in memory.db with 7-day TTL."""


class IssueFinder:
    """GraphQL-enriched issue search."""

    async def find(self, repos: list[str]) -> list[IssueCandidate]:
        """Search for open issues, enrich with GraphQL (labels, comments, timeline)."""


class IssueScorer:
    """v2 scoring with maintainer confirmation and blended rates."""

    def score(self, issue: IssueCandidate, repo_signals: RepoSignals,
              outcomes: list[Outcome], lessons: list[RepoFact]) -> float:
        """score = 5.0 + repo_adj + label_adj + quality_adj + lesson_adj, clamped 1-10."""
```

### Issue Scoring Formula

```
score = 5.0 + repo_adj + label_adj + quality_adj + lesson_adj    (clamped 1-10)

repo_adj   [-2, +2]   Blended merge rate (our history + external rate)
label_adj  [-1, +1]   Label-level merge rate from outcomes table
quality_adj [0, +3.5]  Maintainer confirmed: +1.50, error trace, code block,
                       regression flag, comment count 1-5, reactions >= 5
lesson_adj [-3, 0]     Negative lessons for this repo from memory.db
```

## Dependencies

- `osbot.config` -- search keywords, domain keywords, allowed languages, star range, freshness thresholds
- `osbot.types` -- `RepoCandidate`, `IssueCandidate`, `RepoSignals`, `Outcome`, `RepoFact`
- `osbot.state` -- `MemoryDB` (read/write repo_signals, repo_facts, outcomes; check repo_bans)
- `osbot.state` -- `BotState` (push scored issues into queue)
- `osbot.intel` -- `GraphQLClient` (issue enrichment), `PolicyReader` (CONTRIBUTING.md parsing), `DuplicateDetector`
- `osbot.log` -- structured logging

## Internal Structure

- **`repo_finder.py`** -- `RepoFinder`. Runs `gh search repos` with filters: `language:Python|TypeScript`, topic keywords (ai, llm, ml, rag, agent, transformer, ...), stars 200-30k, pushed within 30 days. Deduplicates against already-known repos. Returns raw candidates for signal enrichment.

- **`repo_signals.py`** -- `RepoSignalCollector`. For each candidate repo, computes: `external_merge_rate` (merged PRs / total closed PRs in last 90 days), `avg_response_hours` (time to first maintainer comment), `close_completion_rate`, CI presence (checks for `.github/workflows/`), and assignment requirements. Results cached in `memory.db.repo_signals` with 7-day TTL. All data from `gh` CLI and GraphQL -- no Claude.

- **`repo_scorer.py`** -- `RepoScorer`. Scores repos 0-10 from signals. Auto-excludes: no-AI-policy repos, >100K stars, >50% closed-without-merge rate, security-sensitive repos. Threshold: score >= 4.0 to enter active pool (capped at 100 repos).

- **`issue_finder.py`** -- `IssueFinder`. For repos in the active pool, searches for open issues matching keywords (typo, broken link, missing import, etc.). Enriches each issue via GraphQL: labels, comment count, reactions, timeline events (especially MEMBER/OWNER comments for maintainer confirmation detection).

- **`issue_scorer.py`** -- `IssueScorer`. Applies the 4-adjustment formula. Maintainer confirmation (+1.50) is the strongest single quality signal -- an issue where a MEMBER/OWNER commented "confirmed bug" or similar is prioritized above all else.

## How to Test

```python
async def test_issue_scoring_maintainer_confirmed():
    issue = IssueCandidate(repo="a/b", number=1, maintainer_confirmed=True, ...)
    signals = RepoSignals(external_merge_rate=0.6, ...)
    scorer = IssueScorer()
    score = scorer.score(issue, signals, outcomes=[], lessons=[])
    assert score >= 6.5  # Base 5.0 + 1.50 confirmation

async def test_lesson_penalty():
    lessons = [RepoFact(key="lesson_1", value="always rejected", ...)]
    scorer = IssueScorer()
    score = scorer.score(issue, signals, outcomes=[], lessons=lessons)
    assert score < 5.0  # Penalty applied

async def test_repo_excluded_for_ai_policy():
    signals = RepoSignals(has_ai_policy=True, ...)
    scorer = RepoScorer()
    score = await scorer.score(candidate, signals)
    assert score == 0.0  # Excluded
```

- Mock `gh` CLI calls with `asyncio.create_subprocess_exec` patches returning canned JSON.
- Mock GraphQL responses for issue enrichment.
- Test scoring with synthetic `RepoSignals` and `Outcome` data -- pure arithmetic, no I/O.

## Design Decisions

1. **Zero Claude calls.** Discovery is the highest-volume phase. Using Claude here would burn the entire token budget on triage, not contribution. All scoring is arithmetic on GitHub signals.

2. **Dynamic repo pool, not a hardcoded list.** v3 had hardcoded repos that led to 80% off-domain attempts. The pool rebuilds from search every 30 minutes, with repos entering and exiting based on score.

3. **Maintainer confirmation is king.** The +1.50 bonus for maintainer-confirmed issues is the single largest quality adjustment. An issue where a maintainer said "this is a real bug" has fundamentally higher merge probability than an issue with no maintainer engagement.

4. **Blended merge rate.** For repos with bot history, the issue score blends our actual merge rate with the repo's external merge rate. For new repos (no history), only the external rate is used. This prevents the bot from over-indexing on repos it has never successfully contributed to.

5. **7-day TTL on signals.** Repo signals (merge rate, response time) change slowly. Recomputing every search would waste API calls. 7-day caching balances freshness with efficiency.

6. **Active pool cap at 100 repos.** Prevents the bot from spreading too thin. Quality over quantity -- better to deeply know 50 repos than superficially know 500.
