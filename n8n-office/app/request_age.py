"""app/request_age.py — classify a pending approval card as LEGACY vs CURRENT.

WHY THIS EXISTS
  The pending-approval queue on the Home screen mixes fresh requests with an
  ingested backlog. The catch: ``approvals.ts_created`` (and ``messages.ts``)
  is *INGEST* time — when our poller first pulled the row — NOT when the patient
  actually reached out. For the current backlog every row was ingested "today",
  so ts_created alone would call a two-week-old voicemail "new". That is
  dishonest and buries genuinely-aged requests.

  The REAL patient-contact time lives in the source RingCentral record, carried
  through on the message's ``detail`` JSON as ``creationTime``. This module
  resolves each card back to that real time and splits the queue on a 48-hour
  threshold so the SPA can park the aged backlog in its own "Legacy Requests"
  box, out of the live dashboard.

HONESTY NOTES
  - We never *invent* an age. When no real source time can be found (no linked
    message, no ``creationTime``, unparseable timestamps) we fall back, in
    order, to the linked message's ingest ``ts`` and finally the card's own
    ``ts_created`` — and we RECORD which basis was used (``age_basis``) so the
    UI/audit can tell a real patient time from a mere ingest-time fallback.
  - A card whose source time cannot be established at all is treated as CURRENT
    (never quarantined into "legacy" on a guess) — the fail-safe keeps a real
    request visible on the live queue rather than hiding it.

PUBLIC API
  LEGACY_THRESHOLD_HOURS : int  (48)
  card_source_time(card) -> (datetime | None, basis_str)
  classify_card(card, now=None) -> dict  (adds age_* keys, returns a copy)
  split_pending(cards, now=None) -> (current: list, legacy: list)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# A request whose real source time is older than this is "legacy" backlog.
LEGACY_THRESHOLD_HOURS = 48

# Basis labels recorded on each classified card (most→least trustworthy):
#   "message_creation_time" — real RingCentral creationTime from the source msg
#   "message_ingest_ts"     — the linked message's ingest ts (poll time)
#   "card_created"          — the card's own ts_created (no source message)
#   "unknown"               — nothing parseable; card stays CURRENT (fail-safe)
_BASIS_MESSAGE_CREATION = "message_creation_time"
_BASIS_MESSAGE_INGEST = "message_ingest_ts"
_BASIS_CARD_CREATED = "card_created"
_BASIS_UNKNOWN = "unknown"


def _parse_ts(value) -> datetime | None:
    """Best-effort parse of a timestamp into a tz-aware UTC ``datetime``.

    Accepts:
      * ISO-8601 strings (``2026-07-02T21:47:42.316987+00:00`` or naive) —
        naive values are assumed UTC.
      * Epoch milliseconds or seconds, as int/float or a numeric string
        (RingCentral sometimes reports ``creationTime`` as an epoch). Values
        that look like ms (>= 1e12) are divided by 1000.

    Returns ``None`` when the value is missing or cannot be parsed — the caller
    then falls back to a less-trustworthy basis rather than guessing.
    """
    if value is None or value == "":
        return None

    # Numeric epoch (int/float, or an all-digit string).
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.strip().lstrip("-").isdigit()
    ):
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        # Heuristic: >= 1e12 is epoch-milliseconds; else seconds. (1e12 s is
        # year 33658, so no real seconds value reaches it.)
        if abs(num) >= 1e12:
            num /= 1000.0
        try:
            return datetime.fromtimestamp(num, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    if isinstance(value, str):
        s = value.strip()
        # Python's fromisoformat handles the offset form our rows use; also
        # tolerate a trailing 'Z'.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    return None


def _creation_time_from_detail(detail) -> datetime | None:
    """Dig a ``creationTime`` out of a message's ``detail`` payload.

    ``comms.get_message`` already JSON-decodes ``detail`` to a dict (or leaves
    it as a raw string / None). We search the top level and one level of nested
    dicts/lists for a ``creationTime`` key — RingCentral nests it under the
    message record in some payload shapes. Returns a parsed UTC datetime or
    ``None``.
    """
    if not isinstance(detail, dict):
        return None

    def _find(obj, depth: int = 0):
        if depth > 4:
            return None
        if isinstance(obj, dict):
            if "creationTime" in obj:
                return obj["creationTime"]
            for v in obj.values():
                found = _find(v, depth + 1)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = _find(v, depth + 1)
                if found is not None:
                    return found
        return None

    raw = _find(detail)
    return _parse_ts(raw)


def _lookup_message(message_id):
    """Return the source message dict for ``message_id`` (or ``None``).

    Imported lazily so this module stays importable without the comms stack.
    Any lookup failure degrades to ``None`` (caller falls back to card age) and
    is logged with the id only — never message content (PHI).
    """
    if message_id in (None, ""):
        return None
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return None
    try:
        from app import comms

        return comms.get_message(mid)
    except Exception as exc:  # noqa: BLE001 - degrade to card-age fallback
        logger.warning("request_age: message lookup failed for %s: %s",
                       message_id, exc)
        return None


def card_source_time(card: dict) -> tuple[datetime | None, str]:
    """Resolve a card's REAL source time and the basis used.

    Resolution order:
      1. Triage cards carry ``params.source_message_id``. Look up that message
         and use its ``detail.creationTime`` (the true RingCentral patient
         time) → basis ``message_creation_time``.
      2. If the message exists but has no usable creationTime, use its ingest
         ``ts`` → basis ``message_ingest_ts`` (still the message, not the card).
      3. No source message (referrals / bookings / manual cards) → the card's
         own ``ts_created`` → basis ``card_created``.
      4. Nothing parseable anywhere → ``(None, "unknown")``.

    Returns ``(datetime | None, basis)``.
    """
    params = card.get("params")
    if not isinstance(params, dict):
        params = {}

    message_id = params.get("source_message_id")
    if message_id not in (None, ""):
        msg = _lookup_message(message_id)
        if msg:
            ct = _creation_time_from_detail(msg.get("detail"))
            if ct is not None:
                return ct, _BASIS_MESSAGE_CREATION
            ingest = _parse_ts(msg.get("ts"))
            if ingest is not None:
                return ingest, _BASIS_MESSAGE_INGEST
        # message_id present but unresolved → still prefer the card's own age
        # below rather than guessing.

    created = _parse_ts(card.get("ts_created"))
    if created is not None:
        return created, _BASIS_CARD_CREATED

    return None, _BASIS_UNKNOWN


def classify_card(card: dict, now: datetime | None = None) -> dict:
    """Return a shallow copy of ``card`` annotated with age fields.

    Added keys (never overwrite existing card data):
      * ``age_basis``       — which source the time came from (see basis labels)
      * ``age_source_ts``   — ISO string of the resolved source time, or None
      * ``age_hours``       — float hours since the source time, or None
      * ``is_legacy``       — True iff a real source time > 48h old

    A card with an unknown/None source time is NEVER legacy (fail-safe: keep it
    on the live queue). This function does not mutate the input.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    out = dict(card)
    source_ts, basis = card_source_time(card)

    if source_ts is None:
        out["age_basis"] = basis
        out["age_source_ts"] = None
        out["age_hours"] = None
        out["is_legacy"] = False
        return out

    age_hours = (now - source_ts).total_seconds() / 3600.0
    out["age_basis"] = basis
    out["age_source_ts"] = source_ts.isoformat()
    out["age_hours"] = round(age_hours, 2)
    out["is_legacy"] = age_hours > LEGACY_THRESHOLD_HOURS
    return out


def split_pending(cards, now: datetime | None = None) -> tuple[list, list]:
    """Split pending cards into ``(current, legacy)``, each annotated.

    Order within each bucket is preserved from the input (callers pass newest-
    first). Every returned card carries the ``age_*`` fields from
    ``classify_card``. Non-dict entries are skipped defensively.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    current: list = []
    legacy: list = []
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        annotated = classify_card(card, now=now)
        (legacy if annotated["is_legacy"] else current).append(annotated)
    return current, legacy
