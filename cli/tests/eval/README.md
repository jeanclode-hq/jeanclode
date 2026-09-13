# Eval Tests

LLM-driven evaluation of agent outputs using [DeepEval](https://docs.confident-ai.com/) with a Gemini judge.

## How to run

```bash
make eval
```

## Required environment variables

| Variable | Description |
|---|---|
| `GOOGLE_API_KEY` or `GEMINI_API_KEY` | API key for the Gemini judge model |

## How to add a new test case

1. Create a file under `cli/tests/eval/` (e.g. `test_triage_agent.py`).
2. Import the fixtures you need — `judge` and `run_agent` are available from `conftest.py`.
3. Write an async test function, decorated with `@pytest.mark.eval`:

```python
import pytest
from deepeval import assert_test
from deepeval.metrics import AnswerRelevancyMetric
from deepeval.test_case import LLMTestCase

from src.agents.sentry import TriageAgent, TriageInput

@pytest.mark.eval
async def test_triage_relevancy(judge, run_agent):
    output = await run_agent(TriageAgent, TriageInput(
        issue_id="1",
        sentry_url="https://sentry.io/issues/1",
        formatted="KeyError: 'user_id' in views.py line 42",
    ))
    test_case = LLMTestCase(input="...", actual_output=str(output))
    assert_test(test_case, [AnswerRelevancyMetric(model=judge)])
```

## Notes

- Eval tests are **excluded from `make test`** (CI unit tests) because they require a live LLM judge.
- Run them on demand with `make eval` before shipping changes to agent prompts.
