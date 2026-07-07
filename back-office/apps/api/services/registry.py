"""Action Registry loader + ActionIntent validator.

The registry data (``packages/action-registry/actions.json``) and the intent
shape (``action_intent.schema.json``) are owned by the action-registry sibling.
This module loads the registry and validates a proposed ActionIntent against it.
Pure logic (no network, no DB) so it can be unit tested and reused by the
Temporal worker's ``validate_intent`` activity.

Real ``actions.json`` entry (relevant fields):
    action_name, target_system ('sis'|'svigg'|'both'|'unknown'),
    risk_level (1..4), requires_approval (bool),
    required_inputs (list[str], often descriptive), implementation_status

Real ActionIntent (action_intent.schema.json) fields used here:
    action_name, target_system, risk_level, requires_approval,
    patient_identifiers, action_inputs, missing_fields (list[str]), reason

Completeness is driven by the intent's ``missing_fields`` (the parser computes
what the request did not supply). For simple/synthetic registries that carry
clean ``required_inputs`` keys and intents without ``missing_fields``, a
mechanical fallback diffs required keys against ``action_inputs``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_IMPLEMENTED = "IMPLEMENTED_VENDORED"


@dataclass(frozen=True)
class ActionContract:
    action_name: str
    target_system: Optional[str]
    risk_level: Optional[int]
    requires_approval: bool
    required_inputs: List[str]
    implementation_status: Optional[str]
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    status: str  # "accepted" | "rejected"
    unknown_action: bool = False
    missing_inputs: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    implemented: bool = True
    contract: Optional[ActionContract] = None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_contract(name: str, raw: Dict[str, Any]) -> ActionContract:
    required = raw.get("required_inputs") or []
    if not isinstance(required, list):
        required = []
    return ActionContract(
        action_name=raw.get("action_name") or raw.get("action") or name,
        target_system=raw.get("target_system") or raw.get("system"),
        risk_level=_as_int(raw.get("risk_level")),
        requires_approval=bool(raw.get("requires_approval", False)),
        required_inputs=[str(x) for x in required],
        implementation_status=raw.get("implementation_status"),
        raw=raw,
    )


class ActionRegistry:
    """In-memory view of the Action Registry."""

    def __init__(self, contracts: Dict[str, ActionContract]):
        self._contracts = contracts

    @classmethod
    def from_data(cls, data: Any) -> "ActionRegistry":
        if isinstance(data, dict) and "actions" in data:
            actions = data["actions"]
        else:
            actions = data

        contracts: Dict[str, ActionContract] = {}
        if isinstance(actions, list):
            for raw in actions:
                if not isinstance(raw, dict):
                    continue
                name = raw.get("action_name") or raw.get("action") or raw.get("name")
                if name:
                    contracts[name] = _to_contract(name, raw)
        elif isinstance(actions, dict):
            for name, raw in actions.items():
                if isinstance(raw, dict):
                    contracts[name] = _to_contract(name, raw)
        return cls(contracts)

    @classmethod
    def load(cls, path: str | Path) -> "ActionRegistry":
        return cls.from_data(json.loads(Path(path).read_text(encoding="utf-8")))

    def get(self, action_name: str) -> Optional[ActionContract]:
        return self._contracts.get(action_name)

    def actions(self) -> List[str]:
        return sorted(self._contracts.keys())

    @staticmethod
    def _systems_compatible(intent_system: Optional[str], contract_system: Optional[str]) -> bool:
        if not intent_system or not contract_system:
            return True
        if intent_system in ("both", "unknown") or contract_system == "both":
            return True
        return intent_system == contract_system

    def validate(self, intent: Dict[str, Any]) -> ValidationResult:
        """Validate a proposed intent (plain dict) against the registry."""
        action_name = intent.get("action_name") or intent.get("action")
        if not action_name:
            return ValidationResult(
                ok=False,
                status="rejected",
                errors=["intent is missing an 'action_name'"],
            )

        contract = self._contracts.get(action_name)
        if contract is None:
            return ValidationResult(
                ok=False,
                status="rejected",
                unknown_action=True,
                errors=[f"unknown action '{action_name}' is not in the registry"],
            )

        errors: List[str] = []

        intent_system = intent.get("target_system") or intent.get("system")
        if not self._systems_compatible(intent_system, contract.target_system):
            errors.append(
                f"intent target_system '{intent_system}' is incompatible with "
                f"registry target_system '{contract.target_system}' for "
                f"'{action_name}'"
            )

        # Completeness: prefer the parser-computed missing_fields; fall back to
        # a mechanical diff for simple registries/intents that lack it.
        missing_fields = intent.get("missing_fields")
        if missing_fields is not None:
            missing = [str(x) for x in missing_fields]
        else:
            provided = intent.get("action_inputs") or intent.get("inputs") or {}
            provided = provided if isinstance(provided, dict) else {}
            missing = [
                name
                for name in contract.required_inputs
                if provided.get(name) in (None, "")
            ]

        if missing:
            errors.append("missing required inputs: " + ", ".join(missing))

        implemented = contract.implementation_status in (None, _IMPLEMENTED)

        ok = not missing and not errors
        return ValidationResult(
            ok=ok,
            status="accepted" if ok else "rejected",
            missing_inputs=missing,
            errors=errors,
            implemented=implemented,
            contract=contract,
        )
