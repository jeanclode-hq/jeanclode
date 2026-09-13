"""Gemini judge for DeepEval — thin DeepEvalBaseLLM wrapper around litellm."""

from __future__ import annotations

import os

import litellm
from deepeval.models.base_model import DeepEvalBaseLLM

_MODEL = "gemini/gemini-3.1-flash-lite"

_judge_singleton: GeminiJudge | None = None


class GeminiJudge(DeepEvalBaseLLM):
    def get_model_name(self) -> str:
        return _MODEL

    def load_model(self) -> None:
        return None

    def generate(self, prompt: str) -> str:
        resp = litellm.completion(
            model=_MODEL,
            messages=[{"role": "user", "content": prompt}],
            api_key=_api_key(),
        )
        return resp.choices[0].message.content  # type: ignore[index,union-attr]

    async def a_generate(self, prompt: str) -> str:
        resp = await litellm.acompletion(
            model=_MODEL,
            messages=[{"role": "user", "content": prompt}],
            api_key=_api_key(),
        )
        return resp.choices[0].message.content  # type: ignore[index,union-attr]


def _api_key() -> str:
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Set GOOGLE_API_KEY or GEMINI_API_KEY to run eval tests")
    return key


def get_judge() -> GeminiJudge:
    """Return the shared GeminiJudge singleton."""
    global _judge_singleton
    if _judge_singleton is None:
        _judge_singleton = GeminiJudge()
    return _judge_singleton
