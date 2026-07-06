#!/usr/bin/env python3
"""tests_request_age.py — offline unit checks for the 48h LEGACY/CURRENT split.

Pure-logic tests: no DB, no server. We monkeypatch ``request_age._lookup_message``
so no sqlite/comms is touched. Exit 0 iff every assertion holds; prints a
machine-readable SUMMARY line for the orchestrator.

Run:  cd /Users/shubh/n8n-office && python3 tests_request_age.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from app import request_age

NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
_passed = 0
_failed = 0


def check(name: str, cond: bool) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS  {name}")
    else:
        _failed += 1
        print(f"FAIL  {name}")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------
# 1. Triage card with a real creationTime > 48h old -> LEGACY, real basis.
# ---------------------------------------------------------------------------
old_creation = NOW - timedelta(hours=72)
request_age._lookup_message = lambda mid: {  # type: ignore[assignment]
    "id": mid, "ts": _iso(NOW),  # ingested "now"
    "detail": {"creationTime": _iso(old_creation)},
}
card = {"id": 1, "ts_created": _iso(NOW), "params": {"source_message_id": 558}}
res = request_age.classify_card(card, now=NOW)
check("triage creationTime 72h -> legacy", res["is_legacy"] is True)
check("triage basis is message_creation_time",
      res["age_basis"] == "message_creation_time")
check("triage age_hours ~72", abs(res["age_hours"] - 72.0) < 0.1)

# ---------------------------------------------------------------------------
# 2. Same card but creationTime is recent -> CURRENT even though present.
# ---------------------------------------------------------------------------
recent_creation = NOW - timedelta(hours=5)
request_age._lookup_message = lambda mid: {  # type: ignore[assignment]
    "id": mid, "ts": _iso(NOW),
    "detail": {"creationTime": _iso(recent_creation)},
}
res = request_age.classify_card(card, now=NOW)
check("triage creationTime 5h -> current", res["is_legacy"] is False)
check("triage recent basis still real", res["age_basis"] == "message_creation_time")

# ---------------------------------------------------------------------------
# 3. creationTime as epoch-MILLISECONDS (RingCentral variant) > 48h -> LEGACY.
# ---------------------------------------------------------------------------
epoch_ms = int((NOW - timedelta(hours=100)).timestamp() * 1000)
request_age._lookup_message = lambda mid: {  # type: ignore[assignment]
    "id": mid, "ts": _iso(NOW), "detail": {"record": {"creationTime": epoch_ms}},
}
res = request_age.classify_card(card, now=NOW)
check("nested epoch-ms creationTime 100h -> legacy", res["is_legacy"] is True)
check("epoch-ms age ~100h", abs(res["age_hours"] - 100.0) < 0.2)

# ---------------------------------------------------------------------------
# 4. Message exists but NO creationTime -> falls back to ingest ts (recent).
# ---------------------------------------------------------------------------
request_age._lookup_message = lambda mid: {  # type: ignore[assignment]
    "id": mid, "ts": _iso(NOW - timedelta(hours=2)),
    "detail": {"format": "sms"},  # no creationTime
}
res = request_age.classify_card(card, now=NOW)
check("no creationTime -> ingest basis", res["age_basis"] == "message_ingest_ts")
check("ingest 2h -> current", res["is_legacy"] is False)

# ---------------------------------------------------------------------------
# 5. No source_message_id at all -> card ts_created basis.
# ---------------------------------------------------------------------------
ref_card = {"id": 2, "ts_created": _iso(NOW - timedelta(hours=1)), "params": {}}
res = request_age.classify_card(ref_card, now=NOW)
check("referral card -> card_created basis", res["age_basis"] == "card_created")
check("referral 1h -> current", res["is_legacy"] is False)

old_ref = {"id": 3, "ts_created": _iso(NOW - timedelta(hours=200)), "params": {}}
res = request_age.classify_card(old_ref, now=NOW)
check("old referral card 200h -> legacy", res["is_legacy"] is True)

# ---------------------------------------------------------------------------
# 6. Unresolvable / missing everything -> unknown basis, NOT legacy (fail-safe).
# ---------------------------------------------------------------------------
request_age._lookup_message = lambda mid: None  # type: ignore[assignment]
bad = {"id": 4, "ts_created": "not-a-date", "params": {"source_message_id": 9}}
res = request_age.classify_card(bad, now=NOW)
check("unparseable -> unknown basis", res["age_basis"] == "unknown")
check("unknown age -> current (fail-safe)", res["is_legacy"] is False)
check("unknown age_source_ts is None", res["age_source_ts"] is None)

# ---------------------------------------------------------------------------
# 7. split_pending partitions correctly and never drops/invents a card.
# ---------------------------------------------------------------------------
request_age._lookup_message = lambda mid: {  # type: ignore[assignment]
    "id": mid, "ts": _iso(NOW),
    "detail": {"creationTime": _iso(NOW - timedelta(hours=72))},
}
cards = [
    {"id": 10, "ts_created": _iso(NOW), "params": {"source_message_id": 1}},   # legacy
    {"id": 11, "ts_created": _iso(NOW - timedelta(hours=1)), "params": {}},     # current
    {"id": 12, "ts_created": _iso(NOW - timedelta(hours=300)), "params": {}},   # legacy
]
current, legacy = request_age.split_pending(cards, now=NOW)
check("split total preserved", len(current) + len(legacy) == len(cards))
check("split legacy count == 2", len(legacy) == 2)
check("split current count == 1", len(current) == 1)
ids = {c["id"] for c in current} | {c["id"] for c in legacy}
check("split no card dropped", ids == {10, 11, 12})

# ---------------------------------------------------------------------------
# 8. Non-dict entries are skipped defensively.
# ---------------------------------------------------------------------------
current, legacy = request_age.split_pending([None, "x", cards[1]], now=NOW)
check("split skips non-dicts", len(current) + len(legacy) == 1)

print(f"SUMMARY {_passed} passed, {_failed} failed")
sys.exit(0 if _failed == 0 else 1)
