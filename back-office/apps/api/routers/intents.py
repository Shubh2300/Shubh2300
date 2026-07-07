"""Intents router — POST /intents/parse.

Sends the free-text prompt to the AI parser and returns a proposed
ActionIntent together with the registry validation result, for human review.
NOTHING is executed here.

If OPENAI_API_KEY is absent the parser is unavailable and this endpoint returns
a structured 503 error — never canned output.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from config import get_settings
from deps import get_registry
from schemas import ActionIntent, ParseRequest, ParseResponse, RegistryValidation
from services.ai_parser import ParserError, ParserUnavailable, build_parser

logger = logging.getLogger("backoffice.intents")

router = APIRouter(prefix="/intents", tags=["intents"])


@router.post("/parse", response_model=ParseResponse)
def parse_intent(payload: ParseRequest) -> ParseResponse:
    settings = get_settings()

    try:
        parser = build_parser(settings)
    except ParserUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": str(exc)},
        )

    try:
        intent_dict = parser.parse(payload.prompt, requested_by=payload.requested_by)
    except ParserError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": exc.code, "message": str(exc)},
        )

    # Validate the proposal against the Action Registry.
    registry = get_registry()
    result = registry.validate(intent_dict)

    intent = ActionIntent(**intent_dict)
    validation = RegistryValidation(
        ok=result.ok,
        status=result.status,
        unknown_action=result.unknown_action,
        missing_inputs=result.missing_inputs,
        errors=result.errors,
        implemented=result.implemented,
    )
    logger.info(
        "intent.parsed action=%s ok=%s", intent.action_name, validation.ok
    )
    return ParseResponse(intent=intent, validation=validation)
