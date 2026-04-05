# osbot.comms

## Purpose

Generate all human-facing text: issue claim comments, PR descriptions (post-processing), feedback responses, and question answers. Enforces voice consistency and filters out AI-detectable phrases. This is Layer 2 -- depends on state (for maintainer profiles), no Claude calls. The generation itself is done by Claude calls in pipeline/iteration; comms handles post-processing and validation.

## Key Interfaces

```python
class CommentGenerator:
    """Post-process and validate all outgoing text."""

    def filter_banned_phrases(self, text: str) -> str:
        """Remove or rephrase any of the 40+ banned AI phrases."""

    def validate_pr_description(self, description: str) -> ValidationResult:
        """Check: >= 2 code references, no banned phrases, has test output, structural variation."""

    def generate_claim_comment(self, issue: Issue, repo_policy: RepoPolicy) -> str:
        """Generate a natural-language claim comment. Template-based, no Claude needed."""

    def generate_thank_you(self, maintainer: str) -> str:
        """Short thank-you for rejection. Template-based, varied."""

    def apply_style_seed(self, text: str, seed: int) -> str:
        """Randomize structural ordering (what/why/how) for anti-detection."""
```

### Banned Phrase Categories (40+)

```
Certainty markers:  "I'd be happy to", "Certainly!", "Absolutely!", "Great question"
Filler hedges:      "It's worth noting that", "It's important to note"
AI signatures:      "As an AI", "As a language model", "I don't have personal"
Over-formality:     "I hope this helps", "Please don't hesitate", "Feel free to"
Excessive praise:   "Great catch!", "Excellent question!", "That's a great point"
Meta-commentary:    "Let me", "I'll", "Allow me to", "I've gone ahead and"
```

## Dependencies

- `osbot.config` -- banned phrase list (or hardcoded constant)
- `osbot.types` -- `Issue`, `RepoPolicy`, `ValidationResult`
- `osbot.state` -- `MemoryDB` (maintainer profiles for tone calibration)
- `osbot.log` -- structured logging

## Internal Structure

- **`comment_generator.py`** -- `CommentGenerator`. The core class. Contains the banned phrase list as a constant. `filter_banned_phrases()` does regex-based replacement: some phrases are removed entirely, others are rephrased to sound human ("I'd be happy to help" -> removed, "It's worth noting" -> removed, leaving just the content). `validate_pr_description()` ensures Claude's output meets structural requirements: at least 2 concrete code references (file paths or function names), test output snippet present, no banned phrases, `Closes #N` present. `generate_claim_comment()` uses templates with slot-filling (issue title, what the bot plans to do) -- no Claude needed for claims. `apply_style_seed()` randomizes the ordering of sections (problem/approach/testing vs approach/problem/testing) to prevent all PRs from having identical structure.

## How to Test

```python
def test_banned_phrase_removal():
    gen = CommentGenerator()
    text = "I'd be happy to help fix this. It's worth noting that the bug is in parser.py."
    filtered = gen.filter_banned_phrases(text)
    assert "I'd be happy to" not in filtered
    assert "It's worth noting" not in filtered
    assert "parser.py" in filtered

def test_pr_description_requires_code_refs():
    gen = CommentGenerator()
    result = gen.validate_pr_description("This fixes the bug by changing the logic.")
    assert not result.valid
    assert "code reference" in result.reason

def test_pr_description_passes_with_refs():
    gen = CommentGenerator()
    desc = "Fixed `parse_input()` in `src/parser.py`. The condition on line 42 was inverted."
    result = gen.validate_pr_description(desc)
    assert result.valid

def test_style_seed_varies_structure():
    gen = CommentGenerator()
    text = "## What\nfoo\n## Why\nbar\n## Testing\nbaz"
    v1 = gen.apply_style_seed(text, seed=1)
    v2 = gen.apply_style_seed(text, seed=2)
    assert v1 != v2  # Different orderings
```

- All comms functions are pure (no I/O) except maintainer profile lookups.
- Test banned phrase removal exhaustively -- every phrase in the list should have a test.
- Test style seed with multiple seeds to verify variation.

## Design Decisions

1. **Post-processing, not generation.** Claude generates the text (in pipeline/iteration calls). Comms filters and validates it. This keeps the banned phrase list in one place and applies it uniformly to all outgoing text.

2. **40+ banned phrases, not 5.** v3 started with 34 phrases and still had detectable patterns. The list is comprehensive and covers certainty markers, filler hedges, AI signatures, over-formality, excessive praise, and meta-commentary.

3. **Claim comments are template-based.** Claim comments are simple ("I'd like to work on this. I plan to [brief approach].") and don't need Claude. This saves a Claude call per assignment-required issue.

4. **Style seed for structural variation.** Without variation, every PR description follows the same What/Why/How template. The style seed randomizes section order and phrasing patterns so PRs from the bot don't have a recognizable structural fingerprint.

5. **Specificity validation.** PR descriptions with zero concrete code references ("Fixed the bug by improving the logic") are rejected by `validate_pr_description()`. At least 2 references (file paths, function names, line numbers) are required. This forces Claude to produce substantive descriptions.
