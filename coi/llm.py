"""Groq structured-output client.

Requests response_format={"type": "json_schema"} (strict) so responses are constrained JSON, never
parsed prose. If the configured model does not support JSON-schema mode, falls back once to JSON-object
mode with the schema included in the system prompt; the application's guardrails validate the result
either way. temperature=0 for deterministic/low-variance output.
"""
from __future__ import annotations

import json
import logging

import groq

from .config import Settings

log = logging.getLogger(__name__)

# Models found not to support json_schema response_format (remembered for this process).
_NO_JSON_SCHEMA: set[str] = set()
# GPT-OSS reasoning models: keep reasoning short and out of the response to save tokens / free-tier quota.
_GPT_OSS_PREFIX = "openai/gpt-oss"



class LLMError(Exception):
    """Any failure to obtain a usable structured response."""


class GroqJSONClient:
    def __init__(self, settings: Settings):
        if not settings.groq_api_key:
            raise LLMError("GROQ_API_KEY is not configured.")
        self.settings = settings
        self.model = settings.groq_model
        # One SDK-level retry (with backoff) for 429/5xx/connection errors; the pipeline adds its own
        # retry-once + fail-safe on top. Kept small so a rate-limited demo fails fast instead of hanging.
        self.client = groq.Groq(api_key=settings.groq_api_key, timeout=60.0, max_retries=1)

    def _create(self, system: str, user: str, schema: dict, json_schema_mode: bool):
        if json_schema_mode:
            response_format = {"type": "json_schema",
                               "json_schema": {"name": "coi_output", "schema": schema, "strict": True}}
        else:
            response_format = {"type": "json_object"}
            system = (system + "\n\nRespond ONLY with a single JSON object that conforms exactly to this JSON schema:\n"
                      + json.dumps(schema))
        reasoning = ({"reasoning_effort": "low", "include_reasoning": False}
                     if self.model.startswith(_GPT_OSS_PREFIX) else {})
        return self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=response_format,
            temperature=0,
            **reasoning,
            # Prompts are ~1.5k-3.5k tokens; 4000 leaves room for the JSON answer while staying under
            # free-tier tokens-per-minute limits (Groq counts requested completion tokens against them).
            max_completion_tokens=4000,
        )

    def call_json(self, system: str, user: str, schema: dict) -> tuple[dict, str]:
        """Returns (parsed_json, raw_text). Raises LLMError on any failure."""
        use_schema = self.model not in _NO_JSON_SCHEMA
        try:
            try:
                response = self._create(system, user, schema, use_schema)
            except groq.BadRequestError as exc:
                if use_schema and ("response_format" in str(exc) or "json_schema" in str(exc)):
                    log.warning("Model %s rejected json_schema mode; using json_object mode: %s", self.model, exc)
                    _NO_JSON_SCHEMA.add(self.model)
                    response = self._create(system, user, schema, False)
                else:
                    raise
        except groq.RateLimitError as exc:
            raise LLMError(f"Groq rate limit reached (429) — wait a minute and re-run: {exc.message}") from exc
        except groq.APITimeoutError as exc:
            raise LLMError(f"Timeout calling Groq API: {exc}") from exc
        except groq.APIConnectionError as exc:
            raise LLMError(f"Connection error calling Groq API: {exc}") from exc
        except groq.APIStatusError as exc:
            raise LLMError(f"Groq API error {exc.status_code}: {exc.message}") from exc
        except groq.APIError as exc:
            raise LLMError(f"Groq API error: {exc}") from exc

        if not response.choices:
            raise LLMError("Groq returned no choices.")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMError("Model output was truncated (finish_reason=length).")
        text = choice.message.content
        if not text:
            raise LLMError("Groq returned an empty response.")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Malformed JSON from model: {exc}") from exc
        if not isinstance(data, dict):
            raise LLMError("Model returned JSON that is not an object.")
        return data, text
