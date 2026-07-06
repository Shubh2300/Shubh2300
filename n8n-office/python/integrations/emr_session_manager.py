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
        """Search SIS patients by name/MRN/DOB token."""
        try:
            client = await self.ensure_sis()
            return await client.search_patient(query)
        except EMRSessionExpired:
            logger.warning("SIS session expired during search")
            self._sis_connected = False
            return []
        except Exception as e:
            logger.error("SIS search failed: %s", e)
            return []

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

    # ------------------------------------------------------------------
    # Combined / cross-EMR methods
    # ------------------------------------------------------------------

    async def combined_search(self, query: str) -> dict:
        """
        Search BOTH EMRs in parallel. Returns merged results.

        Svigg requires last_name; we use the first token of query as last_name
        and the rest (if any) as first_name.

        {
            "sis_results": [...],
            "svigg_results": [...],
            "query": "...",
            "searched_at": "..."
        }
        """
        tokens = query.strip().split()
        svigg_last = tokens[0] if tokens else query
        svigg_first = " ".join(tokens[1:]) if len(tokens) > 1 else ""

        sis_task = self.sis_search(query)
        svigg_task = self.svigg_search(svigg_last, svigg_first)

        sis_results, svigg_results = await asyncio.gather(
            sis_task, svigg_task, return_exceptions=True
        )

        if isinstance(sis_results, Exception):
            logger.error("combined_search SIS arm failed: %s", sis_results)
            sis_results = []
        if isinstance(svigg_results, Exception):
            logger.error("combined_search Svigg arm failed: %s", svigg_results)
            svigg_results = []

        return {
            "sis_results": sis_results,
            "svigg_results": svigg_results,
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
          2. Search Svigg by last name (first token of query).
          3. If Svigg match found, fetch ledger for the first match.
          4. If SIS match found, fetch patient details for the first match's patientId.

        Args:
            query: Patient name (any format) or MRN.

        Returns unified dict — missing fields render as None, never as invented data.
        PHI note: do not log or echo the return value; pass directly to caller.
        """
        tokens = query.strip().split()
        svigg_last = tokens[0] if tokens else query
        svigg_first = " ".join(tokens[1:]) if len(tokens) > 1 else ""

        # Parallel search across both EMRs
        sis_search_task = self.sis_search(query)
        svigg_search_task = self.svigg_search(svigg_last, svigg_first)

        sis_matches, svigg_matches = await asyncio.gather(
            sis_search_task, svigg_search_task, return_exceptions=True
        )

        if isinstance(sis_matches, Exception):
            logger.warning("patient_360 SIS search failed: %s", sis_matches)
            sis_matches = []
        if isinstance(svigg_matches, Exception):
            logger.warning("patient_360 Svigg search failed: %s", svigg_matches)
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
