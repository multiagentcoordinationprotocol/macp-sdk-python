"""``llm_evaluator``: a LangChain chat model as an MACP ``EvaluationStrategy``."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from macp_sdk.agent import EvaluationResult, SessionInfo

from .node import normalize_recommendation

DEFAULT_SYSTEM_PROMPT = (
    "You are one participant in a binding multi-agent decision. Evaluate the proposal "
    "you are given. Answer with a single JSON object and nothing else: "
    '{"recommendation": "APPROVE" | "REJECT" | "REVIEW" | "BLOCK", '
    '"confidence": <number from 0 to 1>, "reason": "<one sentence>"}.'
)
UNPARSEABLE = "unparseable model output"

_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```$", re.DOTALL)


def parse_evaluation(text: str) -> EvaluationResult:
    """Parse a model's JSON answer. Any defect gives ``REVIEW`` with confidence 0."""
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        recommendation = normalize_recommendation(data.get("recommendation"))
        confidence = data.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError("confidence out of range")
        reason = data.get("reason", "")
        return EvaluationResult(
            recommendation=recommendation,
            confidence=float(confidence),
            reason=reason if isinstance(reason, str) else json.dumps(reason),
        )
    except ValueError:
        return EvaluationResult(recommendation="REVIEW", confidence=0.0, reason=UNPARSEABLE)


class _LlmEvaluator:
    __slots__ = ("_model", "_system_prompt")

    def __init__(self, model: BaseChatModel, system_prompt: str) -> None:
        self._model = model
        self._system_prompt = system_prompt

    def evaluate(self, proposal: dict[str, Any], context: SessionInfo) -> EvaluationResult:
        question = {
            "session_id": context.session_id,
            "mode": proposal["mode"],
            "option": proposal["option"],
            "rationale": proposal["rationale"],
            "details": proposal["details"],
        }
        answer = self._model.invoke(
            [
                SystemMessage(content=self._system_prompt),
                HumanMessage(content=json.dumps(question, default=str)),
            ]
        )
        return parse_evaluation(answer.text)


def llm_evaluator(model: BaseChatModel, *, system_prompt: str | None = None) -> _LlmEvaluator:
    """Wrap a LangChain chat model as an ``EvaluationStrategy``.

    The model is asked for ``{recommendation, confidence, reason}`` as JSON.
    A Markdown code fence around the answer is fine. ``ABSTAIN`` becomes
    ``REVIEW``. An answer that cannot be read becomes ``REVIEW`` with
    confidence 0 (reason ``"unparseable model output"``), so a bad answer
    never sinks the session.
    """
    return _LlmEvaluator(model, system_prompt or DEFAULT_SYSTEM_PROMPT)
