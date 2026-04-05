# osbot.iteration

## Purpose

Monitor open PRs and respond to maintainer feedback. This is where the bot earns trust: applying requested changes correctly, answering questions, and handling CI failures. Uses 0-2 Claude calls per PR per cycle (only when new feedback exists). Feedback responses are the highest priority in the entire system -- a maintainer waiting is the most valuable moment.

## Key Interfaces

```python
class PRMonitor:
    """Poll open PRs for new activity via GraphQL."""

    async def poll(self, open_prs: list[OpenPR]) -> list[PRUpdate]:
        """Check for new comments, reviews, CI status, merge status, conflicts.
        Returns only PRs with new activity since last check."""


class FeedbackReader:
    """Call #4: Classify maintainer feedback."""

    async def read(self, pr: OpenPR, new_comments: list[Comment]) -> FeedbackAction:
        """Classify: request_changes | style_feedback | question |
        approval_pending_minor | rejection_with_reason | ci_failure.
        Returns action items for the patch applier."""


class PatchApplier:
    """Call #5: Apply requested changes."""

    async def apply(self, pr: OpenPR, action: FeedbackAction, workspace: str) -> PatchResult:
        """Apply changes, run quality gates, push to same branch.
        Safety: max 3 rounds, no size growth >120%, stop on conflicts."""
```

### Feedback Type -> Action Matrix

```
request_changes        -> Apply changes, push, respond         (2 Claude calls)
style_feedback         -> Apply changes, push, respond         (2 Claude calls)
question               -> Answer with comment only             (1 Claude call)
approval_pending_minor -> Apply minor changes, push            (2 Claude calls)
ci_failure             -> Read logs, attempt fix, push         (2 Claude calls)
rejection_with_reason  -> Record lesson, thank them, stop      (1 Claude call)
```

## Dependencies

- `osbot.config` -- max iteration rounds, size growth limit, conflict handling
- `osbot.types` -- `OpenPR`, `PRUpdate`, `FeedbackAction`, `PatchResult`, `Comment`
- `osbot.state` -- `MemoryDB` (record outcomes, update maintainer profiles), `BotState` (track active PRs)
- `osbot.gateway` -- `GatewayProtocol` (calls #4 and #5, priority 1 for feedback responses)
- `osbot.intel` -- `GraphQLClient` (comment polling, review status, CI status)
- `osbot.comms` -- `CommentGenerator` (response comments, banned phrase filtering)
- `osbot.timing` -- `Humanizer` (30 min - 4 hour response delay)
- `osbot.log` -- structured logging

## Internal Structure

- **`pr_monitor.py`** -- `PRMonitor`. Each cycle, queries GraphQL for all open PRs created by the bot. Checks for: new comments (by timestamp > last_checked), new reviews, CI status changes, merge status, merge conflicts. Returns a `PRUpdate` only when there is new activity -- no updates means no Claude calls. Uses `osbot.intel.GraphQLClient`, not raw `gh` CLI, for efficient batched queries.

- **`feedback_reader.py`** -- `FeedbackReader`. Call #4. Takes new comments/reviews and classifies them into one of 6 action types. For `request_changes` and `style_feedback`, extracts specific action items (what to change, where, how). For `rejection_with_reason`, extracts the lesson to record. Model: sonnet. Timeout: 60s. This call is conditional -- only fires when new feedback exists.

- **`patch_applier.py`** -- `PatchApplier`. Call #5. Receives only the specific action items from the feedback reader. Checks out the PR branch, applies changes via Claude, runs quality gates (same as pipeline), pushes to the same branch. After pushing, posts a response comment (via comms) after a humanizer delay (30 min - 4 hours). Model: sonnet. Timeout: 120s.

### Safety Valves

- **Max 3 rounds.** After 3 iterations on the same PR, stop. Prevents infinite back-and-forth.
- **Size growth limit.** If iteration makes the PR larger than 120% of the original diff, stop. Scope creep in iteration is a red flag.
- **Merge conflicts.** Post a comment explaining the conflict and wait. Do not attempt to resolve merge conflicts automatically.
- **CI fix limit.** If a CI fix attempt fails on the first try, stop. Do not enter a fix-break-fix loop.
- **Response delay.** Always wait 30 min - 4 hours before posting a response. Immediate responses look automated.

## How to Test

```python
async def test_monitor_detects_new_comment(mock_graphql):
    mock_graphql.return_comments([Comment(author="maintainer", body="Please add a test", ...)])
    monitor = PRMonitor(graphql=mock_graphql)
    updates = await monitor.poll([OpenPR(repo="a/b", number=42, last_checked=old_ts)])
    assert len(updates) == 1
    assert updates[0].has_new_feedback

async def test_feedback_reader_classifies_rejection(mock_gateway):
    mock_gateway.responses["feedback_reader"] = AgentResult(
        success=True, text='{"type": "rejection_with_reason", "reason": "out of scope"}', ...)
    reader = FeedbackReader(gateway=mock_gateway)
    action = await reader.read(pr, [Comment(body="Thanks but this is out of scope")])
    assert action.type == "rejection_with_reason"

async def test_patch_applier_stops_at_3_rounds():
    applier = PatchApplier(gateway=mock_gateway, ...)
    pr = OpenPR(iteration_count=3, ...)
    result = await applier.apply(pr, action, workspace)
    assert result.stopped
    assert "max rounds" in result.reason
```

- Mock GraphQL responses for monitor tests.
- Mock gateway for feedback reader and patch applier.
- Test safety valves with synthetic PR state (high iteration count, large diffs).

## Design Decisions

1. **Feedback is highest priority.** Gateway priority 1. A maintainer who left feedback is actively engaged. Responding correctly and promptly (after the humanizer delay) is the single highest-value action the bot can take.

2. **Conditional Claude calls.** The monitor polls via GraphQL (free). Claude is only called when there is new feedback to process. If 10 PRs are open but none have new comments, this phase uses 0 Claude calls.

3. **Rejection is final.** When a maintainer rejects with a reason, the bot thanks them, records the lesson, and stops. No arguing, no re-submission, no "what if I fix X instead?" This is how a respectful contributor behaves.

4. **Response delay is mandatory.** Even if the bot could respond in 10 seconds, it waits 30 min - 4 hours. This is a core anti-detection measure and also a courtesy -- instant responses to code review feel inhuman.

5. **Size growth detection.** If iteration grows the PR beyond 120% of its original size, something is wrong. The bot stops rather than letting scope creep turn a 10-line fix into a 50-line refactor.
