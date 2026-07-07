"""Shared adapter helpers: registry lookup, credential checks, patient matching.

The adapter layer is the ONLY place that decides, at runtime, whether an action
can genuinely execute. Three gates, in order:

  1. The action's contract says IMPLEMENTED_VENDORED (else -> blocked).
  2. The required EMR credentials are present in the environment (else -> blocked
     with the missing env-var names, never a fake success).
  3. For writes, the patient match is strong (else -> needs_human_review).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from ..models import (
    BridgeResponse,
    MatchLevel,
    PatientIdentifiers,
    PatientMatch,
    System,
)

# packages/action-registry/actions.json relative to the back-office root.
_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "action-registry"
    / "actions.json"
)

IMPLEMENTED = "IMPLEMENTED_VENDORED"

# Env-var names (NAMES ONLY — never values) required per system to talk to a
# real EMR. Matches the vendored clients (sis_client.py / svigg_scraper.py).
REQUIRED_ENV = {
    System.sis: ["SIS_USERNAME", "SIS_PASSWORD"],
    System.svigg: ["WEBEDOCTOR_USER", "WEBEDOCTOR_PASS"],
}


@lru_cache(maxsize=1)
def _registry() -> dict:
    with _REGISTRY_PATH.open() as fh:
        doc = json.load(fh)
    return {a["action_name"]: a for a in doc["actions"]}


def get_contract(action_name: str) -> Optional[dict]:
    return _registry().get(action_name)


def is_implemented(action_name: str) -> bool:
    c = get_contract(action_name)
    return bool(c and c.get("implementation_status") == IMPLEMENTED)


def missing_credentials(system: System) -> list[str]:
    """Return the env-var NAMES that are required but absent/empty."""
    return [name for name in REQUIRED_ENV.get(system, []) if not os.environ.get(name)]


def contract_block_response(system: System, action_name: str) -> BridgeResponse:
    """Build the blocked response for a not-yet-implemented action."""
    c = get_contract(action_name) or {}
    reason = c.get(
        "blocked_reason",
        "Missing real SIS/Svigg selectors or credentials",
    )
    needed = c.get("needed_from_user", [])
    return BridgeResponse.blocked(
        system=system, action=action_name, reason=reason, needed_from_user=needed
    )


def credentials_block_response(system: System, action_name: str) -> BridgeResponse:
    missing = missing_credentials(system)
    return BridgeResponse.blocked(
        system=system,
        action=action_name,
        reason="Missing real SIS/Svigg selectors or credentials",
        needed_from_user=[f"env:{name}" for name in missing],
    )


def available_on(system: System, action_name: str) -> bool:
    """Whether the action's contract targets this system (or 'both')."""
    c = get_contract(action_name)
    if not c:
        return False
    return c.get("target_system") in (system.value, "both")


def preflight(system: System, action_name: str) -> Optional[BridgeResponse]:
    """Run gates 1 and 2. Return a blocked BridgeResponse if either fails,
    or None if the action may proceed to execution."""
    # Gate 0: the action must target this system at all.
    if not available_on(system, action_name):
        return BridgeResponse.blocked(
            system=system,
            action=action_name,
            reason=f"Action '{action_name}' is not available on system '{system.value}'.",
            needed_from_user=[],
        )
    # Gate 1: contract must be genuinely implemented.
    if not is_implemented(action_name):
        return contract_block_response(system, action_name)
    # Gate 2: required EMR credentials must be present.
    if missing_credentials(system):
        return credentials_block_response(system, action_name)
    return None


# ---------------------------------------------------------------------------
# Patient matching (strong = emr_id | dob+exact name | dob+phone)
# ---------------------------------------------------------------------------
def compute_match(p: PatientIdentifiers) -> PatientMatch:
    """Classify the identifier set the caller supplied.

    This is the pre-execution strength of the *provided* identifiers. Adapters
    combine it with the record actually found in the EMR before trusting it for
    a write; a weak set never proceeds to a write.
    """
    matched_by: list[str] = []
    if p.emr_id:
        return PatientMatch(match_level=MatchLevel.strong, matched_by=["emr_id"])

    has_dob = bool(p.dob)
    has_full_name = bool(p.first_name and p.last_name)
    has_phone = bool(p.phone)

    if has_dob and has_full_name:
        return PatientMatch(
            match_level=MatchLevel.strong, matched_by=["dob", "first_name", "last_name"]
        )
    if has_dob and has_phone:
        return PatientMatch(match_level=MatchLevel.strong, matched_by=["dob", "phone"])

    if p.first_name or p.last_name:
        matched_by.append("name")
    if has_phone:
        matched_by.append("phone")
    if p.email:
        matched_by.append("email")

    if matched_by:
        return PatientMatch(match_level=MatchLevel.weak, matched_by=matched_by)
    return PatientMatch(match_level=MatchLevel.none, matched_by=[])


def weak_match_block(
    system: System, action_name: str, match: PatientMatch
) -> Optional[BridgeResponse]:
    """For WRITE actions: if the match is not strong, halt for human review."""
    if match.match_level != MatchLevel.strong:
        return BridgeResponse.needs_review(
            system=system,
            action=action_name,
            reason=(
                "Weak or missing patient match — a write requires a strong match "
                "(emr_id, or dob+exact name, or dob+phone). Halted for human review."
            ),
            patient_match=match,
        )
    return None
