"""All LLM calls go through complete_json(). Swap the provider by rewriting this module.

Currently: Nebius Token Factory via the OpenAI-compatible API.
"""

import json
import logging
import re
from typing import TypeVar

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app import config, usage

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_client: AsyncOpenAI | None = None
# Models that rejected response_format={"type": "json_object"}; we stop sending it to them.
_no_json_mode: set[str] = set()


class LLMError(Exception):
    """The model did not return valid output after retrying."""


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        s = config.get_settings()
        _client = AsyncOpenAI(base_url=s.nebius_base_url, api_key=s.require_key("nebius_api_key"))
    return _client


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def clean_json_text(raw: str) -> str:
    """Pull a JSON object out of a model reply that may contain reasoning or code fences."""
    text = _THINK_RE.sub("", raw)
    # Some reasoning models omit the opening <think> tag and only emit the closing one.
    if "</think>" in text.lower():
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object found in model output")
    return text[start : end + 1]


def parse_json_as(raw: str, schema_model: type[T]) -> T:
    return schema_model.model_validate_json(clean_json_text(raw))


async def _raw_completion(messages: list[dict], model: str) -> str:
    """One chat completion call. Tests replace this function."""
    client = get_client()
    kwargs = dict(model=model, messages=messages, temperature=config.get_settings().llm_temperature)
    if model not in _no_json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = await client.chat.completions.create(**kwargs)
    except openai.BadRequestError as e:
        if "response_format" not in kwargs:
            raise
        log.warning("model %s rejected response_format, retrying without it: %s", model, e)
        _no_json_mode.add(model)
        kwargs.pop("response_format")
        resp = await client.chat.completions.create(**kwargs)

    u = usage.current()
    u.llm_calls += 1
    if resp.usage:
        u.prompt_tokens += resp.usage.prompt_tokens or 0
        u.completion_tokens += resp.usage.completion_tokens or 0
    return resp.choices[0].message.content or ""


async def complete_json(system: str, user: str, schema_model: type[T], model: str) -> T:
    """Ask the model for JSON matching schema_model. Retries once with the validation error."""
    schema = json.dumps(schema_model.model_json_schema())
    system = (
        f"{system}\n\nRespond with a single JSON object only, no prose, matching this "
        f"JSON schema:\n{schema}"
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    raw = await _raw_completion(messages, model)
    try:
        return parse_json_as(raw, schema_model)
    except (ValidationError, ValueError) as e:
        log.warning("invalid LLM output from %s, retrying: %s", model, e)
        messages += [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": f"Your reply was invalid: {e}\n"
                "Reply again with only a corrected JSON object matching the schema.",
            },
        ]

    raw = await _raw_completion(messages, model)
    try:
        return parse_json_as(raw, schema_model)
    except (ValidationError, ValueError) as e:
        raise LLMError(f"invalid output from {model} after retry: {e}") from e
