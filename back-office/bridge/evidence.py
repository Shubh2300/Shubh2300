"""evidence.py — screenshot + Playwright trace capture for the bridge.

Every WRITE adapter must capture a post-action verification screenshot proving
the EMR end-state. These helpers generate stable ids, write the artifacts under
``bridge/evidence/`` (gitignored), and return the ids so they can be recorded on
the ``screenshots`` / ``traces`` tables and the ``BridgeResponse``.

No PHI is added to the ids or filenames — an id is an opaque timestamp+uuid.
The image/trace bytes themselves may contain PHI (they are screenshots of a
chart), so ``bridge/evidence/`` is gitignored and lives only on the office
machine.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Artifacts are written next to this module, under evidence/.
EVIDENCE_DIR = Path(__file__).resolve().parent / "evidence"
SCREENSHOT_DIR = EVIDENCE_DIR / "screenshots"
TRACE_DIR = EVIDENCE_DIR / "traces"


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _new_id(prefix: str) -> str:
    """Opaque, sortable id: <prefix>_<utc-timestamp>_<short-uuid>. No PHI."""
    return f"{prefix}_{_now_stamp()}_{uuid.uuid4().hex[:8]}"


def _ensure_dirs() -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    TRACE_DIR.mkdir(parents=True, exist_ok=True)


def _sha256_file(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


async def capture_screenshot(page: Any, *, system: str, label: str = "post_action") -> dict:
    """Save a full-page screenshot of ``page`` and return its metadata.

    ``page`` is a Playwright async Page. This function is intentionally
    defensive: if the screenshot cannot be taken it returns an id with an
    ``error`` field rather than raising, so a verification-capture failure never
    masquerades as a successful write (the caller inspects ``error``).
    """
    _ensure_dirs()
    screenshot_id = _new_id(f"shot_{system}_{label}")
    out = SCREENSHOT_DIR / f"{screenshot_id}.png"
    meta: dict = {
        "screenshot_id": screenshot_id,
        "file_path": str(out),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "system": system,
        "label": label,
    }
    if page is None:
        meta["error"] = "no_page_available"
        return meta
    try:
        await page.screenshot(path=str(out), full_page=True)
        meta["sha256"] = _sha256_file(out)
    except Exception as exc:  # pragma: no cover - depends on live browser
        meta["error"] = f"screenshot_failed: {exc}"
    return meta


async def start_trace(context: Any) -> None:
    """Begin a Playwright trace on a browser context (best-effort)."""
    if context is None:
        return
    try:
        await context.tracing.start(screenshots=True, snapshots=True, sources=False)
    except Exception:  # pragma: no cover
        pass


async def stop_trace(context: Any, *, system: str, label: str = "post_action") -> dict:
    """Stop tracing and write the trace.zip; return its metadata."""
    _ensure_dirs()
    trace_id = _new_id(f"trace_{system}_{label}")
    out = TRACE_DIR / f"{trace_id}.zip"
    meta: dict = {
        "trace_id": trace_id,
        "file_path": str(out),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "system": system,
        "label": label,
    }
    if context is None:
        meta["error"] = "no_context_available"
        return meta
    try:
        await context.tracing.stop(path=str(out))
        meta["sha256"] = _sha256_file(out)
    except Exception as exc:  # pragma: no cover
        meta["error"] = f"trace_failed: {exc}"
    return meta
