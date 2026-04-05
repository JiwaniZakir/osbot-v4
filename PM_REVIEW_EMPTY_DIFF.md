# PM Review: Empty Diff Fix

**Reviewer:** PM Review (automated)
**Date:** 2026-03-27
**Status: APPROVE_WITH_NOTES**

---

## Summary

This change introduces a three-layer defense against "empty diff" failures:

1. **Scoring layer** (`issue_scorer.py`): New `implementability_adj` [-3.5, 0.0] penalizes feature requests, investigation tasks, discussion issues, and long prose without code/error traces.
2. **Discovery layer** (`issue_finder.py`): Pre-filters feature/enhancement labels before GraphQL enrichment and adds negative scoring in `_quick_score` for non-implementable issues.
3. **Implementation layer** (`implementer.py`): "MUST commit something" instruction in the TASK section.
4. **Pipeline layer** (`run.py`): Diagnostic empty-diff messages that classify WHY the diff was empty based on tool call count.

The approach is sound overall. The scoring and filtering changes should meaningfully reduce the 80%+ empty diff rate by steering the bot away from issues it cannot produce code for. Below are specific concerns and recommendations.

---

## 1. Effectiveness -- Will These Changes Reduce Empty Diffs?

**Yes, significantly.** The layered approach is well-designed:

- **Pre-filter in `issue_finder.py` (lines 182-199):** Dropping feature/enhancement/proposal/rfc issues BEFORE spending GraphQL budget is smart. These never produce mergeable fixes. Saves API calls and eliminates the worst offenders early.

- **`_quick_score` negatives in `issue_finder.py` (lines 267-285):** The -3.0 for feature labels, -2.0 for investigation keywords, and -2.0 for "add support for" / "implement" phrasing will strongly deprioritize these during pre-scoring. Well-calibrated: these penalties are large enough to push non-implementable issues below implementable ones even when the non-implementable issue has positive signals (e.g., code blocks, bug label).

- **`implementability_adj` in `issue_scorer.py` (lines 321-369):** The four-part penalty is well-structured. Feature requests (-2.0) and investigation keywords (-1.5) are the most aggressive, which matches the data (these categories have the highest empty-diff rate). The -3.5 cap prevents any single issue from being penalized to the floor by implementability alone.

**Concern:** The pre-filter in `issue_finder.py` (line 184) and the penalty in `issue_scorer.py` (line 308) define the same label set independently (`_SKIP_LABELS` vs `_FEATURE_LABELS`). If one is updated and the other is not, they will drift. Consider extracting to a shared constant or referencing the same source.

---

## 2. Safety -- Could Good Issues Be Accidentally Rejected?

### 2a. "help wanted" in `_DISCUSSION_LABELS` (issue_scorer.py line 318)

**This is the highest-risk item.** The label "help wanted" is one of the three `_TARGET_LABELS` in `issue_finder.py` (line 42) -- it is a primary search label. Putting it in `_DISCUSSION_LABELS` means any issue with "help wanted" but NOT "bug" gets a -1.0 penalty. Many genuinely fixable issues carry "help wanted" without "bug" -- e.g., "help wanted" + "good first issue" for a typo fix.

**Impact:** An issue with labels ["help wanted", "good first issue"] and title "Fix typo in README" would receive a -1.0 implementability penalty even though it is the exact kind of issue the bot excels at. Base(5.0) + quality(+1.0 for typo) + implementability(-1.0) = 5.0 instead of 6.0.

**Recommendation:** Remove "help wanted" from `_DISCUSSION_LABELS`. It is an invitation to contribute, not a discussion marker. If you want to penalize "help wanted" alone (no bug, no good-first-issue, no typo), add that as a separate check with a smaller penalty (-0.5).

### 2b. Investigation keywords in body (issue_scorer.py lines 348-349)

Checking the full body for keywords like "analyze", "look into", and "figure out" risks false positives. A maintainer might write "I looked into this and found the bug is in parser.py" -- the issue is perfectly implementable but contains "look into" in the body.

**Impact:** Moderate. The -1.5 penalty is aggressive for a body keyword match. Title matches are much stronger signals.

**Recommendation:** Either (a) apply the full -1.5 only for title matches and reduce body matches to -0.75, or (b) require 2+ investigation keywords in the body before applying the penalty.

### 2c. "explore" in `_INVESTIGATION_TITLE_KW` (issue_finder.py line 277)

The keyword "explore" is too broad. Issues like "Explore button not working" or "File explorer crashes" would be penalized. The current issue_scorer version uses "explore why" which is more specific.

**Impact:** Low but unnecessary. Could filter out legitimate bug reports about UI exploration features.

**Recommendation:** Change "explore" to "explore why" in the issue_finder's `_INVESTIGATION_TITLE_KW` to match the scorer's more specific version.

---

## 3. Side Effects

### 3a. Tests: All 76 tests pass

```
76 passed in 0.38s
```

The existing `test_base_score_is_5` test still passes because the bare issue data (`labels=[], title="some issue"`, empty body) triggers none of the implementability penalties: no feature labels, no investigation keywords in "some issue", no discussion labels, and no long prose body. The new adjustment returns 0.0 for this case, preserving the base score of 5.0.

### 3b. No test coverage for `_compute_implementability_adj`

The test suite imports `_compute_repo_adj`, `_compute_lesson_adj`, `_compute_quality_adj`, and `_compute_benchmark_adj` but does NOT import or test `_compute_implementability_adj`. This is a gap -- the most impactful new function has zero test coverage.

