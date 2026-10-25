from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from macp_sdk.agent import EvaluationResult, SessionInfo

from langgraph_macp import (
    DEFAULT_SYSTEM_PROMPT,
    UNPARSEABLE,
    ConsensusParticipant,
    MacpConsensusNode,
    llm_evaluator,
    parse_evaluation,
)

from .conftest import FakeClient

UNREADABLE = EvaluationResult("REVIEW", 0.0, UNPARSEABLE)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '{"recommendation": "approve", "confidence": 0.8, "reason": "fine"}',
            EvaluationResult("APPROVE", 0.8, "fine"),
        ),
        (
            '```json\n{"recommendation": "BLOCK", "confidence": 1, "reason": "fraud"}\n```',
            EvaluationResult("BLOCK", 1.0, "fraud"),
        ),
        (
            '```\n{"recommendation": "reject", "confidence": 0}\n```',
            EvaluationResult("REJECT", 0.0, ""),
        ),
        (
            '{"recommendation": "ABSTAIN", "confidence": 0.5, "reason": ["a", 1]}',
            EvaluationResult("REVIEW", 0.5, '["a", 1]'),
        ),
        ("not json", UNREADABLE),
        ("[1, 2]", UNREADABLE),
        ('{"recommendation": "YES", "confidence": 0.5}', UNREADABLE),
        ('{"recommendation": null, "confidence": 0.5}', UNREADABLE),
        ('{"recommendation": "APPROVE", "confidence": 2}', UNREADABLE),
        ('{"recommendation": "APPROVE", "confidence": -0.1}', UNREADABLE),
        ('{"recommendation": "APPROVE", "confidence": "0.5"}', UNREADABLE),
        ('{"recommendation": "APPROVE", "confidence": true}', UNREADABLE),
        ('{"recommendation": "APPROVE", "confidence": NaN}', UNREADABLE),
        ('{"recommendation": "APPROVE"}', UNREADABLE),
    ],
)
def test_parse_evaluation(text: str, expected: EvaluationResult) -> None:
    assert parse_evaluation(text) == expected


class Recording(FakeListChatModel):
    prompts: list[Any]

    def invoke(self, messages: Any, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        self.prompts.append(messages)
        return super().invoke(messages, *args, **kwargs)


def test_llm_evaluator_prompts_and_parses() -> None:
    model = Recording(
        responses=['{"recommendation": "APPROVE", "confidence": 0.7, "reason": "ok"}'], prompts=[]
    )
    evaluator = llm_evaluator(model)
    proposal = {
        "proposal_id": "p1",
        "option": "ship",
        "rationale": "tests pass",
        "details": {"n": 1},
        "mode": "macp.mode.decision.v1",
    }
    result = evaluator.evaluate(proposal, SessionInfo("sid", "macp.mode.decision.v1"))
    assert result == EvaluationResult("APPROVE", 0.7, "ok")
    system, human = model.prompts[0]
    assert system.content == DEFAULT_SYSTEM_PROMPT
    assert json.loads(human.content) == {
        "session_id": "sid",
        "mode": "macp.mode.decision.v1",
        "option": "ship",
        "rationale": "tests pass",
        "details": {"n": 1},
    }


def test_custom_system_prompt() -> None:
    model = Recording(responses=["garbage"], prompts=[])
    evaluator = llm_evaluator(model, system_prompt="be strict")
    proposal = {"option": "x", "rationale": "", "details": None, "mode": "m"}
    assert evaluator.evaluate(proposal, SessionInfo("s", "m")) == UNREADABLE
    assert model.prompts[0][0].content == "be strict"


def test_llm_evaluators_drive_the_node() -> None:
    client = FakeClient()

    def model(answer: str) -> FakeListChatModel:
        return FakeListChatModel(responses=[answer])

    participants = [
        ConsensusParticipant(
            "a", llm_evaluator(model('{"recommendation":"APPROVE","confidence":0.9}'))
        ),
        ConsensusParticipant("b", llm_evaluator(model("I think it's fine!"))),
        ConsensusParticipant(
            "c", llm_evaluator(model('{"recommendation":"APPROVE","confidence":0.6}'))
        ),
    ]
    result = MacpConsensusNode(client, participants).invoke({"proposal": "x"})["macp"]
    evaluations = [
        (p.recommendation, p.confidence, p.reason)
        for t, _, _, p in client.sent
        if t == "Evaluation"
    ]
    assert evaluations[1] == ("REVIEW", 0.0, UNPARSEABLE)
    assert result["outcome"] == "approve"
