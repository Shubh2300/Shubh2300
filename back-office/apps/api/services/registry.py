"""Action Registry loader + ActionIntent validator.

The registry data (``actions.json``) is owned by the action-registry package
(sibling agent). This module loads it and validates a proposed ActionIntent
against it. It is deliberately pure logic (no network, no DB) so it can be unit
tested and reused by the Temporal worker's ``validate_intent`` activity.

Assumed ``actions.json`` shape (normalizer tolerates a couple of variants):

    {
      "version": "1",
      "actions": [
        {
          "action": "sis.get_patient_demographics",
          "system": "sis",
          "endpoint": "/sis/patient/demographics",
          "method": "POST",
          "write": false,
          "description": "...",
          "required_inputs": ["patient_id"],
          "optional_inputs": ["as_of"]
        },
        ...
      ]
    }

Also accepted:
  * top-level list of action objects,
  * top-level object keyed by action name,
  * ``inputs`` as a list of ``{"name": ..., "required": true}`` objects
    instead of the ``required_inputs`` / ``optional_inputs`` split.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ActionContract:
    action: str
    system: Optional[str]
    endpoint: Optional[str]
    method: str
    write: bool
    required_inputs: List[str]
    optional_inputs: List[str]
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    status: str  # "accepted" | "rejected"
    unknown_action: bool = False
    missing_inputs: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    contract: Optional[ActionContract] = None


def _normalize_inputs(raw: Dict[str, Any]) -> tuple[List[str], List[str]]:
    """Return (required, optional) input names from a raw contract dict."""
    required: List[str] = list(raw.get("required_inputs") or [])
    optional: List[str] = list(raw.get("optional_inputs") or [])

    inputs = raw.get("inputs")
    if isinstance(inputs, list):
        for item in inputs:
            if isinstance(item, str):
                required.append(item)
            elif isinstance(item, dict):
                name = item.get("name")
                if not name:
                    continue
                if item.get("required", False):
                    required.append(name)
                else:
                    optional.append(name)
    elif isinstance(inputs, dict):
        for name, spec in inputs.items():
            is_req = bool(spec.get("required")) if isinstance(spec, dict) else False
            (required if is_req else optional).append(name)

    # de-dupe, preserve order
    def _dedupe(xs: List[str]) -> List[str]:
        seen: set = set()
        out: List[str] = []
        for x in xs:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return _dedupe(required), _dedupe(optional)


def _to_contract(action_name: str, raw: Dict[str, Any]) -> ActionContract:
    required, optional = _normalize_inputs(raw)
    return ActionContract(
        action=raw.get("action") or action_name,
        system=raw.get("system"),
        endpoint=raw.get("endpoint") or raw.get("path"),
        method=str(raw.get("method") or "POST").upper(),
        write=bool(raw.get("write", False)),
        required_inputs=required,
        optional_inputs=optional,
        raw=raw,
    )


class ActionRegistry:
    """In-memory view of the Action Registry."""

    def __init__(self, contracts: Dict[str, ActionContract]):
        self._contracts = contracts

    @classmethod
    def from_data(cls, data: Any) -> "ActionRegistry":
        contracts: Dict[str, ActionContract] = {}

        if isinstance(data, dict) and "actions" in data:
            actions = data["actions"]
        else:
            actions = data

        if isinstance(actions, list):
            for raw in actions:
                if not isinstance(raw, dict):
                    continue
                name = raw.get("action") or raw.get("name")
                if not name:
                    continue
                contracts[name] = _to_contract(name, raw)
        elif isinstance(actions, dict):
            for name, raw in actions.items():
                if isinstance(raw, dict):
                    contracts[name] = _to_contract(name, raw)

        return cls(contracts)

    @classmethod
    def load(cls, path: str | Path) -> "ActionRegistry":
        text = Path(path).read_text(encoding="utf-8")
        return cls.from_data(json.loads(text))

    def get(self, action: str) -> Optional[ActionContract]:
        return self._contracts.get(action)

    def actions(self) -> List[str]:
        return sorted(self._contracts.keys())

    def validate(self, intent: Dict[str, Any]) -> ValidationResult:
        """Validate a proposed intent (as a plain dict) against the registry."""
        action = intent.get("action")
        if not action:
            return ValidationResult(
                ok=False,
                status="rejected",
                errors=["intent is missing an 'action'"],
            )

        contract = self._contracts.get(action)
        if contract is None:
            return ValidationResult(
                ok=False,
                status="rejected",
                unknown_action=True,
                errors=[f"unknown action '{action}' is not in the registry"],
            )

        provided = intent.get("inputs") or {}
        if not isinstance(provided, dict):
            return ValidationResult(
                ok=False,
                status="rejected",
                errors=["'inputs' must be an object"],
                contract=contract,
            )

        missing = [
            name
            for name in contract.required_inputs
            if provided.get(name) in (None, "")
        ]

        errors: List[str] = []
        # If the registry declares a system for the action and the intent also
        # declares one, they must agree.
        intent_system = intent.get("system")
        if contract.system and intent_system and intent_system != contract.system:
            errors.append(
                f"intent system '{intent_system}' does not match "
                f"registry system '{contract.system}' for action '{action}'"
            )

        if missing:
            errors.append(
                "missing required inputs: " + ", ".join(missing)
            )

        ok = not missing and not errors
        return ValidationResult(
            ok=ok,
            status="accepted" if ok else "rejected",
            missing_inputs=missing,
            errors=errors,
            contract=contract,
        )
