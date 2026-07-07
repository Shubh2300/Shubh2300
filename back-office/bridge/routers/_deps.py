"""Shared router dependencies — the approval-token gate for write actions.

Any action with risk_level >= 2 (scheduling / patient / notes) MUST carry an
approval_token issued after a human approved the plan on screen. A missing token
is a hard block returned as the standard BridgeResponse — never a silent
execute. (This is a presence/shape check at the bridge boundary; the token's
validity is verified against the approvals table by the API before it is ever
handed to a caller.)
"""

from __future__ import annotations

from typing import Optional

from ..models import BridgeResponse, System


def require_approval_token(
    system: System, action: str, token: Optional[str]
) -> Optional[BridgeResponse]:
    """Return a blocked BridgeResponse if the write is missing its approval
    token, else None (proceed)."""
    if not token or not str(token).strip():
        return BridgeResponse.blocked(
            system=system,
            action=action,
            reason=(
                "Write action requires an approval_token (risk_level >= 2). "
                "No token supplied — refusing to execute."
            ),
            needed_from_user=["approval_token (from an approved on-screen plan)"],
        )
    return None
