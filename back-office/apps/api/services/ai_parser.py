"""AI parser: free-text staff prompt -> ActionIntent (proposal only).

The parser NEVER executes anything. It only turns a natural-language request
into a structured, reviewable ActionIntent, constrained to the strict JSON
schema owned by the action-registry package (``action_intent.schema.json``).

HARD RULE: if OPENAI_API_KEY is absent the parser raises ``ParserUnavailable``
and the router returns a structured error — never canned / fabricated output.

No PHI in logs: we log prompt length and outcome only, never the prompt text
or parsed patient fields.
"""

from __future__ import annotations

import abc
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("backoffice.ai_parser")


class ParserError(Exception):
    """Base class for parser failures (structured, surfaced to the API)."""

    code = "parser_error"


class ParserUnavailable(ParserError):
    """Raised when the parser cannot run (e.g. missing OPENAI_API_KEY)."""

    code = "parser_unavailable"


class ParserOutputInvalid(ParserError):
    """Raised when the model returned something that is not a valid intent."""

    code = "parser_output_invalid"


class AIParser(abc.ABC):
    """Abstract interface. Implementations parse a prompt into an intent dict
    conforming to ``action_intent.schema.json``."""

    @abc.abstractmethod
    def parse(self, prompt: str, *, requested_by: Optional[str] = None) -> Dict[str, Any]:
        ...


def _load_schema(schema_path: str | Path) -> Dict[str, Any]:
    return json.loads(Path(schema_path).read_text(encoding="utf-8"))


SYSTEM_PROMPT = (
    "You are a parser for a surgical-center back-office platform. "
    "Convert the staff member's request into a single structured ActionIntent "
    "that conforms exactly to the provided JSON schema. "
    "You ONLY describe the intended action; you never execute anything and you "
    "never invent patient identifiers, results, or fields that were not stated. "
    "If the request does not name a patient, leave patient fields empty. "
    "If you are unsure which registry action applies, choose the closest and "
    "set a low confidence value."
)


class OpenAIMiniParser(AIParser):
    """OpenAI structured-outputs implementation.

    Uses the Responses/Chat structured output (strict JSON schema) so the model
    is constrained to the ActionIntent shape. The API key is read from config;
    if it is missing we fail closed with ``ParserUnavailable``.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str],
        model: str,
        schema_path: str | Path,
    ) -> None:
        if not api_key:
            # Fail closed. Never emit canned output.
            raise ParserUnavailable(
                "OPENAI_API_KEY is not configured; the parser is unavailable"
            )
        self._model = model
        self._schema = _load_schema(schema_path)
        # Import here so the module is importable without the openai package
        # installed (e.g. in pure-logic test runs).
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)

    def parse(self, prompt: str, *, requested_by: Optional[str] = None) -> Dict[str, Any]:
        logger.info("parse.start prompt_len=%d", len(prompt))
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "action_intent",
                        "strict": True,
                        "schema": self._schema,
                    },
                },
                temperature=0,
            )
        except Exception as exc:  # noqa: BLE001 - surface as structured error
            logger.warning("parse.api_error type=%s", type(exc).__name__)
            raise ParserError(f"upstream parser call failed: {type(exc).__name__}")

        content = response.choices[0].message.content
        if not content:
            raise ParserOutputInvalid("parser returned empty content")
        try:
            intent = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ParserOutputInvalid("parser returned non-JSON content") from exc

        if requested_by and not intent.get("requested_by"):
            intent["requested_by"] = requested_by
        logger.info(
            "parse.ok action=%s", intent.get("action", "?")
        )
        return intent


def build_parser(settings, schema_path: Optional[str] = None) -> AIParser:
    """Factory used by the router. Raises ParserUnavailable if no key."""
    return OpenAIMiniParser(
        api_key=settings.openai_api_key,
        model=settings.openai_parser_model,
        schema_path=schema_path or settings.action_intent_schema_path,
    )
