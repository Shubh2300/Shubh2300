# VENDORED from mainlinesurgery-a11y/n8n-office @ backoffice-autopilot-live-20260705, commit eec3888
# Source path: python/integrations/emr_session_manager.py
# Re-vendored from the REAL, HAR-verified production repo (do not edit lightly).
# Singleton session manager wrapping both EMR clients (SIS + Svigg).
#!/usr/bin/env python3
"""
emr_session_manager.py — Persistent session manager for both EMRs.

Singleton that manages SIS Complete + Svigg connections. Provides auto-login,
session persistence, health monitoring, and unified search across both systems.

Used by the MCP server and n8n flows. Import pattern:

    from emr_session_manager import EMRSessionManager

    mgr = EMRSessionManager.get_instance()
    await mgr.connect_all()
    health = await mgr.health()

PHI note: patient data returned by these methods must never be written inside
the git repo, logged to files, or echoed to chat. It flows through to the MCP
caller and stops there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure this directory is on sys.path so sibling imports work
_THIS_DIR = Path(__file__).parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from sis_client import SISClient, EMRSessionExpired  # noqa: E402
from svigg_scraper import SviggScraper  # noqa: E402

logger = logging.getLogger(__name__)

SESSION_DIR = Path(os.path.expanduser("~/.gemini/antigravity/scratch/emr_sessions"))

# ---------------------------------------------------------------------------
# Singleton registry
# ---------------------------------------------------------------------------

_instance: Optional["EMRSessionManager"] = None


def _sis_query_variants(query: str) -> list[str]:
    """Pure helper: candidate retry strings for a SIS SearchToken query that
    returned zero results.

    SIS's SearchToken endpoint does NOT reliably match "First Last" free text
    — live test 2026-07-05: "Shamell Austin" -> 0 results, plain "Austin" -> 1
    result (a real, findable patient). Only applies when the query looks like
    a plain two-plus-token name (no comma, no digits — MRNs/phone/DOB queries
    should not be mangled). Returns at most 2 variants (last token, then
    first token), so sis_search makes at most 3 total portal calls.
    """
    q = (query or "").strip()
    if not q:
        return []
    if "," in q or any(ch.isdigit() for ch in q):
        return []
    tokens = q.split()
    if len(tokens) < 2:
        return []
    variants = [tokens[-1], tokens[0]]
    # de-dup while preserving order (e.g. 2-token names where both already tried)
    out: list[str] = []
    seen = set()
    for v in variants:
        key = v.lower()
        if key and key not in seen and key != q.lower():
            seen.add(key)
            out.append(v)
    return out[:2]


class EMRSessionManager:
    """
    Unified session manager for SIS Complete and Svigg/WEBeDoctor EMRs.

    Lifecycle:
        mgr = EMRSessionManager.get_instance()
        await mgr.connect_all()
        ...
        await mgr.disconnect_all()

    If one EMR fails to connect the other remains available. All methods
    handle EMRSessionExpired gracefully — they return a structured error dict
    rather than raising, so callers (MCP, n8n) get actionable messages.
    """

    def __init__(self):
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self._sis = SISClient(headless=True)
        self._svigg = SviggScraper(headless=True)
        self._sis_connected = False
        self._svigg_connected = False
        self._started_at: Optional[float] = None
        # Single shared Page inside SviggScraper cannot service overlapping
        # navigations — concurrent goto() calls race and one wins with
        # ERR_ABORTED. Serialize ALL Svigg operations through this lock so
        # the persistent context is used safely from parallel callers.
        self._svigg_lock = asyncio.Lock()
        # Cold-start connect locks: guard the "check flag → _connect_*" window in
        # ensure_sis/ensure_svigg so two concurrent first requests don't BOTH
        # connect (duplicate SIS logins; for Svigg two contexts fighting the
        # portal). Each ensure_* double-checks its flag after acquiring the lock.
        # Distinct from _svigg_lock (which serializes per-OPERATION navigations)
        # — these only serialize the one-time CONNECT, so they never nest with
        # _svigg_lock in a deadlock-prone order.
        self._sis_connect_lock = asyncio.Lock()
        self._svigg_connect_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> "EMRSessionManager":
        """Return (or create) the process-level singleton."""
        global _instance
        if _instance is None:
            _instance = cls()
        return _instance

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect_all(self):
        """
        Start both EMR connections in parallel.
        Failures are logged but do not raise — the other EMR still works.
        """
        self._started_at = time.time()
        results = await asyncio.gather(
            self._connect_sis(),
            self._connect_svigg(),
            return_exceptions=True,
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                name = ["SIS", "Svigg"][i]
                logger.error("%s connection failed: %s", name, r)
        logger.info(
            "EMR connections: SIS=%s, Svigg=%s",
            "up" if self._sis_connected else "DOWN",
            "up" if self._svigg_connected else "DOWN",
        )

    async def _connect_sis(self):
        try:
            await self._sis.start()
            h = await self._sis.health_check()
            self._sis_connected = h["status"] == "connected"
            if not self._sis_connected:
                logger.warning("SIS health check: %s", h)
        except Exception as e:
            self._sis_connected = False
            logger.error("SIS start() raised: %s", e)
            raise

    async def _connect_svigg(self):
        try:
            await self._svigg.start()
            ok = await self._svigg.login()
            self._svigg_connected = ok
            if not ok:
                logger.warning("Svigg login returned False")
        except Exception as e:
            self._svigg_connected = False
            logger.error("Svigg start() raised: %s", e)
            raise

    async def disconnect_all(self):
        """Stop both EMR connections cleanly."""
        results = await asyncio.gather(
            self._disconnect_sis(),
            self._disconnect_svigg(),
            return_exceptions=True,
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                name = ["SIS", "Svigg"][i]
                logger.warning("%s disconnect error (non-fatal): %s", name, r)
        logger.info("EMR session manager disconnected")

    async def _disconnect_sis(self):
        await self._sis.stop()
        self._sis_connected = False

    async def _disconnect_svigg(self):
        await self._svigg.stop()
        self._svigg_connected = False

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self) -> dict:
        """
        Returns status dict for both EMRs.

        {
            "sis":   {"status": "connected"|"expired"|"error", "user": "...", "last_check": "..."},
            "svigg": {"status": "connected"|"disconnected", "last_check": "..."},
            "session_dir": "...",
            "uptime_seconds": ...
        }
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # SIS — use the client's own health_check
        try:
            sis_h = await self._sis.health_check()
        except Exception as e:
            sis_h = {"status": "error", "user": None, "error": str(e), "last_check": now_iso}
        sis_h["last_check"] = sis_h.pop("checked_at", now_iso)

        # Svigg — no equivalent REST endpoint; report based on known state
        svigg_h = {
            "status": "connected" if self._svigg_connected else "disconnected",
            "last_check": now_iso,
        }

        uptime = round(time.time() - self._started_at, 1) if self._started_at else None

        return {
            "sis": sis_h,
            "svigg": svigg_h,
            "session_dir": str(SESSION_DIR),
            "uptime_seconds": uptime,
        }

    # ------------------------------------------------------------------
    # Ensure helpers (lazy connect on demand)
    # ------------------------------------------------------------------

    async def ensure_sis(self) -> SISClient:
        """Return connected SIS client, connecting if needed.

        Double-checked locking: a fast unlocked check avoids the lock on the
        common already-connected path; if not connected, exactly one coroutine
        connects under _sis_connect_lock while the others await and then re-check
        the flag (so they skip the redundant _connect_sis).
        """
        if not self._sis_connected:
            async with self._sis_connect_lock:
                if not self._sis_connected:
                    await self._connect_sis()
        return self._sis

    async def ensure_svigg(self) -> SviggScraper:
        """Return connected Svigg scraper, connecting if needed.

        Double-checked locking under _svigg_connect_lock so two concurrent first
        requests don't both spin up a Svigg context. This is the CONNECT guard
        only; per-operation navigations are still serialized separately by
        _svigg_lock (held by the Svigg convenience wrappers, never here), so the
        two locks do not nest and cannot deadlock.
        """
        if not self._svigg_connected:
            async with self._svigg_connect_lock:
                if not self._svigg_connected:
                    await self._connect_svigg()
        return self._svigg

    # ------------------------------------------------------------------
    # SIS convenience wrappers
    # ------------------------------------------------------------------

    async def sis_search(self, query: str) -> list[dict]:
        """Search SIS patients by name/MRN/DOB token.

        HONESTY: a search failure (session expiry, network error, portal
        error) is RAISED, never swallowed to []. Silently returning an empty
        list on error previously made a real EMR outage look identical to a
        genuine zero-match search, which reads to staff as "patient does not
        exist" — that is unsafe. Callers MUST catch and surface the failure
        (see combined_search's asyncio.gather(return_exceptions=True) path,
        and atlantic_emr_server.py's per-tool try/except blocks).

        Also runs SIS_MULTI-VARIANT retries (see _sis_query_variants) because
        SIS's SearchToken endpoint does NOT reliably match "First Last" free
        text — live test 2026-07-05: "Shamell Austin" -> 0 results, plain
        "Austin" -> 1 result (the real, findable patient). Without this retry
        a correctly-spelled two-word name query silently misses a real chart.
        """
        client = await self.ensure_sis()
        try:
            results = await client.search_patient(query)
        except EMRSessionExpired:
            logger.warning("SIS session expired during search (query_len=%d)",
                            len(query or ""))
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error(
                "SIS search failed (query_len=%d words=%d): %s",
                len(query or ""), len((query or "").split()), type(e).__name__,
            )
            raise

        if results:
            return results

        # Zero results on the raw query — try surname/first-name variants
        # before concluding "not found". Bounded to 2 extra portal calls
        # (3 total) to limit portal load.
        variants = _sis_query_variants(query)
        merged: list[dict] = list(results)
        seen_ids = {
            m.get("patientId") or m.get("PatientId") or m.get("id")
            for m in merged
        }
        for variant in variants:
            try:
                extra = await client.search_patient(variant)
            except EMRSessionExpired:
                logger.warning(
                    "SIS session expired during variant retry (query_len=%d)",
                    len(variant or ""),
                )
                self._sis_connected = False
                raise
            except Exception as e:
                logger.error(
                    "SIS variant search failed (query_len=%d): %s",
                    len(variant or ""), type(e).__name__,
                )
                raise
            for m in extra or []:
                mid = m.get("patientId") or m.get("PatientId") or m.get("id")
                if mid is None or mid not in seen_ids:
                    seen_ids.add(mid)
                    merged.append(m)
        return merged

    async def sis_todays_patients(self) -> list[dict]:
        """Fetch today's patient tracker from SIS."""
        try:
            client = await self.ensure_sis()
            return await client.get_todays_patients()
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching today's patients")
            self._sis_connected = False
            return []
        except Exception as e:
            logger.error("SIS today's patients failed: %s", e)
            return []

    async def sis_unsigned_cases(self) -> list[dict]:
        """Fetch unsigned/uncancelled cases from SIS."""
        try:
            client = await self.ensure_sis()
            return await client.get_unsigned_cases()
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching unsigned cases")
            self._sis_connected = False
            return []
        except Exception as e:
            logger.error("SIS unsigned cases failed: %s", e)
            return []

    async def sis_rooms(self) -> list[dict]:
        """Fetch OR/procedure rooms from SIS."""
        try:
            client = await self.ensure_sis()
            return await client.get_rooms()
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching rooms")
            self._sis_connected = False
            return []
        except Exception as e:
            logger.error("SIS rooms failed: %s", e)
            return []

    async def sis_schedule_week(self, start_date: str = None) -> list[dict]:
        """Fetch a Mon-Sun week of scheduled cases from SIS."""
        try:
            client = await self.ensure_sis()
            return await client.get_schedule_week(start_date)
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching week schedule")
            self._sis_connected = False
            return []
        except Exception as e:
            logger.error("SIS schedule_week failed: %s", e)
            return []

    async def sis_schedule_day(self, date: str = None) -> list[dict]:
        """Fetch single-day scheduled cases from SIS."""
        try:
            client = await self.ensure_sis()
            return await client.get_schedule_day(date)
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching day schedule")
            self._sis_connected = False
            return []
        except Exception as e:
            logger.error("SIS schedule_day failed: %s", e)
            return []

    async def sis_case_details(self, case_id: int) -> dict:
        """Fetch detailed case record from SIS by caseSummaryId."""
        try:
            client = await self.ensure_sis()
            return await client.get_case_details(int(case_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching case details (id=%s)", case_id)
            self._sis_connected = False
            return {"error": "session_expired", "case_id": case_id}
        except Exception as e:
            logger.error("SIS case_details failed (id=%s): %s", case_id, e)
            return {"error": str(e), "case_id": case_id}

    async def sis_patient_details(self, patient_id: int) -> dict:
        """Fetch detailed patient demographics from SIS by patientId."""
        try:
            client = await self.ensure_sis()
            return await client.get_patient_details(int(patient_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching patient details (id=%s)", patient_id)
            self._sis_connected = False
            return {"error": "session_expired", "patient_id": patient_id}
        except Exception as e:
            logger.error("SIS patient_details failed (id=%s): %s", patient_id, e)
            return {"error": str(e), "patient_id": patient_id}

    # ------------------------------------------------------------------
    # SIS billing / AR convenience wrappers (live, verified 2026-06-30)
    # ------------------------------------------------------------------

    async def sis_patient_billing_ledger(self, patient_id: int) -> dict:
        """Fetch per-patient billing ledger + aging from SIS by patientId."""
        try:
            client = await self.ensure_sis()
            return await client.get_patient_billing_ledger(int(patient_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching billing ledger (id=%s)", patient_id)
            self._sis_connected = False
            return {"error": "session_expired", "patient_id": patient_id}
        except Exception as e:
            logger.error("SIS billing ledger failed (id=%s): %s", patient_id, e)
            return {"error": str(e), "patient_id": patient_id}

    async def sis_patient_balance(self, patient_id: int) -> dict:
        """Resolve a single numeric patient balance + its source field from SIS."""
        try:
            client = await self.ensure_sis()
            return await client.get_patient_balance(int(patient_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired resolving balance (id=%s)", patient_id)
            self._sis_connected = False
            return {"error": "session_expired", "patient_id": patient_id, "balance": None}
        except Exception as e:
            logger.error("SIS patient balance failed (id=%s): %s", patient_id, e)
            return {"error": str(e), "patient_id": patient_id, "balance": None}

    async def sis_patient_insurance(self, patient_id: int) -> list[dict]:
        """Fetch insurance records for a patient from SIS by patientId."""
        try:
            client = await self.ensure_sis()
            return await client.get_patient_insurance(int(patient_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching insurance (id=%s)", patient_id)
            self._sis_connected = False
            return []
        except Exception as e:
            logger.error("SIS insurance failed (id=%s): %s", patient_id, e)
            return []

    async def sis_patient_record(self, patient_id: int) -> dict:
        """Consolidated patient record aggregator (graceful-degrade) from SIS."""
        try:
            client = await self.ensure_sis()
            return await client.get_patient_record(int(patient_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching patient record (id=%s)", patient_id)
            self._sis_connected = False
            return {"error": "session_expired", "patient_id": patient_id}
        except Exception as e:
            logger.error("SIS patient record failed (id=%s): %s", patient_id, e)
            return {"error": str(e), "patient_id": patient_id}

    async def sis_patient_demographics(self, patient_id: int) -> dict:
        """Fetch full patient demographics from SIS by patientId."""
        try:
            client = await self.ensure_sis()
            return await client.get_patient_demographics(int(patient_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching demographics (id=%s)", patient_id)
            self._sis_connected = False
            return {"error": "session_expired", "patient_id": patient_id}
        except Exception as e:
            logger.error("SIS demographics failed (id=%s): %s", patient_id, e)
            return {"error": str(e), "patient_id": patient_id}

    async def sis_patient_cases(self, patient_id: int) -> list[dict]:
        """Fetch the lean per-patient case list from SIS by patientId."""
        try:
            client = await self.ensure_sis()
            return await client.get_patient_cases(int(patient_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching cases (id=%s)", patient_id)
            self._sis_connected = False
            return []
        except Exception as e:
            logger.error("SIS cases failed (id=%s): %s", patient_id, e)
            return []

    async def sis_patient_notes(self, patient_id: int) -> list[dict]:
        """Fetch a patient's notes from SIS by patientId. SURFACES errors (does
        NOT swallow to []) so a failed fetch is distinguishable from 'no notes'."""
        try:
            client = await self.ensure_sis()
            return await client.get_patient_notes(int(patient_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching notes (id=%s)", patient_id)
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS notes failed (id=%s): %s", patient_id, e)
            raise

    async def sis_patient_allergies(self, patient_id: int) -> list[dict]:
        """Fetch a patient's allergy history from SIS by patientId. SURFACES
        errors (does NOT swallow to []) — a failed fetch must not read as
        'no allergies' (clinical-safety / honesty rule)."""
        try:
            client = await self.ensure_sis()
            return await client.get_patient_allergies(int(patient_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching allergies (id=%s)", patient_id)
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS allergies failed (id=%s): %s", patient_id, e)
            raise

    async def sis_patient_medications(self, patient_id: int) -> list[dict]:
        """Fetch a patient's case-resolved medications from SIS by patientId.
        SURFACES errors (does NOT swallow to []) — a failed fetch must not read
        as 'no medications'."""
        try:
            client = await self.ensure_sis()
            return await client.get_patient_medications(int(patient_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching medications (id=%s)", patient_id)
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS medications failed (id=%s): %s", patient_id, e)
            raise

    async def sis_staff_roster(self, org_id: int = 3) -> list[dict]:
        """Fetch the org staff roster from SIS (no PHI)."""
        try:
            client = await self.ensure_sis()
            return await client.get_staff_roster(int(org_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching staff roster")
            self._sis_connected = False
            return []
        except Exception as e:
            logger.error("SIS staff roster failed: %s", e)
            return []

    async def sis_ar_tracker(
        self,
        organization_id: int = 3,
        page_number: int = 1,
        page_size: int = 25,
        view_by: int = 0,
    ) -> list[dict]:
        """Fetch practice-wide AR tracker rows (RCM) from SIS."""
        try:
            client = await self.ensure_sis()
            return await client.get_ar_tracker(
                organization_id, page_number, page_size, view_by
            )
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching AR tracker")
            self._sis_connected = False
            return []
        except Exception as e:
            logger.error("SIS AR tracker failed: %s", e)
            return []

    async def sis_resolve_patient_id(self, query: str) -> Optional[int]:
        """
        Resolve a free-text patient query (name/MRN/phone) to a SIS patientId.
        Returns the first match's patientId as int, or None if unresolved.
        Mirrors the resolution pattern used in patient_360.
        """
        try:
            matches = await self.sis_search(query)
        except Exception as e:
            logger.warning("sis_resolve_patient_id search failed: %s", e)
            return None
        if matches and isinstance(matches, list):
            first = matches[0]
            pid = first.get("patientId") or first.get("PatientId") or first.get("id")
            if pid is not None:
                try:
                    return int(pid)
                except (TypeError, ValueError):
                    return None
        return None

    # ------------------------------------------------------------------
    # SIS chart / case / worklist wrappers
    # (routes HAR-derived 2026-07-01; NOT yet live-verified unless noted)
    # ------------------------------------------------------------------

    async def sis_recent_patients(self, from_date: str = None) -> list[dict]:
        """Fetch recently seen/accessed patients from SIS.
        POST RecentPatients/GetRecentPatientsData — HAR-derived 2026-07-01;
        NOT yet live-verified. SURFACES errors (does NOT swallow to []) so a
        failed fetch is distinguishable from 'no recent patients'."""
        try:
            client = await self.ensure_sis()
            return await client.get_recent_patients(from_date)
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching recent patients")
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS recent_patients failed: %s", e)
            raise

    async def sis_case_procedures(self, case_id: int) -> list[dict]:
        """Fetch procedures attached to a case from SIS by caseSummaryId.
        GET RecordHeader/{id}/GetCaseProcedurebyCase — HAR-derived 2026-07-01;
        NOT yet live-verified. SURFACES errors (does NOT swallow to [])."""
        try:
            client = await self.ensure_sis()
            return await client.get_case_procedures(int(case_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching case procedures (id=%s)", case_id)
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS case_procedures failed (id=%s): %s", case_id, e)
            raise

    async def sis_case_record(self, case_id: int) -> dict:
        """Fetch the chart/record header for a case from SIS by caseSummaryId.
        GET RecordHeader/{id}/GetPatientRecordByCaseSummaryId — HAR-derived
        2026-07-01; NOT yet live-verified."""
        try:
            client = await self.ensure_sis()
            return await client.get_case_record(int(case_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching case record (id=%s)", case_id)
            self._sis_connected = False
            return {"error": "session_expired", "case_id": case_id}
        except Exception as e:
            logger.error("SIS case_record failed (id=%s): %s", case_id, e)
            return {"error": str(e), "case_id": case_id}

    async def sis_case_diagnoses(self, case_id: int, procedure_id: int) -> list[dict]:
        """Fetch diagnoses linked to one procedure on a case from SIS.
        GET RecordHeader/{case}/{proc}/GetDiagnosisbyCaseAndProcedure —
        HAR-derived 2026-07-01; NOT yet live-verified. EXPERIMENTAL: two-slot
        path order ASSUMED (caseSummaryId, caseProcedureId — the latter from
        get_case_procedures rows). SURFACES errors (does NOT swallow to [])."""
        try:
            client = await self.ensure_sis()
            return await client.get_case_diagnoses(int(case_id), int(procedure_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching case diagnoses (id=%s)", case_id)
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS case_diagnoses failed (id=%s proc=%s): %s",
                         case_id, procedure_id, e)
            raise

    async def sis_worklist_case_details(self, case_id: int, module_id: int = 1020) -> dict:
        """Fetch the worklist view of a case for one clinical module from SIS.
        GET WorklistCaseDetails/GetCaseDetails/{case}/{module} — HAR-derived
        2026-07-01; NOT yet live-verified. Default module_id 1020 = Pre-Operative."""
        try:
            client = await self.ensure_sis()
            return await client.get_worklist_case_details(int(case_id), int(module_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching worklist case (id=%s)", case_id)
            self._sis_connected = False
            return {"error": "session_expired", "case_id": case_id, "module_id": module_id}
        except Exception as e:
            logger.error("SIS worklist_case_details failed (id=%s): %s", case_id, e)
            return {"error": str(e), "case_id": case_id, "module_id": module_id}

    async def sis_facesheet_case_detail(self, patient_id: int, case_id: int) -> dict:
        """Fetch the face-sheet case-detail block for one case from SIS.
        GET PatientFacesheet/GetCaseDetailInformation/{patient}/{case} —
        HAR-derived 2026-07-01; NOT yet live-verified (slot order (patientId,
        caseId) observed directly in the HAR)."""
        try:
            client = await self.ensure_sis()
            return await client.get_facesheet_case_detail(int(patient_id), int(case_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching facesheet case detail (case=%s)", case_id)
            self._sis_connected = False
            return {"error": "session_expired", "patient_id": patient_id, "case_id": case_id}
        except Exception as e:
            logger.error("SIS facesheet_case_detail failed (case=%s): %s", case_id, e)
            return {"error": str(e), "patient_id": patient_id, "case_id": case_id}

    async def sis_cover_page(
        self, patient_id: int, case_id: int, module_id: int = 1010
    ) -> dict:
        """Fetch chart cover-page info for a case within one clinical module.
        GET PatientFacesheet/GetCoverPageInformation/{patient}/{case}/{module} —
        HAR-derived 2026-07-01; NOT yet live-verified. Default 1010 = Pre-Admission."""
        try:
            client = await self.ensure_sis()
            return await client.get_cover_page(int(patient_id), int(case_id), int(module_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching cover page (case=%s)", case_id)
            self._sis_connected = False
            return {"error": "session_expired", "patient_id": patient_id,
                    "case_id": case_id, "module_id": module_id}
        except Exception as e:
            logger.error("SIS cover_page failed (case=%s): %s", case_id, e)
            return {"error": str(e), "patient_id": patient_id,
                    "case_id": case_id, "module_id": module_id}

    async def sis_chart_attachments_meta(self, patient_id: int, case_id: int) -> list[dict]:
        """Fetch chart-attachment METADATA (no file contents) from SIS.
        GET Attachments/v2/AllAttachmentsMetaData/{patient}/{case} —
        HAR-derived 2026-07-01; NOT yet live-verified. EXPERIMENTAL: the
        (patientId, caseId) slot order is ASSUMED from adjacent
        PatientFacesheet routes. SURFACES errors (does NOT swallow to [])."""
        try:
            client = await self.ensure_sis()
            return await client.get_chart_attachments_meta(int(patient_id), int(case_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching attachments meta (case=%s)", case_id)
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS chart_attachments_meta failed (case=%s): %s", case_id, e)
            raise

    async def sis_attachment_types(self) -> list[dict]:
        """Fetch the attachment-type lookup table from SIS (reference data, no PHI).
        GET ConfigAttachmentType/GetAllAttachmentTypes — HAR-derived
        2026-07-01; NOT yet live-verified. SURFACES errors."""
        try:
            client = await self.ensure_sis()
            return await client.get_attachment_types()
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching attachment types")
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS attachment_types failed: %s", e)
            raise

    async def sis_chart_attachment_consent_info(self, patient_id: int) -> dict:
        """Fetch the combined chart-attachment + consent status block from SIS.
        GET PatientFacesheet/GetPatientChartAttachmentAndConsentInfoV2/{patient}
        — HAR-derived 2026-07-01; NOT yet live-verified. EXPERIMENTAL: the
        single path slot's patientId reading is unconfirmed."""
        try:
            client = await self.ensure_sis()
            return await client.get_chart_attachment_consent_info(int(patient_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching attachment/consent info (id=%s)",
                           patient_id)
            self._sis_connected = False
            return {"error": "session_expired", "patient_id": patient_id}
        except Exception as e:
            logger.error("SIS chart_attachment_consent_info failed (id=%s): %s",
                         patient_id, e)
            return {"error": str(e), "patient_id": patient_id}

    async def sis_consent_signed(self, case_id: int):
        """Whether every consent on a case is signed, from SIS.
        GET ConsentClinical/IsAllConsentsSignedOfACase/{case} — HAR-derived
        2026-07-01; NOT yet live-verified. Returned as-is (expected bare JSON
        boolean, not assumed until live-verified). SURFACES errors — the raw
        payload could be mistaken for data, so failures raise."""
        try:
            client = await self.ensure_sis()
            return await client.get_consent_signed(int(case_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching consent status (id=%s)", case_id)
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS consent_signed failed (id=%s): %s", case_id, e)
            raise

    async def sis_hp_previously_signed(self, patient_id: int):
        """Previously signed H&P documents for a patient, from SIS.
        GET HistoryPhysicalClinical/PreviouslySignedForPatient/{patient} —
        HAR-derived 2026-07-01; NOT yet live-verified. Returned as-is.
        SURFACES errors."""
        try:
            client = await self.ensure_sis()
            return await client.get_hp_previously_signed(int(patient_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching signed H&P (id=%s)", patient_id)
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS hp_previously_signed failed (id=%s): %s", patient_id, e)
            raise

    async def sis_risk_assessments(self, case_id: int, second_id: int) -> list[dict]:
        """Fetch all six risk assessments for a case from SIS
        (DvtPrevention/DvtRisk/Fall/Fire/Ponv/StopBang).
        GET v2/CaseSummary/{case}/RiskAssessment/{kind}/{second_id} —
        HAR-derived 2026-07-01; NOT yet live-verified. EXPERIMENTAL: second_id
        is a module-id-like value whose semantics are unconfirmed. Each entry
        is tri-stated: recorded True/False, or None + error when that kind's
        call failed. SURFACES wrapper-level errors."""
        try:
            client = await self.ensure_sis()
            return await client.get_risk_assessments(int(case_id), int(second_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching risk assessments (id=%s)", case_id)
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS risk_assessments failed (id=%s): %s", case_id, e)
            raise

    async def sis_case_vitals(self, case_id: int) -> dict:
        """Fetch case vitals / basic-module info from SIS (two calls combined:
        BasicModuleInfo/GetBasicModuleInfoByCaseSummaryId + GetHtWtLastUpdated).
        HAR-derived 2026-07-01; NOT yet live-verified. Returns
        {basic_module, ht_wt_last_updated}; either may legitimately be empty."""
        try:
            client = await self.ensure_sis()
            return await client.get_case_vitals(int(case_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching case vitals (id=%s)", case_id)
            self._sis_connected = False
            return {"error": "session_expired", "case_id": case_id}
        except Exception as e:
            logger.error("SIS case_vitals failed (id=%s): %s", case_id, e)
            return {"error": str(e), "case_id": case_id}

    async def sis_block_schedule(self, start_iso: str, end_iso: str) -> list[dict]:
        """Fetch the OR block-time schedule for a date range from SIS.
        POST BlockSchedule/GetBlockScheduleData — HAR-derived 2026-07-01;
        NOT yet live-verified. start/end are ISO 8601 UTC (.000Z/.999Z
        04:00Z-anchor convention, see get_schedule_day). SURFACES errors."""
        try:
            client = await self.ensure_sis()
            return await client.get_block_schedule(start_iso, end_iso)
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching block schedule")
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS block_schedule failed: %s", e)
            raise

    async def sis_anesthesia_schedule(self, start_iso: str, end_iso: str) -> list[dict]:
        """Fetch the anesthesia-side schedule for a date range from SIS.
        GET AnesthesiaScheduling/ (trailing slash; startDate/endDate query
        params) — HAR-derived 2026-07-01; NOT yet live-verified. SURFACES errors."""
        try:
            client = await self.ensure_sis()
            return await client.get_anesthesia_schedule(start_iso, end_iso)
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching anesthesia schedule")
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS anesthesia_schedule failed: %s", e)
            raise

    async def sis_primary_physician(self, case_id: int):
        """Fetch the primary physician for a case from SIS.
        GET Staff/GetPrimaryPhysicianFromCase?caseSummaryId={case} —
        HAR-derived 2026-07-01; NOT yet live-verified. Returned as-is (may be
        a dict or bare string). SURFACES errors."""
        try:
            client = await self.ensure_sis()
            return await client.get_primary_physician(int(case_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching primary physician (id=%s)", case_id)
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS primary_physician failed (id=%s): %s", case_id, e)
            raise

    async def sis_user_permissions(self) -> dict:
        """Fetch the current SIS user's permission set + role (no PHI).
        GET Security/Permissions + GET Security/UserRole — HAR-derived
        2026-07-01; NOT yet live-verified. Returns {permissions, role}."""
        try:
            client = await self.ensure_sis()
            return await client.get_user_permissions()
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching user permissions")
            self._sis_connected = False
            return {"error": "session_expired"}
        except Exception as e:
            logger.error("SIS user_permissions failed: %s", e)
            return {"error": str(e)}

    async def sis_report_pdf(
        self,
        report_name: str,
        case_id: int,
        module_id: int = None,
        out_dir: str = None,
    ) -> dict:
        """Fetch an SSRS chart print-out PDF from SIS and save it to disk.
        POST Reports/SSRSReport/GetReportAsPdfAndAudit/... — HAR-derived
        2026-07-01; NOT yet live-verified from this client. Still a READ, but
        the endpoint writes an access-audit row on the SIS side per call (by
        design) — don't call in tight loops. The client verifies the %PDF-
        magic and NEVER saves a non-PDF body; it returns an honest error dict
        instead."""
        try:
            client = await self.ensure_sis()
            return await client.get_report_pdf(
                report_name, int(case_id), module_id, out_dir
            )
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching report PDF (case=%s)", case_id)
            self._sis_connected = False
            return {"error": "session_expired", "report_name": report_name,
                    "case_id": case_id}
        except Exception as e:
            logger.error("SIS report_pdf failed (case=%s): %s", case_id, e)
            return {"error": str(e), "report_name": report_name, "case_id": case_id}

    # ------------------------------------------------------------------
    # SIS RCM / billing tracker wrappers
    # (routes HAR-derived 2026-07-01; NOT yet live-verified)
    # ------------------------------------------------------------------

    async def sis_case_charges(self, case_id: int) -> list[dict]:
        """Fetch all charge rows for a case (revenue-cycle view) from SIS.
        GET RCMTracker/RCMGetAllChargesByCaseSummaryId/{case} — HAR-derived
        2026-07-01; NOT yet live-verified. SURFACES errors (does NOT swallow
        to []) — a failed fetch must not read as 'no charges'."""
        try:
            client = await self.ensure_sis()
            return await client.get_case_charges(int(case_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching case charges (id=%s)", case_id)
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS case_charges failed (id=%s): %s", case_id, e)
            raise

    async def sis_case_responsible_parties(self, case_id: int) -> dict:
        """Fetch insurance + guarantor responsible parties for a case from SIS
        (RCMTracker/RCMInsuranceResponsibleParty + RCMGuarantorResponsibleParty).
        HAR-derived 2026-07-01; NOT yet live-verified. Returns
        {insurance, guarantor}; either may legitimately be empty."""
        try:
            client = await self.ensure_sis()
            return await client.get_case_responsible_parties(int(case_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching responsible parties (id=%s)", case_id)
            self._sis_connected = False
            return {"error": "session_expired", "case_id": case_id}
        except Exception as e:
            logger.error("SIS case_responsible_parties failed (id=%s): %s", case_id, e)
            return {"error": str(e), "case_id": case_id}

    async def sis_charges_for_insurance(self, party_id: int, case_id: int) -> list[dict]:
        """Fetch charges attributed to one insurance responsible party on a case.
        GET RCMTracker/RCMChargesForInsurance/{party}/{case} — HAR-derived
        2026-07-01; NOT yet live-verified. EXPERIMENTAL: the FIRST path slot is
        ASSUMED to be a responsible-party/carrier id (likely from
        get_case_responsible_parties' insurance rows). SURFACES errors."""
        try:
            client = await self.ensure_sis()
            return await client.get_charges_for_insurance(int(party_id), int(case_id))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching insurance charges (case=%s)", case_id)
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS charges_for_insurance failed (party=%s case=%s): %s",
                         party_id, case_id, e)
            raise

    async def sis_insurance_verification_queue(
        self, dos_from: str, dos_to: str, page: int = 1
    ) -> list[dict]:
        """Fetch the insurance-verification work queue for a DOS window from SIS.
        POST InsuranceTracker/GetTrackerData — HAR-derived 2026-07-01; NOT yet
        live-verified. BEST-EFFORT BODY: the HAR gave key names only; the
        default filter values are guesses, and a server rejection surfaces as
        a normal HTTP error. SURFACES errors."""
        try:
            client = await self.ensure_sis()
            return await client.get_insurance_verification_queue(dos_from, dos_to, int(page))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching insurance verification queue")
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS insurance_verification_queue failed: %s", e)
            raise

    async def sis_billing_tracker(self) -> list[dict]:
        """Fetch the practice-wide insurance billing tracker from SIS.
        GET InsuranceBillingTracker/GetTrackerData — HAR-derived 2026-07-01;
        NOT yet live-verified. SURFACES errors."""
        try:
            client = await self.ensure_sis()
            return await client.get_billing_tracker()
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching billing tracker")
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS billing_tracker failed: %s", e)
            raise

    async def sis_charge_entry_tracker(self, date_time: str = None) -> list[dict]:
        """Fetch the charge-entry work queue from SIS.
        POST ChargeEntryTracker/GetTrackerData — HAR-derived 2026-07-01; NOT
        yet live-verified. Body mirrors the UnsignedCasesTracker contract
        exactly (dateTime/pageNumber=1/columnId=3/orderType=0). SURFACES errors."""
        try:
            client = await self.ensure_sis()
            return await client.get_charge_entry_tracker(date_time)
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching charge-entry tracker")
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS charge_entry_tracker failed: %s", e)
            raise

    async def sis_clinical_doc_tracker(self, date_time: str = None) -> list[dict]:
        """Fetch the clinical-documentation work queue from SIS.
        POST ClinicalDocumentationTracker/GetTrackerData — HAR-derived
        2026-07-01; NOT yet live-verified. Same body shape as the charge-entry
        tracker. SURFACES errors."""
        try:
            client = await self.ensure_sis()
            return await client.get_clinical_doc_tracker(date_time)
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching clinical-doc tracker")
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS clinical_doc_tracker failed: %s", e)
            raise

    async def sis_case_coordination(self, page: int = 1) -> dict:
        """Fetch the case-coordination request queue + total count from SIS
        (CaseCoordinationTracker/GetTrackerData + GetTrackerDataCount, same
        body). HAR-derived 2026-07-01; NOT yet live-verified. Returns
        {count, rows}; an empty rows list is a valid result."""
        try:
            client = await self.ensure_sis()
            return await client.get_case_coordination(int(page))
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching case coordination")
            self._sis_connected = False
            return {"error": "session_expired", "page": page}
        except Exception as e:
            logger.error("SIS case_coordination failed: %s", e)
            return {"error": str(e), "page": page}

    async def sis_insurance_carriers(self) -> list[dict]:
        """Fetch the insurance-carrier lookup table from SIS (reference data).
        GET InsuranceCarrierObject/GetInsuranceCarriers — HAR-derived
        2026-07-01; NOT yet live-verified. SURFACES errors."""
        try:
            client = await self.ensure_sis()
            return await client.get_insurance_carriers()
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching insurance carriers")
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS insurance_carriers failed: %s", e)
            raise

    async def sis_transaction_codes(self, type_id: int = None) -> list[dict]:
        """Fetch the transaction-code lookup table from SIS (reference data).
        GET TransactionCode/GetTransactionCodeList (or
        GetTransactionCodeListByType/{type_id} when type_id is given) —
        HAR-derived 2026-07-01; NOT yet live-verified. SURFACES errors."""
        try:
            client = await self.ensure_sis()
            return await client.get_transaction_codes(type_id)
        except EMRSessionExpired:
            logger.warning("SIS session expired fetching transaction codes")
            self._sis_connected = False
            raise
        except Exception as e:
            logger.error("SIS transaction_codes failed: %s", e)
            raise

    # ------------------------------------------------------------------
    # Svigg convenience wrappers
    # ------------------------------------------------------------------

    async def svigg_search(self, last_name: str, first_name: str = "") -> list[dict]:
        """Search Svigg patients by last name (and optional first name)."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.search_patient(last_name, first_name)
        except Exception as e:
            logger.error("Svigg search failed: %s", e)
            self._svigg_connected = False
            return []

    async def svigg_patient_summary(self, last_name: str, first_name: str = "") -> dict:
        """
        Search Svigg and return the full patient summary for the first match.
        Returns empty dict if no match or on error.
        """
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                results = await scraper.search_and_summarize(last_name, first_name)
            return results[0] if results else {}
        except Exception as e:
            logger.error("Svigg patient summary failed: %s", e)
            self._svigg_connected = False
            return {}

    async def svigg_patient_ledger(self, account_number: str, rowid: str = "") -> dict:
        """Fetch visit/charge ledger for a Svigg patient by account number.

        rowid is REQUIRED to reach the actual charges ledger: acct-only lands on
        the Visit-Entry search form and returns zero charges. Pass the rowid
        harvested alongside acct from a prior search_patient()/svigg_search()
        result (pentry.htm?rowid=...&acct=...).
        """
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_patient_ledger(account_number, rowid)
        except Exception as e:
            logger.error(
                "Svigg patient_ledger failed (acct=%s rowid=%s): %s",
                account_number, rowid or "(none)", e,
            )
            self._svigg_connected = False
            return {
                "account_number": account_number,
                "error": str(e),
                "charges": [],
                "payments": [],
                "balance": None,
                "source": "svigg_live",
            }

    async def svigg_patient_appointments(self, account_number: str) -> list[dict]:
        """
        Get appointment history for a Svigg patient.
        Inferred from visit ledger dates — no dedicated appointments endpoint exists.
        """
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_patient_appointments(account_number)
        except Exception as e:
            logger.error("Svigg patient_appointments failed (acct=%s): %s", account_number, e)
            self._svigg_connected = False
            return []

    async def svigg_appointment_calendar(self, date: str = None) -> list[dict]:
        """Scrape the Svigg global appointment calendar for a given date."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_appointment_calendar(date)
        except Exception as e:
            logger.error("Svigg appointment_calendar failed (date=%s): %s", date, e)
            self._svigg_connected = False
            return [{"error": str(e), "date": date, "source": "svigg_live"}]

    async def svigg_schedule_day(self, day_offset: int = 0) -> dict:
        """READ-ONLY: scrape the Svigg per-day schedule report by day offset.

        Serialized through _svigg_lock like every other Svigg operation so the
        single shared browser page is never navigated concurrently. Read-only —
        only fetches the appt_b.htm schedule report.
        """
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_schedule_day(day_offset)
        except Exception as e:
            logger.error("Svigg schedule_day failed (offset=%s): %s", day_offset, e)
            self._svigg_connected = False
            return {"date": "", "day_offset": day_offset, "count": 0,
                    "appointments": [], "error": str(e), "source": "svigg_live"}

    async def svigg_book_appointment(self, **kwargs) -> dict:
        """Prepare (and, if explicitly unlocked, commit) a Svigg appointment.

        Thin wrapper over SviggScraper.book_appointment. PROPOSE-ONLY by
        default (execute=False) — prepares the bk_p form and returns WITHOUT
        submitting, creating nothing. The commit path is UNVERIFIED (HAR
        pending) and double-gated inside the scraper; this wrapper passes the
        caller's kwargs straight through and does NOT relax any guard.
        """
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.book_appointment(**kwargs)
        except Exception as e:
            logger.error("Svigg book_appointment failed: %s", e)
            self._svigg_connected = False
            return {"status": "error", "stage": "session",
                    "error": str(e), "source": "svigg_live"}

    async def svigg_cancel_appointment(self, **kwargs) -> dict:
        """Cancel a Svigg appointment (DESTRUCTIVE). Thin wrapper over
        SviggScraper.cancel_appointment — fail-closed inside the scraper
        (requires confirm=True AND an allowlisted acct). Passes kwargs straight
        through and relaxes no guard. Cancel flow verified live 2026-07-01."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.cancel_appointment(**kwargs)
        except Exception as e:
            logger.error("Svigg cancel_appointment failed: %s", e)
            self._svigg_connected = False
            return {"status": "error", "stage": "session",
                    "error": str(e), "source": "svigg_live"}

    async def svigg_create_patient(self, demographics: dict, **kwargs) -> dict:
        """Create (propose, and only if explicitly unlocked, commit) a NEW
        Svigg patient chart.

        Thin wrapper over SviggScraper.create_patient. DRY-RUN by default
        (dry_run=True) — walks the entry flow, de-dupes, discovers the add-form
        fields and returns WITHOUT saving, creating nothing. The commit path is
        UNVERIFIED (Save POST not in HAR) and double-gated inside the scraper;
        this wrapper passes kwargs straight through and relaxes no guard.
        """
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.create_patient(demographics, **kwargs)
        except Exception as e:
            logger.error("Svigg create_patient failed: %s", e)
            self._svigg_connected = False
            return {"status": "error", "stage": "session",
                    "error": str(e), "source": "svigg_live"}

    async def svigg_update_patient(
        self, last_name: str, first_name: str, updates: dict, **kwargs
    ) -> dict:
        """Edit (propose, and only if explicitly unlocked, commit) an EXISTING
        Svigg patient's demographic fields.

        Thin wrapper over SviggScraper.update_patient. DRY-RUN by default
        (dry_run=True) — resolves the chart, opens the pre-filled edit form,
        reports current + proposed values and returns WITHOUT saving. The commit
        path is HAR-confirmed but identity-guarded + triple-gated
        (SVIGG_EDIT_EXECUTE=1 + dry_run=False + confirm_unverified=True) +
        verify-after inside the scraper; this wrapper passes kwargs straight
        through and relaxes no guard. Serialized under the shared svigg lock so
        the write never races an in-flight read."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.update_patient(
                    last_name, first_name, updates, **kwargs)
        except Exception as e:
            logger.error("Svigg update_patient failed: %s", e)
            self._svigg_connected = False
            return {"status": "error", "stage": "session",
                    "error": str(e), "source": "svigg_live"}

    # ------------------------------------------------------------------
    # Svigg clinical-module / report / calendar wrappers
    # (routes captured live 2026-07-01; parses HAR-derived, NOT yet
    #  live-verified — see each scraper method's docstring)
    # ------------------------------------------------------------------

    async def svigg_problem_list(self, acct: str, rowid: str) -> dict:
        """Fetch a Svigg patient's problem list (apps/enc/plist.htm).
        Route captured live 2026-07-01; table parse HAR-derived 2026-07-01,
        NOT yet live-verified — raw rows, column semantics unmapped."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_problem_list(acct, rowid)
        except Exception as e:
            logger.error("Svigg problem_list failed (acct=%s): %s", acct, e)
            self._svigg_connected = False
            return {"account_number": acct, "rowid": rowid, "problems": [],
                    "problems_header": [], "raw_tables": [], "text_excerpt": "",
                    "status": "error", "error": str(e), "source": "svigg_live"}

    async def svigg_patient_display(self, acct: str, rowid: str, tab: str) -> dict:
        """Fetch one tab of the Svigg pdisplay patient-display family
        (Info/Clinical/Scheduling/Ledger/Bills/ProcedureLedger). Routes
        captured live 2026-07-01 (HTTP 204 = empty tab); parse HAR-derived
        2026-07-01, NOT yet live-verified. NOT the system-of-record for
        balances — that remains svigg_patient_ledger."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_patient_display(acct, rowid, tab)
        except Exception as e:
            logger.error("Svigg patient_display failed (acct=%s tab=%s): %s",
                         acct, tab, e)
            self._svigg_connected = False
            return {"tab": tab, "account_number": acct, "rowid": rowid,
                    "tables": [], "text_excerpt": "", "status": "error",
                    "error": str(e), "source": "svigg_live"}

    async def svigg_patient_rx(self, acct: str, rowid: str) -> dict:
        """Fetch a Svigg patient's medications module (apps/rx/PtntRx.htm).
        Route captured live 2026-07-01 (HTTP 204 = empty module); parse
        HAR-derived 2026-07-01, NOT yet live-verified."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_patient_rx(acct, rowid)
        except Exception as e:
            logger.error("Svigg patient_rx failed (acct=%s): %s", acct, e)
            self._svigg_connected = False
            return {"account_number": acct, "rowid": rowid, "tables": [],
                    "text_excerpt": "", "status": "error", "error": str(e),
                    "source": "svigg_live"}

    async def svigg_patient_allergies(self, acct: str, rowid: str) -> dict:
        """Fetch a Svigg patient's allergies module (apps/aller/PtntAller.htm).
        Route captured live 2026-07-01 (HTTP 204 = empty module); parse
        HAR-derived 2026-07-01, NOT yet live-verified."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_patient_allergies(acct, rowid)
        except Exception as e:
            logger.error("Svigg patient_allergies failed (acct=%s): %s", acct, e)
            self._svigg_connected = False
            return {"account_number": acct, "rowid": rowid, "tables": [],
                    "text_excerpt": "", "status": "error", "error": str(e),
                    "source": "svigg_live"}

    async def svigg_patient_immunizations(self, acct: str, rowid: str) -> dict:
        """Fetch a Svigg patient's immunizations module (apps/immun/PtntImmun.htm).
        Route captured live 2026-07-01 (HTTP 204 = empty module); parse
        HAR-derived 2026-07-01, NOT yet live-verified."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_patient_immunizations(acct, rowid)
        except Exception as e:
            logger.error("Svigg patient_immunizations failed (acct=%s): %s", acct, e)
            self._svigg_connected = False
            return {"account_number": acct, "rowid": rowid, "tables": [],
                    "text_excerpt": "", "status": "error", "error": str(e),
                    "source": "svigg_live"}

    async def svigg_scan_review_queue(self, provider: str = "", category: str = "",
                                      scan_type: str = "") -> dict:
        """Fetch the Svigg doctor-review scan queue
        (apps/scan/scanList_DrReview.htm; blank filters = all). Route captured
        live 2026-07-01; queue-table parse HAR-derived 2026-07-01, NOT yet
        live-verified — column semantics unmapped."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_scan_review_queue(provider, category, scan_type)
        except Exception as e:
            logger.error("Svigg scan_review_queue failed: %s", e)
            self._svigg_connected = False
            return {"queue": [], "queue_header": [], "raw_tables_count": 0,
                    "filters": {"provider": provider, "category": category,
                                "scan_type": scan_type},
                    "status": "error", "error": str(e), "source": "svigg_live"}

    async def svigg_office_todo(self) -> dict:
        """Fetch the Svigg built-in office to-do widget (off/home/todo_b.htm).
        Route captured live 2026-07-01; row parse HAR-derived 2026-07-01,
        NOT yet live-verified — raw rows, semantics unmapped."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_office_todo()
        except Exception as e:
            logger.error("Svigg office_todo failed: %s", e)
            self._svigg_connected = False
            return {"items": [], "raw_tables_count": 0, "text_excerpt": "",
                    "status": "error", "error": str(e), "source": "svigg_live"}

    async def svigg_office_notes(self) -> dict:
        """Fetch the Svigg built-in office notes widget (off/home/notes_b.htm).
        Route captured live 2026-07-01; row parse HAR-derived 2026-07-01,
        NOT yet live-verified — raw rows, semantics unmapped."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_office_notes()
        except Exception as e:
            logger.error("Svigg office_notes failed: %s", e)
            self._svigg_connected = False
            return {"items": [], "raw_tables_count": 0, "text_excerpt": "",
                    "status": "error", "error": str(e), "source": "svigg_live"}

    async def svigg_open_balances_report(self, **filters) -> dict:
        """Run the practice-wide Svigg Open Balances report
        (off/reports/openbal.htm; form defaults preserved, **filters
        best-effort matched to same-named controls, unmatched names reported
        in fields_failed). Route captured live 2026-07-01; row parse
        HAR-derived 2026-07-01, NOT yet live-verified."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_open_balances_report(**filters)
        except Exception as e:
            logger.error("Svigg open_balances_report failed: %s", e)
            self._svigg_connected = False
            return {"rows": [], "header": [], "row_count": 0,
                    "status": "error", "error": str(e), "source": "svigg_live"}

    async def svigg_referrals_in_report(self, date_from: str = "",
                                        date_to: str = "") -> dict:
        """Run the Svigg referrals-received report (off/reports/refIn.htm).
        Route captured live 2026-07-01; row parse HAR-derived 2026-07-01, NOT
        yet live-verified. Date-control names are resolved at runtime
        (DtFrom/FromDate/from, DtTo/ToDate/until); unmatched dates are
        reported in fields_failed, never silently guessed."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_referrals_in_report(date_from, date_to)
        except Exception as e:
            logger.error("Svigg referrals_in_report failed: %s", e)
            self._svigg_connected = False
            return {"rows": [], "header": [], "row_count": 0,
                    "status": "error", "error": str(e), "source": "svigg_live"}

    async def svigg_payments_by_provider(self, date_from: str = "",
                                         date_to: str = "") -> dict:
        """Run the Svigg payments-by-provider report (off/reports/paymProv.htm;
        DtFrom/DtTo are the captured date controls, all other keys come from
        the form's own rendered defaults). Route + result contract captured
        live 2026-07-01; row parse HAR-derived 2026-07-01, NOT yet
        live-verified."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_payments_by_provider(date_from, date_to)
        except Exception as e:
            logger.error("Svigg payments_by_provider failed: %s", e)
            self._svigg_connected = False
            return {"rows": [], "header": [], "row_count": 0,
                    "status": "error", "error": str(e), "source": "svigg_live"}

    async def svigg_search_open_slots(self, appt_type: str = "", office: str = "",
                                      cpt: str = "", duration_min: int = None,
                                      date_from: str = "", date_to: str = "") -> dict:
        """Search Svigg open appointment slots (sched.htm; form defaults
        preserved, only given filters overridden, fresh session token from the
        calendar frameset). Route captured live 2026-07-01/07-03; result-page
        parse HAR-derived, NOT yet live-verified, and the result-row semantics
        are EXPERIMENTAL — candidate_slots are parsed rows carrying a
        time-like token, not confirmed bookable slots."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.search_open_slots(
                    appt_type=appt_type, office=office, cpt=cpt,
                    duration_min=duration_min, date_from=date_from,
                    date_to=date_to)
        except Exception as e:
            logger.error("Svigg search_open_slots failed: %s", e)
            self._svigg_connected = False
            return {"candidate_slots": [], "rows": [], "header": [],
                    "fields_applied": [], "fields_failed": [],
                    "status": "error", "error": str(e), "source": "svigg_live"}

    async def svigg_oneday_view(self, date_mdy: str, resource_id: str) -> dict:
        """Fetch the Svigg single-day calendar view for one resource
        ({resource_id}/oneday.htm?dt=MM/DD/YYYY; fresh session token from the
        calendar frameset). Route captured live 2026-07-01/07-03; parse
        HAR-derived, NOT yet live-verified. EXPERIMENTAL: resource_id
        semantics are unconfirmed (likely a provider/resource id) — do NOT
        treat rows as belonging to a specific provider until verified live."""
        try:
            scraper = await self.ensure_svigg()
            async with self._svigg_lock:
                return await scraper.get_oneday_view(date_mdy, resource_id)
        except Exception as e:
            logger.error("Svigg oneday_view failed (resource=%s): %s", resource_id, e)
            self._svigg_connected = False
            return {"date": date_mdy, "resource_id": resource_id, "tables": [],
                    "rows": [], "text_excerpt": "", "status": "error",
                    "error": str(e), "source": "svigg_live"}

    async def svigg_prepare_appointment_update(self, ticket_number: str,
                                               room: str = "", staged: str = "",
                                               note: str = "",
                                               missed: str = "") -> dict:
        """PREPARE-ONLY: build (never POST) the Svigg appt_u.htm front-desk
        check-in/staging payload. Target captured live 2026-07-03. The scraper
        method performs ZERO network I/O and deliberately has NO submit code
        path — execution is blocked until one supervised live run; this
        wrapper relaxes no guard. Returns status=prepared + would_post +
        unknown_defaults.

        Deliberately NO ensure_svigg()/_svigg_lock here: the builder is pure
        (it reads only the class-level BASE_URL), and the MCP tool advertises
        that a prepare call never touches the portal — so it must not trigger
        a live Playwright login (real portal session activity, lockout/rate
        risk, and a spurious error when the portal is unreachable). It runs
        on the manager's scraper INSTANCE without starting it."""
        try:
            return await self._svigg.prepare_appointment_update(
                ticket_number, room=room, staged=staged, note=note,
                missed=missed)
        except Exception as e:
            logger.error("Svigg prepare_appointment_update failed (ticket=%s): %s",
                         ticket_number, e)
            return {"status": "error", "error": str(e), "source": "svigg_live"}

    # ------------------------------------------------------------------
    # Combined / cross-EMR methods
    # ------------------------------------------------------------------

    _NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "md", "do", "esq"}

    @staticmethod
    def _svigg_name_interpretations(query: str) -> list:
        """(last, first) interpretations of a free-text patient name, most
        likely first.

        Staff type names in NATURAL order ("Shamell Austin" = first last), but
        Svigg's search form is last-name keyed — reading the FIRST token as the
        last name (the old behavior) searched last_name="Shamell" and reported
        a real, findable patient as absent from the EMR (live failure
        2026-07-04, Svigg acct present all along). Rules, in priority order
        (the original two orderings keep their priority; everything else is
        additive):

          * "Last, First"    -> the comma is authoritative: FIRST interpretation.
          * "First [M] Last" -> natural order (last = FINAL token) is next,
                                then the swapped reading (last = first token)
                                as a fallback so "Austin Shamell" phrasing
                                still hits. These two keep their original
                                priority relative to each other.
          * 3+ tokens        -> also try (last=last_token,
                                first=everything else joined) [same as
                                natural, already covered] and
                                (last=first_token, first=everything else
                                joined) for names like "Mary Jo Smith" where
                                the middle token is part of a compound first
                                name rather than a true middle name.
          * hyphenated last  -> also try the pre-hyphen component as the last
                                name (e.g. "Smith-Jones" -> also try "Smith").
          * single token     -> last-name-only search.

        Generational/professional suffixes (Jr, Sr, II, III, IV, MD, DO, Esq
        — case-insensitive, trailing only) are stripped before building
        interpretations so "Robert Smith Jr" searches as "Robert Smith".

        Capped at 4 interpretations (portal load). _svigg_search_all_orders
        stops at the first interpretation that returns a non-empty result.
        """
        q = (query or "").strip()
        if not q:
            return []

        has_comma = "," in q

        def _strip_suffix(tokens: list) -> list:
            if len(tokens) > 1 and tokens[-1].strip(".").lower() in EMRSessionManager._NAME_SUFFIXES:
                return tokens[:-1]
            return tokens

        def _add(interps: list, pair: tuple) -> None:
            if pair[0] and pair not in interps:
                interps.append(pair)

        if has_comma:
            last, _, first = q.partition(",")
            last, first = last.strip(), first.strip()
            interps: list = []
            _add(interps, (last, first))
            # Also offer natural-order fallbacks from the de-comma'd tokens,
            # in case the comma was mis-typed (e.g. "Austin, Shamell" typo'd
            # as a natural name with a stray comma).
            tokens = _strip_suffix((first + " " + last).split()) if first else _strip_suffix([last])
            if len(tokens) >= 2:
                _add(interps, (tokens[-1], " ".join(tokens[:-1])))
            return interps[:4]

        tokens = _strip_suffix(q.split())
        if len(tokens) == 1:
            return [(tokens[0], "")]

        interps: list = []
        _add(interps, (tokens[-1], " ".join(tokens[:-1])))          # natural: First .. Last
        _add(interps, (tokens[0], " ".join(tokens[1:])))            # legacy: Last First ..

        # 3+ tokens: the natural (last=last_token) and legacy (last=first_token)
        # readings above already cover "everything else joined" as the first
        # name for compound first names (e.g. "Mary Jo Smith" -> natural
        # reading is last="Smith", first="Mary Jo" — already included).

        last_token = tokens[-1]
        if "-" in last_token:
            pre_hyphen = last_token.split("-", 1)[0].strip()
            if pre_hyphen:
                _add(interps, (pre_hyphen, " ".join(tokens[:-1])))

        return interps[:4]

    async def _svigg_search_all_orders(self, query: str) -> list[dict]:
        """Search Svigg trying every name interpretation until one hits.

        Natural order first; on ZERO results falls back to the swapped order —
        a staff member's phrasing must never decide whether a real patient is
        found. Raises only if EVERY attempted interpretation raised, so a
        scrape failure is never disguised as "no matches" (house rule).
        """
        interps = self._svigg_name_interpretations(query)
        if not interps:
            return []
        last_error = None
        for last, first in interps:
            try:
                results = await self.svigg_search(last, first)
            except Exception as e:  # try the other order before giving up
                last_error = e
                continue
            if results:
                return results
        if last_error is not None:
            raise last_error
        return []

    async def combined_search(self, query: str) -> dict:
        """
        Search BOTH EMRs in parallel. Returns merged results.

        The Svigg arm tries the natural "First Last" reading first and falls
        back to the swapped order (see _svigg_name_interpretations). The SIS
        arm retries surname/first-name variants on a zero-result raw query
        (see sis_search / _sis_query_variants).

        HONESTY: a search arm that ERRORED is reported in "sis_error" /
        "svigg_error" AND in "sis_status"/"svigg_status" — an empty list
        always means the EMR really answered with zero matches, never a
        swallowed exception. If BOTH arms failed, "warning" makes explicit
        that the result proves nothing about whether the patient exists.

        {
            "sis_results": [...],
            "svigg_results": [...],
            "sis_error": None | str,
            "svigg_error": None | str,
            "sis_status": "ok" | "error" | "session_expired",
            "svigg_status": "ok" | "error" | "session_expired",
            "warning": None | str,
            "query": "...",
            "searched_at": "..."
        }
        """
        sis_task = self.sis_search(query)
        svigg_task = self._svigg_search_all_orders(query)

        sis_results, svigg_results = await asyncio.gather(
            sis_task, svigg_task, return_exceptions=True
        )

        sis_error = svigg_error = None
        sis_status = svigg_status = "ok"
        if isinstance(sis_results, Exception):
            logger.error("combined_search SIS arm failed: %s", type(sis_results).__name__)
            sis_error = f"{type(sis_results).__name__}: {sis_results}"
            sis_status = "session_expired" if isinstance(sis_results, EMRSessionExpired) else "error"
            sis_results = []
        if isinstance(svigg_results, Exception):
            logger.error("combined_search Svigg arm failed: %s", type(svigg_results).__name__)
            svigg_error = f"{type(svigg_results).__name__}: {svigg_results}"
            svigg_status = "session_expired" if isinstance(svigg_results, EMRSessionExpired) else "error"
            svigg_results = []

        warning = None
        if sis_status != "ok" and svigg_status != "ok":
            warning = "BOTH EMR SEARCHES FAILED — this result proves nothing about patient existence"

        return {
            "sis_results": sis_results,
            "svigg_results": svigg_results,
            "sis_error": sis_error,
            "svigg_error": svigg_error,
            "sis_status": sis_status,
            "svigg_status": svigg_status,
            "warning": warning,
            "query": query,
            "searched_at": datetime.now(timezone.utc).isoformat(),
        }

    async def combined_schedule(self, start_date: str = None, end_date: str = None) -> dict:
        """
        Fetch schedules from BOTH EMRs in parallel and merge.

        SIS: GET week via get_schedule_week (Mon-Sun window from start_date).
        Svigg: GET appointment calendar for the start_date (single day).

        Args:
            start_date: ISO date YYYY-MM-DD. Defaults to today for both.
            end_date:   Ignored for now (SIS uses full week; Svigg has no range calendar).
                        Reserved for future multi-day calendar iteration.

        Returns merged dict with SIS surgical cases + Svigg office appointments.
        """
        sis_task = self.sis_schedule_week(start_date)
        svigg_task = self.svigg_appointment_calendar(start_date)

        sis_results, svigg_results = await asyncio.gather(
            sis_task, svigg_task, return_exceptions=True
        )

        if isinstance(sis_results, Exception):
            logger.warning("combined_schedule SIS arm failed: %s", sis_results)
            sis_results = []
        if isinstance(svigg_results, Exception):
            logger.warning("combined_schedule Svigg arm failed: %s", svigg_results)
            svigg_results = []

        return {
            "sis_schedule": sis_results,
            "sis_count": len(sis_results) if isinstance(sis_results, list) else 0,
            "svigg_calendar": svigg_results,
            "svigg_count": len(svigg_results) if isinstance(svigg_results, list) else 0,
            "start_date": start_date,
            "end_date": end_date,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    async def patient_360(self, query: str) -> dict:
        """
        Unified patient view: search both EMRs, then fetch demographics + ledger.

        Steps (all run with generous error isolation):
          1. Search SIS by token (name/MRN).
          2. Search Svigg by name — natural "First Last" reading first, then
             the swapped order (see _svigg_name_interpretations), so staff
             phrasing never hides a real patient.
          3. If Svigg match found, fetch ledger for the first match.
          4. If SIS match found, fetch patient details for the first match's patientId.

        Args:
            query: Patient name (any format) or MRN.

        Returns unified dict — missing fields render as None, never as invented data.
        A search arm that ERRORED is surfaced in "search_errors" (never
        silently reported as zero matches). PHI note: do not log or echo the
        return value; pass directly to caller.
        """
        # Parallel search across both EMRs (Svigg tries both name orders)
        sis_search_task = self.sis_search(query)
        svigg_search_task = self._svigg_search_all_orders(query)

        sis_matches, svigg_matches = await asyncio.gather(
            sis_search_task, svigg_search_task, return_exceptions=True
        )

        search_errors: dict = {}
        if isinstance(sis_matches, Exception):
            logger.warning("patient_360 SIS search failed: %s", sis_matches)
            search_errors["sis"] = f"{type(sis_matches).__name__}: {sis_matches}"
            sis_matches = []
        if isinstance(svigg_matches, Exception):
            logger.warning("patient_360 Svigg search failed: %s", svigg_matches)
            search_errors["svigg"] = (
                f"{type(svigg_matches).__name__}: {svigg_matches}"
            )
            svigg_matches = []

        # Drill into first SIS match for full demographics
        sis_demographics = None
        sis_patient_id = None
        if sis_matches and isinstance(sis_matches, list) and len(sis_matches) > 0:
            first = sis_matches[0]
            pid = first.get("patientId") or first.get("PatientId") or first.get("id")
            if pid:
                sis_patient_id = int(pid)
                try:
                    sis_demographics = await self.sis_patient_details(sis_patient_id)
                except Exception as e:
                    logger.warning("patient_360 SIS demographics fetch failed: %s", e)

        # Drill into first Svigg match for ledger
        svigg_ledger = None
        svigg_acct = None
        svigg_rowid = None
        if svigg_matches and isinstance(svigg_matches, list) and len(svigg_matches) > 0:
            first = svigg_matches[0]
            svigg_acct = first.get("acct")
            # rowid is REQUIRED for real charges — acct-only serves the search
            # form. The search result already carries it; thread it through.
            svigg_rowid = first.get("rowid")
            if svigg_acct:
                try:
                    svigg_ledger = await self.svigg_patient_ledger(svigg_acct, svigg_rowid or "")
                except Exception as e:
                    logger.warning("patient_360 Svigg ledger fetch failed: %s", e)

        return {
            "query": query,
            "sis_match_count": len(sis_matches) if isinstance(sis_matches, list) else 0,
            "svigg_match_count": len(svigg_matches) if isinstance(svigg_matches, list) else 0,
            "sis_patient_id": sis_patient_id,
            "svigg_account": svigg_acct,
            "svigg_rowid": svigg_rowid,
            "sis_demographics": sis_demographics,
            "svigg_ledger": svigg_ledger,
            "sis_search_results": sis_matches,
            "svigg_search_results": svigg_matches,
            "search_errors": search_errors,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

async def _main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 emr_session_manager.py status")
        print("  python3 emr_session_manager.py connect")
        print("  python3 emr_session_manager.py search <name>")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cmd = sys.argv[1]
    mgr = EMRSessionManager.get_instance()

    if cmd == "status":
        # Quick health check — try to connect if not already
        await mgr.connect_all()
        h = await mgr.health()
        print(json.dumps(h, indent=2, default=str))

    elif cmd == "connect":
        await mgr.connect_all()
        h = await mgr.health()
        print(json.dumps(h, indent=2, default=str))

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: python3 emr_session_manager.py search <name>")
            sys.exit(1)
        query = " ".join(sys.argv[2:])
        await mgr.connect_all()
        results = await mgr.combined_search(query)
        # Print counts only — do NOT echo PHI to terminal in production
        print(
            f"SIS: {len(results['sis_results'])} result(s), "
            f"Svigg: {len(results['svigg_results'])} result(s) "
            f"for query: {query!r}"
        )
        if "--verbose" in sys.argv:
            print(json.dumps(results, indent=2, default=str))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)

    await mgr.disconnect_all()


if __name__ == "__main__":
    asyncio.run(_main())