**Recommendation:** Add tests for:
- Feature label penalty (-2.0)
- Investigation keyword in title (-1.5)
- Discussion label without bug (-1.0)
- Discussion label WITH bug (should be 0.0)
- Long prose without code/trace (-1.0)
- Cumulative cap at -3.5
- "help wanted" + "bug" should NOT be penalized (regression test for 2a)

### 3c. Interaction with reflections, prompt variants, and meta-lessons

No conflict. The implementability adjustment is purely in the scoring layer (Layer 4 discovery), while reflections, prompt variants, and meta-lessons are in the implementation prompt (Layer 4 pipeline). They operate at different stages:
- Implementability filters WHICH issues are selected (discovery)
- Reflections/variants affect HOW selected issues are implemented (pipeline)

These are complementary, not competing.

### 3d. Interaction with the cleanup/docs bonus (issue_scorer.py lines 192-218)

Potential positive interaction: cleanup issues get +1.50 from quality_adj and 0.0 from implementability_adj (they don't match feature/investigation/discussion patterns). This means cleanup issues are now relatively even MORE favored compared to feature requests, which is correct behavior since cleanup issues have the highest merge rate (84.7%).

---

## 4. Completeness -- What's Missing?

### 4a. No feedback loop for empty diff outcomes

The empty diff diagnostic in `run.py` (lines 192-217) produces useful failure reasons, but there is no mechanism to feed these back into scoring. If issue X produces an empty diff, the outcome is recorded, but the implementability_adj does not learn from it. The lesson_adj path requires Claude-analyzed comments, which empty diffs don't produce.

**Recommendation (future):** Consider adding a lightweight signal: if an issue produces an empty diff, increment a counter for its label combination in the DB. Issues with label combos that have historically produced empty diffs would get an additional penalty. This is pure arithmetic, no Claude cost.

### 4b. No penalty for issues requiring external service changes

Issues that say "the API response changed" or "the upstream dependency updated" often require changes outside the repo. These produce empty diffs because the bot can't fix external dependencies. Not addressed here.

### 4c. The pre-filter in issue_finder removes features BEFORE enrichment, but the scorer also penalizes them

This is defense-in-depth (good), but it means the scorer's feature penalty (line 341) will rarely fire in practice because those issues are already filtered out. This is fine for correctness but means the scorer penalty is mostly a safety net for issues that slip through non-label-based discovery.

---

## 5. Risk Assessment -- "MUST commit something" Instruction

**This is the second-highest risk item after the "help wanted" issue.**

The instruction at `implementer.py` lines 81-84:

```
CRITICAL: You MUST make at least one code change and commit it. If you cannot
find a clear fix after reading the relevant files, make the smallest improvement
you can identify (fix a typo, add a missing type hint, improve an error message)
and commit that. An empty diff is NEVER acceptable.
```

**Risks:**

1. **Garbage commits:** Claude may add a spurious type hint or docstring to an unrelated file just to satisfy the "MUST commit" requirement. The FORBIDDEN section says "Do NOT add docstrings to unchanged code" but the CRITICAL section says "add a missing type hint" -- these instructions conflict. Claude will likely prioritize CRITICAL over FORBIDDEN.

2. **Scope creep from desperation:** If Claude can't find the real fix, it may make a tangentially related change (e.g., fixing a typo in a nearby file) that the critic then rejects for scope. This burns 2 more Claude calls (critic + retry) on a doomed attempt.

3. **The critic is the safety net:** The hard gate critic (call #2) should catch garbage commits. But every garbage commit that reaches the critic costs ~1500 tokens for nothing. With a 60%+ empty-diff rate reduced to maybe 30%, that's still significant waste.

**Mitigating factors:**
- The FORBIDDEN section (lines 86-104) explicitly prohibits most junk changes
- The time limit instruction (lines 102-104) tells Claude to simplify after 3 minutes / 5 files
- The critic is a hard gate and will reject spurious changes

**Recommendation:** Soften the instruction. Instead of "An empty diff is NEVER acceptable," use:

```
IMPORTANT: Strongly prefer making a concrete code change and committing it.
If after reading the relevant files you cannot identify a fix, make the smallest
correct improvement you can find (fix a typo in a nearby file, add a missing
type annotation). Only give up with no commit if you genuinely cannot find any
improvement to make.
```

This preserves the intent (push Claude toward committing) without the absolute mandate that could produce junk.

---

## Summary of Recommendations

| Priority | Item | Location | Action |
|----------|------|----------|--------|
| HIGH | Remove "help wanted" from `_DISCUSSION_LABELS` | issue_scorer.py:318 | Prevents penalizing the bot's primary target issues |
| HIGH | Soften "MUST commit" to strong preference | implementer.py:81-84 | Prevents garbage commits when no fix exists |
| MEDIUM | Add test coverage for `_compute_implementability_adj` | tests/test_scoring.py | Prevents regressions in the most impactful new function |
| MEDIUM | Reduce body-only investigation penalty to -0.75 | issue_scorer.py:348-349 | Reduces false positives from maintainer comments |
| LOW | Change "explore" to "explore why" in `_INVESTIGATION_TITLE_KW` | issue_finder.py:277 | Consistency with scorer; prevents "file explorer" false positives |
| LOW | Extract shared feature label constants | issue_finder.py:184 + issue_scorer.py:308 | Prevents drift between pre-filter and scorer |

---

## Test Results

```
76 passed in 0.38s
```

All existing tests pass. No regressions detected. The new `implementability_adj` returns 0.0 for the bare issue in `test_base_score_is_5`, preserving the expected score of 5.0.
