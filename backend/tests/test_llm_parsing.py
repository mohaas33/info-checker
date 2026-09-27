import pytest

from app import llm
from app.schemas import ExtractionOutput

VALID = '{"claims": [{"original_text": "a", "claim": "A", "type": "factual", "search_queries": ["q"]}]}'


@pytest.mark.parametrize(
    "raw",
    [
        VALID,
        f"```json\n{VALID}\n```",
        f"```\n{VALID}\n```",
        f"<think>Let me think {{about}} this.</think>\n{VALID}",
        f"<think>reasoning</think>\n```json\n{VALID}\n```",
        f"reasoning without opening tag</think>{VALID}",
        f"Here is the result:\n{VALID}\nHope that helps.",
    ],
)
def test_parse_handles_wrappers(raw):
    out = llm.parse_json_as(raw, ExtractionOutput)
    assert out.claims[0].claim == "A"
    assert out.claims[0].type == "factual"


def test_parse_rejects_no_json():
    with pytest.raises(ValueError):
        llm.parse_json_as("I cannot help with that.", ExtractionOutput)


async def test_retry_includes_validation_error(monkeypatch):
    calls = []
    replies = iter(['{"claims": [{"claim": "missing fields"}]}', VALID])

    async def fake(messages, model):
        calls.append(messages)
        return next(replies)

    monkeypatch.setattr(llm, "_raw_completion", fake)
    out = await llm.complete_json("sys", "user", ExtractionOutput, model="m")
    assert out.claims[0].claim == "A"
    assert len(calls) == 2
    assert "invalid" in calls[1][-1]["content"].lower()
    assert "original_text" in calls[1][-1]["content"]  # the validation error names the field


async def test_gives_up_after_one_retry(monkeypatch):
    async def fake(messages, model):
        return "not json"

    monkeypatch.setattr(llm, "_raw_completion", fake)
    with pytest.raises(llm.LLMError):
        await llm.complete_json("sys", "user", ExtractionOutput, model="m")
