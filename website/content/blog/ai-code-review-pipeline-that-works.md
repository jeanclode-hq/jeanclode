---
title: "Why JeanClode's Code Review Runs Seven Agents, Not One"
description: "How JeanClode's code review workflow splits the job across seven agents (IssueExplorer, dual Analyzers, Synthesizer, Deduplicator, FactChecker, Guardrail, Styler) so what reaches your PR has already survived six rounds of filtering."
publishedAt: "2025-06-30"
readingTime: 9
seoTitle: "AI Code Review Pipeline: 7 Agents, Zero Noise"
seoDescription: "How JeanClode's code review runs seven agents in sequence: explorer, dual analyzers, synthesizer, deduplicator, fact-checker, guardrail and styler."
---

Most AI code review tools produce a lot of comments. Most of those comments are noise: obvious observations, stylistic opinions, things the linter already caught, findings about code that isn't even in the diff. Teams learn to ignore them, and once a review tool trains you to skim past it, it's stopped doing its job.

JeanClode's code review workflow starts from the opposite premise: fewer comments, higher confidence. Seven agents run in sequence, and each one's job is to remove findings that don't pass its criterion. What reaches your PR is whatever survived all seven.

## What a single pass gets wrong

Point one LLM call at a diff and it will produce findings. It will also produce comments on code outside the diff ("this pre-existing pattern is risky", true, but not this PR's problem), things the test suite already catches, duplicates of threads already in the PR, suggestions the repo's own test files already contradict, and stylistic rewrites dressed up as bugs.

On a large diff, a single pass often gets the signal-to-noise ratio below half. Developers stop reading and start clicking resolve. At that point the tool is overhead, not help.

The fix isn't a better prompt. It's a different shape entirely: specialized agents, each doing one job, each filtering what the last one produced.

## The seven stages

### 1. IssueExplorer

Before reading a line of diff, the IssueExplorer fetches the linked GitHub issue or GitLab work item from the PR description.

A PR that says "fixes the race condition in the session manager" carries context the diff alone doesn't. The IssueExplorer reads the issue and pulls out what problem it's solving, what constraints came up, what approaches were tried and dropped. Every later agent works from that context, not just the code.

A "fix" the issue thread already says was tried and abandoned won't survive: the FactChecker catches it as contradicted by documented intent.

### 2. Analyzer × 2, in parallel

Two Analyzer agents get the same input, the diff, the PR description, and the issue context, and run independently, with no shared state and no knowledge of each other.

Each produces its own list of findings: bugs, security concerns, missing error handling, logic errors, performance regressions. Two independent reads catch more than one, not because either model is weak but because long diffs run into attention limits. The Synthesizer sees both lists afterward and keeps whatever survives.

This is the stage that most resembles an actual code review: two reviewers reading the same diff without comparing notes first.

### 3. Synthesizer

The Synthesizer takes both Analyzer outputs plus the full context and merges them: it deduplicates near-identical findings, assigns each one a confidence score, and anchors it to a specific file and line in the diff.

The output is a typed list of comment objects (file, line, body, confidence), not prose. Every stage after this one filters that list programmatically instead of re-reading paragraphs.

### 4. Deduplicator

The Deduplicator compares the synthesized findings against the PR's existing discussion thread, pulled from GitHub's or GitLab's API, and drops anything already raised. If a human reviewer already flagged the missing null check on line 47, posting it again adds nothing.

This is what makes the pipeline useful on a PR a human has already started reviewing: it doesn't repeat what's already been said. When there's no existing discussion, this stage is a pass-through.

### 5. FactChecker

This is the highest-value filter in the pipeline, and the most expensive: it checks each remaining finding against the real repository, not just the diff, using file reads and grep.

The pattern it catches most often: "this function doesn't handle the empty case" when a helper it calls does; "this variable is never used" when it's used in a file the diff doesn't touch; "this needs error handling" when a global error boundary already covers it; a recommendation the issue thread already ruled out for a documented reason. Anything that can't be verified against the codebase gets removed here, which is the main reason the false-positive rate stays low.

### 6. Guardrail

The Guardrail is the one stage with no model in it. It scans every surviving comment body for secrets and drops anything that trips the scanner.

That's deliberate. An agent that just read your codebase to verify a finding is holding, in context, whatever it read, and quoting the wrong line back at you in a public PR comment is a real way for a credential to leak. That's not a call worth leaving to a model that could be argued out of it, so it's a deterministic check instead.

Everything upstream of this handles judgment: the Analyzers are told what to skip, the FactChecker drops what it can't verify, and severity lives on the finding itself. Only CRITICAL (a crash, a security hole, data loss), HIGH (a broken feature or a common failure path), and MEDIUM (an edge case that shows up in production) exist. There's no LOW, because a bucket for "technically true, not worth reading" is a bucket that fills up and gets ignored.

### 7. Styler

The Styler normalizes tone and format on the final list: consistent verb tense, no "you should have" turned into "consider", code blocks formatted for the target platform, line references matched to the diff format the comment API expects.

It doesn't add or remove anything. It just makes what survived read well.

## Posting

Comments go up as inline review comments through the GitHub or GitLab API. If every finding got filtered out along the way, the pipeline posts a single LGTM instead, so a clean review reads as a clean signal, not silence that might mean the tool never ran.

If an inline comment gets rejected, most often because its line isn't part of what the diff API will accept comments on, it falls back to a top-level thread with the file and line reference in the body rather than getting dropped.

On a typical 200-line diff across three files, the two Analyzers produce around 15 findings each, the Synthesizer merges that to roughly a dozen unique ones, the Deduplicator clears a couple if there's existing discussion, the FactChecker removes three to five that don't hold up, and four to six reach your PR after the Styler. That's a ratio worth trusting.

## What happens on a PR JeanClode opened itself

Everything above describes reviewing a human's PR. On a PR the bot opened, a Sentry fix or an issue resolution, review is one turn of a loop rather than the end of the line.

If findings remain, the workflow posts its inline comments and then one more: `@jeanclode-bot handle all the comments above, verify each isn't a false positive or already resolved first. If it's a non-issue or already fixed, resolve the thread directly. Otherwise, fix it, then resolve the thread.` That mention dispatches the respond workflow, which works through the threads. Whatever it pushes re-triggers this pipeline against the new commit, and the loop continues.

It converges one of two ways: review comes back clean and posts LGTM, or a respond turn resolves the last open thread without pushing anything, because every remaining finding turned out to be a false positive, so there's no new commit to re-review and no LGTM ever coming to say so. Both paths end the same way: the people your org configured to be notified get @-mentioned in a comment, once, guarded by a marker so whichever exit fires second doesn't double it up.

The self-mention that kicks off the sweep only ever fires when the PR's author is the bot, and the respond planner is explicitly forbidden from emitting it on its own. That's the whole loop guard: there's exactly one door into this cycle, and it isn't a model's call.

Both Analyzers and the FactChecker also carry cross-run memory here: a convention a human corrected on a past review of this repo doesn't get re-flagged and re-argued on the next one.

## Failure handling

Each agent's failure is isolated. If the Deduplicator fails, say the discussion API is down, the pipeline continues with the Synthesizer's output instead of stalling the whole review. If the FactChecker fails, findings are kept rather than dropped. If the Styler fails, the originals post unstyled.

The fallbacks all lean the same direction: a false positive is a better failure mode than a real bug silently disappearing.

## Running it

```bash
# On a GitHub PR
jeanclode https://github.com/org/repo/pull/123

# On a GitLab MR
jeanclode https://gitlab.com/group/project/-/merge_requests/42

# Dry run: don't post, print findings to the terminal
jeanclode https://github.com/org/repo/pull/123 --dry-run
```

The CLI detects the platform from the URL, resolves auth from the standard CLI token store (`gh auth token` / `glab auth token`), and runs the full seven-agent pipeline. `--dry-run` runs everything up through the posting step and prints what would be posted, which is the way to see it before wiring it to a webhook.

## Self-hosting and code privacy

The diff and surrounding code are read inside the container running the CLI. None of it passes through JeanClode's backend, which only ever stores issue IDs, statuses, and PR URLs. Self-hosted, the only external system that sees your code is your own LLM provider.

For teams triggering JeanClode from GitHub Actions or GitLab CI, the runner is your own CI infrastructure. The agent runs where your code already is.

---

The full workflow source is on [GitHub](https://github.com/jeanclode-hq/jeanclode).
