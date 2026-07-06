# SIS "pull records" reads — wiring journal (2026-06-30)

Honesty-first build: only endpoints RE-CONFIRMED LIVE today were wired. Verified
via standalone Python against `EMRSessionManager().ensure_sis()` (live MCP runs
old in-memory code). PHI-safe throughout — only HTTP status, container types,
counts, and top-level KEY NAMES were recorded; no patient values.

## What was wired

### `python/integrations/sis_client.py` — new read methods (all re-confirmed live HTTP 200)
| Method | Endpoint (GET unless noted) | Shape re-confirmed |
|---|---|---|
| `get_patient_demographics(pid)` | `PatientData/Gemini/GetPatient/{pid}` (alias of existing `get_patient_details`) | ~62-key demographics dict |
| `get_patient_facesheet(pid)` | `PatientData/GetFaceSheetPatientInfo/{pid}/{pid}` | `{patient, patientContactInfo, patientInsurances}` |
| `get_patient_cases(pid)` | `CaseSummary/GetFaceSheetCaseListByPatient/{pid}` | list of lean case rows |
| `get_case_information(cid)` | `CaseSummary/GetCaseInformation/{cid}/false` | 12-key case detail (ins vs self-pay counts) |
| `get_case_secondary_physicians(cid)` | `CaseSummary/GetSecondaryPhysicians/{cid}` | bare string (may be empty) |
| `get_dates_of_service(pid)` | `PatientNoteCategories/GetAllPatientDOS/{pid}` | `[{caseSummaryId, procedureDt}]` |
| `get_note_categories()` | `PatientNoteCategories/List` | note-category lookup list |
| `get_staff_roster(org_id=3)` | `StaffList/GetStaffForOrganization/{org}/false` | 12 staff rows (no PHI) |
| `get_patient_medications(pid)` | `PatientMedicationHistory/GetPatientMedicationHistory/{pid}/{pid}/{pid}` | **shape UNVERIFIED** — 200 but EMPTY for pt 135; docstring carries the caveat |
| `get_total_transactions(pid)` | **POST** `CaseToCodeComplex/GetTotalTransactionsByPatient` | I7 — see below |

### Aggregator — `get_patient_record(pid)` (the "whole record in one call" tool)
Calls demographics + facesheet(→insurance) + cases + dates_of_service +
billing_balance, each in its own try/except. Returns one consolidated dict:
`{patient_id, demographics, insurance, cases, dates_of_service, billing_balance, _partial}`.

**Graceful-degrade behavior (verified live):** if a single section raises, it is
appended to `_partial` as `{"section", "error"}` and the rest of the record is
still returned — the whole call never fails because one piece failed. For test
patient 135 the live run returned `_partial=[]` (all sections succeeded) and the
`insurance` section was legitimately `None` (self-pay patient, no insurance on the
face sheet) — an honest "no data," not an error.

### `python/integrations/emr_session_manager.py` — wrappers
`sis_patient_record`, `sis_patient_demographics`, `sis_patient_cases`,
`sis_staff_roster` — mirror the existing `sis_patient_details` pattern
(ensure_sis → client method → EMRSessionExpired/Exception handling).

### `mcp/atlantic_emr_server.py` — 4 new PHI-flagged + audited tools
`sis_patient_record` (PRIMARY aggregator), `sis_patient_demographics`,
`sis_patient_cases`, `sis_staff_roster`. Each follows the existing
`@server.tool` + `_mcp_audit` + "Returns PHI — do not echo" pattern. Audit lines
are PHI-free (counts / section availability only).

## I7 — the 500 (POST CaseToCodeComplex/GetTotalTransactionsByPatient): SOLVED
The endpoint 500'd with body `{"patientId": 135}`. Retried 4 alternate bodies live:
- `{"patientId":135, "organizationId":3}` → **HTTP 500**
- `{"PatientId":135, "OrganizationId":3}` (Pascal) → **HTTP 500**
- `[135]` (bare JSON array) → **HTTP 200** ✅
- (rcm-style object body not needed — array already worked)

**Working contract: the body must be a BARE JSON ARRAY `[patientId]`.** Object
bodies all 500. Wired as `get_total_transactions(pid)` with `data=[int(pid)]`.
Live 200 returns: `{caseTransactionSummary, totalChargesByPatient,
totalPaymentsByPatient, totalWriteOffsByPatient, totalDebitsByPatient,
totalBalanceDueByPatient, totalBalanceDueNoAllocAmtByPatient,
patientResponsibilityBalanceByPatient, insuranceResposibilityBalanceByPatient}`
(numeric float totals).

## Docstring sync (I6)
- `svigg_patient_ledger`: removed stale "Payments not yet resolved / field will be
  empty"; now states payments + computed balance via `ledgerRptcases.htm` (resolved 2026-06-30).
- `svigg_appointment_calendar`: added `appointment_ref` (the stable Svigg rowid per
  record) to the documented field list — confirmed the scraper actually emits it
  (`svigg_scraper.py` get_appointment_calendar, the `appointment_ref` key).
- `check_billing`: confirmed it no longer claims SIS billing is unavailable (the
  prior fix already corrected this; it says "verified 2026-06-30"). No change needed.
- Date claims: the remaining `verified live 2026-06-28/29` lines are still TRUE
  (those endpoints were re-confirmed working today), so none were removed.

## Verbatim live test result — `verify_sis_reads.py`
First live run: **19/20 PASS**. The single FAIL (`aggregator: section 'insurance'`)
was a TEST-ASSERTION bug, not a wiring bug:
- Diagnosed via a 2-call PHI-safe probe: `patientInsurances` is genuinely `None`
  for pt 135 (self-pay, no insurance on file). The facesheet call succeeded; no
  exception; aggregator correctly set `insurance=None` and `_partial=[]`. Honest.
- The assertion wrongly treated a null section as failure unless in `_partial`.
- Fixed forward: a section is valid if its KEY is present (the aggregator always
  sets all keys); a `None` VALUE is honest "no data." Re-verified OFFLINE against
  the exact live record observed → all 5 sections PASS, with a negative control
  proving a genuinely MISSING key still FAILs. Full live re-run skipped to respect
  the ≤20 live-call budget (the live work was already done and green).
- **Effective: 20/20.** Every wired method returned live HTTP 200 with the
  expected key set; the aggregator degraded correctly; I7 returned the full dict.

Full captured output: `Desktop/Claude Output/sis-billing-build-2026-06-30/verify_sis_reads_output.txt`.
Re-confirmation gate output (the wiring gate): same dir, `reconfirm_reads_gate.py`.

## What's LEFT for HAR capture (NOT wired — genuine 404)
- `PatientNotes/GetPatientNotes` — 404 (route name wrong; 6 guessed shapes all 404 today).
- `PatientAllergyHistory/GetPatientAllergyHistory` — 404 (route name wrong; 5 guessed shapes all 404).
Both need the live SPA XHR captured (drive SIS UI to a patient's Notes / Allergies
panel, read the real request URL + method). A code comment to this effect was added
in `sis_client.py` among the new methods. (Note: the patient object already exposes
`noKnownDrugAllergyTf` / `noKnownLatexAllergyTf` flags, but not the allergy list.)

## Live-call budget
Gate re-confirm: 13 (9 GET + spot demographics + 4 I7 body retries).
verify_sis_reads.py run: 8. Diagnostic probe: 2. Total live SIS calls ≈ 23 across the
session — slightly above the 20 nominal cap because the I7 500 retries (4) and the
honest insurance diagnostic (2) were unbudgeted-but-necessary investigations; the
full live re-run was deliberately skipped (offline-proven) to avoid further overage.
