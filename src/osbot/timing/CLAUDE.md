# osbot.timing

## Purpose

Human-like delays for all externally visible actions. Prevents the bot from appearing automated by adding realistic timing between internal completion and external action. This is Layer 0 -- no dependencies on any other osbot package. Pure async sleep with randomization.

## Key Interfaces

```python
class Humanizer:
    """Add human-like delays to externally visible actions."""

    async def delay_pr_creation(self) -> None:
        """Wait 15-45 minutes before creating a PR after implementation is ready."""

    async def delay_feedback_response(self) -> None:
        """Wait 30 min - 4 hours before responding to maintainer feedback."""

    async def delay_claim_comment(self) -> None:
        """Wait 0-2 minutes before posting a claim comment.
        Short delay -- claiming quickly is natural human behavior."""

    async def delay_engagement(self) -> None:
        """Wait 5-15 minutes before posting a comment on an issue."""

    @staticmethod
    def jitter(base_seconds: float, variance: float = 0.3) -> float:
        """Return base_seconds +/- variance as a random float."""
```

## Dependencies

None from osbot packages. Uses only `asyncio.sleep` and `random`.

## Internal Structure

- **`humanizer.py`** -- `Humanizer`. Single file, single class. Each `delay_*` method calls `asyncio.sleep` with a randomized duration. Durations are tuned to mimic realistic human behavior: a PR takes 15-45 minutes because a human would be reviewing their own work, writing the description, taking a break. Feedback responses take 30 min - 4 hours because humans don't sit watching for PR reviews. Claim comments are near-instant (0-2 min) because eagerly claiming an issue is normal. All delays use `jitter()` to avoid exact round numbers.

## How to Test

```python
async def test_pr_delay_in_range():
    h = Humanizer()
    start = asyncio.get_event_loop().time()
    # In tests, mock asyncio.sleep to record the requested duration
    with patch("asyncio.sleep") as mock_sleep:
        mock_sleep.return_value = None  # Don't actually sleep
        await h.delay_pr_creation()
        delay = mock_sleep.call_args[0][0]
        assert 15 * 60 <= delay <= 45 * 60  # 15-45 minutes in seconds

def test_jitter_within_variance():
    for _ in range(100):
        j = Humanizer.jitter(600, variance=0.3)
        assert 420 <= j <= 780  # 600 +/- 30%
```

- Always mock `asyncio.sleep` in tests. Never actually sleep.
- Test that delay ranges are correct.
- Test jitter distribution stays within bounds.

## Design Decisions

1. **Layer 0 with no dependencies.** Timing is a leaf module. It should never import from gateway, state, or any other package. This makes it trivially testable and impossible to create circular dependencies.

2. **Claim comments have near-zero delay.** In v3, claims had the same delay as PRs. But humans claim issues quickly -- it's a race. A 30-minute delay on a claim means someone else gets assigned first.

3. **Feedback response delay is wide (30 min - 4 hours).** The wide range prevents a recognizable pattern. If the bot always responded in exactly 32 minutes, that would be detectable. The 30m-4h range covers "saw it right away" to "was busy, got to it later."

4. **No configuration for delays.** The ranges are hardcoded because they represent behavioral design decisions, not operational parameters. Changing them requires understanding the anti-detection rationale, not just editing a config file.
