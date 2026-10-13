"""GroqJSONClient response handling (no network: the SDK call is stubbed)."""
from types import SimpleNamespace

import groq
import httpx
import pytest

from coi import llm
from coi.config import Settings
from coi.llm import GroqJSONClient, LLMError


def _status_error(cls, code, message="error"):
    response = httpx.Response(code, request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"))
    return cls(message, response=response, body=None)


def _client(*results, model="openai/gpt-oss-120b"):
    """Each result is a response object (returned) or an exception (raised), consumed in order."""
    c = GroqJSONClient(Settings(groq_api_key="test", groq_model=model))
    calls = []
    queue = list(results)

    def create(**kwargs):
        calls.append(kwargs)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    c.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return c, calls


def _resp(content, finish="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content))])


def test_parses_json_and_requests_strict_schema_with_temperature_zero():
    c, calls = _client(_resp('{"issues": [], "summary": "ok"}'))
    data, raw = c.call_json("sys", "user", {"type": "object"})
    assert data == {"issues": [], "summary": "ok"}
    kw = calls[0]
    assert kw["model"] == "openai/gpt-oss-120b" and kw["temperature"] == 0
    assert kw["response_format"]["type"] == "json_schema"
    assert kw["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert kw["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}]


def test_falls_back_to_json_object_when_model_lacks_json_schema():
    model = "test/no-schema-model"
    llm._NO_JSON_SCHEMA.discard(model)
    err = _status_error(groq.BadRequestError, 400, "response_format json_schema is not supported by this model")
    c, calls = _client(err, _resp('{"a": 1}'), model=model)
    assert c.call_json("sys", "user", {"type": "object"})[0] == {"a": 1}
    assert calls[1]["response_format"] == {"type": "json_object"}
    assert "JSON schema" in calls[1]["messages"][0]["content"]
    assert model in llm._NO_JSON_SCHEMA  # remembered: next call goes straight to json_object mode


@pytest.mark.parametrize("resp", [_resp("not json"), _resp(""), _resp(None), _resp('{"a":1}', finish="length"),
                                  _resp("[1, 2]"), SimpleNamespace(choices=[])])
def test_unusable_responses_raise_llm_error(resp):
    c, _ = _client(resp)
    with pytest.raises(LLMError):
        c.call_json("s", "u", {})


@pytest.mark.parametrize("exc", [
    _status_error(groq.RateLimitError, 429, "rate limited"),
    _status_error(groq.InternalServerError, 503, "unavailable"),
    _status_error(groq.AuthenticationError, 401, "invalid api key"),
    _status_error(groq.BadRequestError, 400, "something else"),
    groq.APITimeoutError(request=httpx.Request("POST", "https://api.groq.com")),
])
def test_api_errors_raise_llm_error(exc):
    c, _ = _client(exc)
    with pytest.raises(LLMError):
        c.call_json("s", "u", {})


def test_missing_key_raises():
    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        GroqJSONClient(Settings(groq_api_key=""))
